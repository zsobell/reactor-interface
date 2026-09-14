"""Shutting the server down must always reach the teardown, and always end.

Zach, 2026-08-28: "i still have the kill server bug - i press shut down server,
and the latest event says 'server shutdown requested from the UI', when I open
it ill get the DAQ error because its still running, then ill kill it again and
itll kill multiple instances, then boot correctly."

Two defects, both here:

  * uvicorn's connection drain is UNBOUNDED by default
    (`timeout_graceful_shutdown=None`) and runs BEFORE the lifespan shutdown.
    One WebSocket that never closes - a laptop asleep over Tailscale, a browser
    gone without a FIN - parks the whole stop. The listening socket is released
    immediately (so the next server binds port 8000 and looks fine), but
    `Supervisor.stop()` is never reached, so the old process keeps the DAQ and
    COM8-COM12 and the new one comes up unable to reach a single device.
  * the 20 s hard deadline behind it could not fire: its first statement used a
    `log` that `reactor/__main__.py` never defined, so the daemon thread died on
    NameError instead of ending the process. Nothing else in that module
    referenced `log`, so nothing ever raised anywhere visible.

Section 2 is the real reproduction: a WebSocket handler that behaves like the
reactor's (parked on a send queue, never reading, never closing) and a client
that never goes away. Against the old config it hangs forever; the assertion is
that the app's shutdown still runs, promptly.

No hardware and no Supervisor here - the bug is in how the process is served
and stopped, not in what it was serving.

Run directly: python -m tests.test_server_shutdown
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import uvicorn
import websockets
from fastapi import FastAPI, WebSocket

from reactor.__main__ import main as reactor_main   # noqa: F401  (import check)
from tests._support import Checker

#: Everything above the drain timeout the stop is allowed to take. The teardown
#: itself is a handful of disconnects; this is slack for the loop, not a budget.
SLACK_S = 5.0


def _undefined_globals(path: Path) -> set[str]:
    """Names a module loads that it never binds and that are not builtins.

    A cheap stand-in for a linter (this project has none installed). Pointed at
    the entry point on purpose: its cold paths - the shutdown deadline, the
    windowless-launch fallbacks - are the ones that can sit broken for days
    because nothing routine executes them. That is exactly what `log` did.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound: set[str] = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    loaded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (bound if isinstance(node.ctx, (ast.Store, ast.Del)) else loaded).add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for a in (args.posonlyargs + args.args + args.kwonlyargs
                          + [args.vararg, args.kwarg]):
                    if a is not None:
                        bound.add(a.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
    return loaded - bound


def _exit_is_guaranteed(path: Path) -> tuple[bool, str]:
    """Is the `server.run()` call wrapped in a try whose finally ends the process?

    A string search for "os._exit(0)" used to stand in for this and was not
    enough - it was present, and the process still lingered. uvicorn 0.52's
    Server.startup() calls sys.exit(STARTUP_FAILURE) when the bind fails, and
    the resulting SystemExit propagates straight out of server.run(), jumping
    over anything that merely FOLLOWS it. Only a finally runs on that path.

    Structural, so it survives the exit code being made non-zero.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        runs = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "run"
                   for stmt in node.body for n in ast.walk(stmt))
        if not runs:
            continue
        if not node.finalbody:
            return False, "server.run() is in a try, but it has no finally"
        exits = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "_exit"
                    for stmt in node.finalbody for n in ast.walk(stmt))
        return (exits, "" if exits else
                "the finally around server.run() does not end the process")
    return False, "server.run() is not inside a try/finally"


def _stuck_ws_app() -> tuple[FastAPI, dict]:
    """An app shaped like the reactor's: a WebSocket that only ever SENDS (so it
    never sees a disconnect on the receive side) and a shutdown hook that
    records whether it ran - standing in for Supervisor.stop() releasing the DAQ
    and the serial ports."""
    seen = {"shutdown": False}
    app = FastAPI()

    @app.on_event("shutdown")
    async def _stop() -> None:
        seen["shutdown"] = True

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        # CancelledError explicitly: it is a BaseException, and letting it out
        # makes uvicorn log a traceback over the test's own output when the
        # drain timeout cancels this task - which is the expected outcome here.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            while True:                      # never reads, never closes
                await asyncio.sleep(3600)

    return app, seen


async def main() -> int:
    c = Checker("test_server_shutdown")

    c.section("1. the entry point defines every name it uses")
    missing = _undefined_globals(Path("reactor/__main__.py"))
    # `log` was in here until 2026-08-28, used only by the shutdown deadline.
    c.check("reactor/__main__.py has no undefined globals", not missing,
            str(sorted(missing)) if missing else "")

    c.section("2. a WebSocket that never closes cannot park the shutdown")
    app, seen = _stuck_ws_app()
    drain = 2
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning",
        timeout_graceful_shutdown=drain))
    serving = asyncio.create_task(server.serve())
    for _ in range(200):                      # wait for the bind
        if server.started:
            break
        await asyncio.sleep(0.05)
    c.check("test server started", server.started)
    port = server.servers[0].sockets[0].getsockname()[1]

    sock = await websockets.connect(f"ws://127.0.0.1:{port}/ws")
    await asyncio.sleep(0.2)

    # The button's effect, minus the HTTP round trip.
    t0 = time.monotonic()
    server.should_exit = True
    try:
        await asyncio.wait_for(serving, timeout=drain + SLACK_S)
        took = time.monotonic() - t0
        c.check(f"serve() returned (<= {drain}s drain + slack)", True, f"{took:.1f}s")
    except asyncio.TimeoutError:
        # This is the bug: with an unbounded drain it never returns.
        took = time.monotonic() - t0
        c.check("serve() returned", False,
                f"still running after {took:.1f}s - the drain is unbounded again")
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving

    # The point of a timeout rather than force_exit: the app still gets torn
    # down, so on the real server the DAQ and the serial ports are released.
    c.check("the app's shutdown hook still ran (devices released)", seen["shutdown"])
    with contextlib.suppress(Exception):
        await sock.close()

    c.section("3. the shipped server is configured that way")
    # Built the way main() builds it, read back off the object it hands uvicorn.
    src = Path("reactor/__main__.py").read_text(encoding="utf-8")
    c.check("main() passes timeout_graceful_shutdown",
            "timeout_graceful_shutdown=SHUTDOWN_DRAIN_S" in src)
    c.check("SHUTDOWN_DRAIN_S is a small finite number",
            any(line.strip().startswith("SHUTDOWN_DRAIN_S = ")
                and 0 < float(line.split("=")[1]) <= 10
                for line in src.splitlines()))
    # However the server ends - button, failed bind, startup error - the process
    # must not be left behind holding hardware.
    guaranteed, why = _exit_is_guaranteed(Path("reactor/__main__.py"))
    c.check("the exit after server.run() is unconditional",
            guaranteed and 'if shutdown["wanted"]:' not in src, why)
    # The operator watches this number tick down. It was 20 s, sized for a
    # teardown that had not run yet; the teardown now happens in the request.
    c.check("SHUTDOWN_DEADLINE_S is a few seconds, not tens",
            any(line.strip().startswith("SHUTDOWN_DEADLINE_S = ")
                and 0 < float(line.split("=")[1]) <= 5
                for line in src.splitlines()))

    c.section("4. the sweep can see another server without asking Windows")
    # The PowerShell Win32_Process query was 1-3 s of cold start inside the
    # shutdown request. The registry replaces it - but only if it can actually
    # match a live process. The first cut compared sys.executable against the
    # running image and NEVER matched under a venv (the shim in .venv\\Scripts
    # is not the image Windows runs), which silently swept nothing at all.
    from reactor import instances

    path = instances.register(8099)
    c.check("registering wrote an entry", path is not None and path.exists())
    try:
        # me=1 so this very process counts as "another" one.
        others = instances.live_others(me=1)
        c.check("a live server is found by the sweep",
                any(r["pid"] == os.getpid() for r in others),
                f"found {[r['pid'] for r in others]}, this pid is {os.getpid()}")
        c.check("...and it never returns the caller itself",
                instances.live_others() == [])

        # The venv launcher is a real second process (measured: from a venv,
        # os.getppid() is .venv\\Scripts\\python.exe while this process's own
        # image is the base interpreter). The old sweep took the tree with
        # `taskkill /T`; this one has to be told about the parent or it leaves
        # a stray pythonw.exe behind.
        rec = json.loads(path.read_text(encoding="utf-8"))
        c.check("the entry records the parent launcher",
                rec.get("ppid") == os.getppid() and "pimage" in rec,
                f"ppid={rec.get('ppid')} vs {os.getppid()}")
        # `image` must be the RUNNING image, which is what live_others compares
        # against. Recording sys.executable there instead is the bug that made
        # the first version of this match nothing at all under a venv.
        c.check("...and the running image, which is what the guard compares",
                Path(rec.get("image", "")) == Path(instances._image_path(os.getpid())),
                f"recorded {rec.get('image')!r}")

        dead = instances.INSTANCES_DIR / "999999.json"
        dead.write_text(json.dumps({"pid": 999999, "port": 8000, "exe": "",
                                    "image": "", "started_at": 0}),
                        encoding="utf-8")
        instances.live_others(me=1)
        c.check("a stale entry for a dead PID is pruned", not dead.exists())
    finally:
        instances.unregister(path)
    c.check("unregistering removed the entry", path is None or not path.exists())

    c.section("5. the button answers with a receipt, before it goes quiet")
    # The whole point of the 2026-09-10 change: the response is sent AFTER the
    # teardown, so what the page shows is evidence rather than inference. Driven
    # over raw ASGI with no lifespan, so this never touches the DAQ or the
    # serial ports of the server actually running the reactor.
    from reactor.server import app as app_mod
    from tests._support import asgi_call

    app = app_mod.create_app()
    asked = {"n": 0}
    app.state.request_shutdown = lambda: asked.__setitem__("n", asked["n"] + 1)

    t0 = time.monotonic()
    status, body = await asgi_call(app, "POST", "/api/server/shutdown")
    took = time.monotonic() - t0

    c.check("200 from the button", status == 200, f"{status} {body}")
    # Zach, 2026-09-10: "it needs to be a lot faster". The PowerShell sweep this
    # replaced cost 1-3 s of cold start on its own, every press, before the
    # browser heard anything at all.
    c.check("it answered promptly", took < 2.0, f"{took:.2f}s")
    for key in ("released", "failed", "ok", "elapsed_s", "also_killed"):
        c.check(f"the receipt carries `{key}`", key in body, str(sorted(body)))
    c.check("the receipt reports the teardown, not just an intention",
            isinstance(body.get("released"), list))
    # Devices were never connected here (no lifespan), so nothing should be
    # reported as FAILING to disconnect - a spurious failure would put a red
    # warning in front of the operator on every clean shutdown.
    c.check("a never-started server tears down cleanly",
            body.get("ok") is True, str(body.get("failed")))

    # The process exit is scheduled behind the response, not in front of it.
    c.check("the process was not ended before answering", asked["n"] == 0)
    await asyncio.sleep(0.4)
    c.check("...and is requested just after", asked["n"] == 1, str(asked["n"]))

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
