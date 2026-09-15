"""Which reactor servers are running right now, and how to end one.

WHY THIS EXISTS
===============
The Shut down button kills every OTHER `python -m reactor` process before
stopping this one, so nothing is left holding the DAQ or COM8-COM12. Until
2026-09-10 it found them by shelling out to PowerShell:

    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ... }

which is correct and unbearably slow. `powershell.exe` cold-starts in 1-3 s on
this machine, and that ran INSIDE the shutdown request, before the browser was
told anything at all - so the button greyed out and sat there. Zach, 2026-09-10:
"20 s hold is way too long, and there is no way for me to know if it worked or
not."

The command line was the only thing that distinguished a reactor server from
any other `pythonw.exe`, and nothing in the standard library will read another
process's command line on Windows. So the servers say who they are instead:
each one drops a small JSON file here at startup and removes it on the way out.
Reading a directory is microseconds, and killing is a `TerminateProcess` call
rather than another subprocess.

WHAT REGISTERS
==============
Only `python -m reactor` does, from `reactor.__main__` (see `register`). A
Supervisor or FastAPI app built inside a test never appears here, which is the
point - the sweep must not be able to reach the test runner that started it.

STALE ENTRIES
=============
A process killed with /F, or one that dies on a DAQmx fault, leaves its file
behind. Every read verifies liveness, so a stale file is inert; `live_others`
deletes the ones it finds dead. PID reuse is guarded twice: the recorded exe
path must still match the running image, and that image must look like Python.
Neither check is free of the theoretical race, but the failure mode is bounded -
worst case a sibling server is missed and the operator presses the button again,
which is exactly where this started.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

#: One file per running server, named for its PID. A directory rather than a
#: single shared file on purpose: two servers starting at once would race on a
#: read-modify-write, and the whole point is that a second copy is a normal
#: thing to have to clean up after.
INSTANCES_DIR = Path(__file__).resolve().parent.parent / "config" / "instances"

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _image_path(pid: int) -> str:
    """Full path of the running image for `pid`, or "" if it cannot be read.

    Used to tell a recycled PID from the process that registered it.
    """
    if os.name != "nt":
        return ""
    k = _kernel32()
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        k.CloseHandle(h)


def is_alive(pid: int) -> bool:
    """True if `pid` names a process that has not exited.

    `os.kill(pid, 0)` is not usable here: on Windows it is implemented with
    TerminateProcess and would kill what it was asked to check.
    """
    if pid <= 0 or os.name != "nt":
        return False
    k = _kernel32()
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_uint32()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def terminate(pid: int) -> bool:
    """End `pid` immediately. True if it is gone afterwards.

    Blunt on purpose. Every caller has already released its own hardware, and a
    sibling that has not is precisely what we are removing - waiting politely
    for it is what left the DAQ reserved and COM9 "Access is denied".
    """
    if os.name != "nt":
        return False  # Cross-platform development never terminates host processes.
    k = _kernel32()
    h = k.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not h:
        return not is_alive(pid)
    try:
        k.TerminateProcess(h, 1)
    finally:
        k.CloseHandle(h)
    # TerminateProcess is asynchronous: it queues the kill and returns.
    for _ in range(50):
        if not is_alive(pid):
            return True
        time.sleep(0.01)
    return not is_alive(pid)


def register(port: int, *, directory: Path | None = None) -> Path | None:
    """Record this process as a running server. Returns the file, or None.

    Never raises: failing to register costs the sweep one process, and that is
    not a reason to refuse to start the server.
    """
    try:
        directory = directory or INSTANCES_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{os.getpid()}.json"
        path.write_text(json.dumps({
            "pid": os.getpid(),
            "port": port,
            # Both, and they routinely DISAGREE. Under a venv `sys.executable`
            # is the shim in .venv\Scripts, while the image Windows is actually
            # running is the base interpreter it redirects to - so comparing
            # sys.executable against QueryFullProcessImageName never matches and
            # would skip every sibling, silently disabling the sweep. `image` is
            # the one to compare; `exe` is kept because it is what identifies
            # WHICH install this server came from, and today's orphan pair
            # differed exactly there (venv vs system Python 3.12).
            "exe": sys.executable or "",
            "image": _image_path(os.getpid()),
            # The venv launcher. `.venv\\Scripts\\pythonw.exe` really is a
            # separate PROCESS that re-execs the base interpreter as a child -
            # measured, not assumed: from a venv, os.getppid() is the shim and
            # QueryFullProcessImageName on self reports Python312\\python.exe.
            # The old sweep used `taskkill /T`, which took the tree; terminating
            # only the registered PID would leave the shim behind. It holds no
            # hardware and no socket, but a stray pythonw.exe in Task Manager is
            # exactly the thing that makes an operator distrust the button.
            "ppid": os.getppid(),
            "pimage": _image_path(os.getppid()),
            "started_at": time.time(),
        }), encoding="utf-8")
        return path
    except Exception:
        return None


def unregister(path: Path | None = None) -> None:
    """Remove this process's entry. Never raises."""
    try:
        (path or INSTANCES_DIR / f"{os.getpid()}.json").unlink(missing_ok=True)
    except Exception:
        pass


def live_others(me: int | None = None, *, directory: Path | None = None) -> list[dict]:
    """Every registered server that is still running, except this one.

    Prunes entries whose process is gone, so the directory does not silently
    fill up with the ones that were killed rather than stopped.
    """
    me = os.getpid() if me is None else me
    out: list[dict] = []
    try:
        entries = sorted((directory or INSTANCES_DIR).glob("*.json"))
    except Exception:
        return out

    for path in entries:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            pid = int(rec.get("pid", 0))
        except Exception:
            # Unreadable or half-written: it tells us nothing and cannot be
            # acted on. Leave it rather than delete something we cannot read.
            continue

        if pid == me:
            continue
        if not is_alive(pid):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            continue

        # PID reuse guard. Compare against the recorded IMAGE, not `exe` - see
        # the note in register(). A record written before `image` existed, or
        # one where the lookup failed, falls back to "is this still a Python
        # process at all", which is enough to stop us terminating whatever
        # Windows handed the number to next.
        image = _image_path(pid)
        recorded = str(rec.get("image") or "")
        if image and recorded:
            if Path(image) != Path(recorded):
                continue
        elif image and "python" not in Path(image).name.lower():
            continue

        rec["path"] = str(path)
        out.append(rec)
    return out


def kill_others(me: int | None = None, *, directory: Path | None = None) -> list[int]:
    """Terminate every other registered server. Returns the PIDs actually gone.

    Takes the venv launcher with it (see `register`), but ONLY when that parent
    is itself a Python interpreter. Launched from a terminal instead, the parent
    is the shell - and killing an operator's console because they started the
    server from it would be its own bug.
    """
    killed: list[int] = []
    for rec in live_others(me, directory=directory):
        pid = int(rec["pid"])
        if not terminate(pid):
            continue
        killed.append(pid)

        ppid = int(rec.get("ppid") or 0)
        pimage = str(rec.get("pimage") or "")
        if (ppid > 0 and pimage and "python" in Path(pimage).name.lower()
                and is_alive(ppid) and _image_path(ppid) == pimage
                and ppid != (os.getpid() if me is None else me)):
            if terminate(ppid):
                killed.append(ppid)

        try:
            Path(rec["path"]).unlink(missing_ok=True)
        except Exception:
            pass
    return killed
