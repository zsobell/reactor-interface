"""Entry point.

    python -m reactor --check     validate the config and exit
    python -m reactor             connect to the reactor and serve the interface

There is no simulation mode. Connecting is read-only: opening a session never
writes a setpoint or moves a valve, so it is safe to start this against the live
tool and just watch the readings.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import instances

#: Named, not `logging.getLogger(__name__)`: this module is `__main__` when run
#: with -m, and the file handler and level are configured for "reactor".
#: It was missing entirely until 2026-08-28, and the one place that used it was
#: the shutdown deadline thread below - which therefore died on a NameError
#: instead of ending a wedged process. Nothing else referenced it, so nothing
#: ever raised where anyone would see it.
log = logging.getLogger("reactor")


RESTART_PARENT_WAIT_S = 15.0
RESTART_HANDSHAKE_WAIT_S = 3.0


def restart_command(args: argparse.Namespace, parent_pid: int,
                    ready_file: Path | None = None) -> list[str]:
    """Build the replacement invocation without carrying `--open` across.

    The existing browser page is deliberately retained.  The replacement waits
    for its parent before it connects to a device or attempts to bind the port.
    """
    command = [sys.executable, "-m", "reactor", "--host", str(args.host),
               "--port", str(args.port), "--restart-after-pid", str(parent_pid)]
    if ready_file is not None:
        command.extend(("--restart-ready-file", str(ready_file)))
    command.extend(("--restart-lifecycle-file", str(instances.LIFECYCLE_PATH)))
    if args.config is not None:
        command.extend(("--config", str(args.config)))
    if args.verbose:
        command.append("--verbose")
    return command


def wait_for_parent_exit(parent_pid: int, *, timeout_s: float = RESTART_PARENT_WAIT_S) -> bool:
    """Do not let a replacement overlap its parent on the reactor hardware.

    ``os.kill(pid, 0)`` is a POSIX liveness idiom, not a portable one.  On this
    Windows host it raises WinError 6 for a healthy parent.  The instance
    registry already owns the tested Win32 process query, so restart uses that
    same source of truth.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not instances.is_alive(parent_pid):
            return True
        time.sleep(0.05)
    return not instances.is_alive(parent_pid)


def launch_replacement(args: argparse.Namespace, parent_pid: int,
                       ready_file: Path) -> subprocess.Popen:
    """Start one detached replacement that waits for this process to exit."""
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return subprocess.Popen(
        restart_command(args, parent_pid, ready_file),
        cwd=str(Path(__file__).resolve().parent.parent),
        close_fds=True,
        creationflags=flags,
    )


def start_restart_handoff(args: argparse.Namespace) -> dict[str, int]:
    """Launch the replacement and describe it to the HTTP restart route.

    This lives at module scope deliberately: ``main`` also has windowless
    startup fallbacks, and imports made inside a function become local names
    for its nested callbacks.  The handoff must always be able to read this
    process's PID before any shutdown is committed.
    """
    parent_pid = os.getpid()
    ready_file = (instances.INSTANCES_DIR
                  / f"restart-{parent_pid}-{secrets.token_hex(8)}.ready.json")
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.unlink(missing_ok=True)
    replacement = launch_replacement(args, parent_pid, ready_file)
    deadline = time.monotonic() + RESTART_HANDSHAKE_WAIT_S
    try:
        while time.monotonic() < deadline:
            try:
                ready = json.loads(ready_file.read_text(encoding="utf-8"))
                child_pid = int(ready.get("pid", 0))
                if (ready.get("parent_pid") == parent_pid and child_pid > 0
                        and ready.get("state") == "waiting"):
                    log.warning("restart handoff confirmed replacement PID %s", child_pid)
                    return {"pid": child_pid}
            except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
            time.sleep(0.025)
    finally:
        ready_file.unlink(missing_ok=True)

    with contextlib.suppress(Exception):
        if replacement.poll() is None:
            replacement.terminate()
    raise RuntimeError(
        f"replacement did not confirm its waiting state within "
        f"{RESTART_HANDSHAKE_WAIT_S:g} seconds")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reactor", description=__doc__)
    ap.add_argument("-c", "--config", type=Path, default=None)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address; 0.0.0.0 (default) exposes on all "
                         "interfaces incl. Tailscale/LAN, 127.0.0.1 is localhost-only")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open a browser on start")
    ap.add_argument("--restart-after-pid", type=int, default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--restart-ready-file", type=Path, default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--restart-lifecycle-file", type=Path, default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--check", action="store_true",
                    help="validate the config, print a summary, and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    # pythonw.exe (the taskbar shortcut) runs with NO console, so sys.stdout and
    # sys.stderr are None. Every print() below would raise, and uvicorn installs
    # its own logging handlers on those streams and dies on the way up - which
    # is exactly what happened first time: the process started, wrote two lines
    # to server.log, and vanished with nothing to show why.
    #
    # Point both at the log file instead. Everything the console would have
    # shown then lands there, which is the only record a windowless launch has.
    log_path = Path(__file__).resolve().parent.parent / "server.log"
    windowless = sys.stdout is None or sys.stderr is None
    if windowless:
        try:
            stream = open(log_path, "a", encoding="utf-8", buffering=1)
            sys.stdout = stream
            sys.stderr = stream
        except Exception:
            # Nowhere to write and nowhere to complain; carry on rather than
            # refusing to start.
            sys.stdout = sys.stderr = open(os.devnull, "w")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # "socket.send() raised exception." from asyncio, repeatedly.
    #
    # It means a WebSocket client vanished without closing - a laptop sleeping,
    # a Tailscale link dropping, a browser tab closing. asyncio logs it at
    # WARNING on the proactor event loop, and with several remote viewers it
    # scrolls the console constantly. Nothing is wrong and nothing is lost: the
    # UI reconnects by itself every 1.5 s.
    #
    # Errors still get through - this only lifts the floor to ERROR, so a real
    # asyncio failure is still shown. -v restores the full noise.
    if not args.verbose:
        logging.getLogger("asyncio").setLevel(logging.ERROR)

    if args.restart_after_pid is not None:
        try:
            if args.restart_ready_file is None:
                raise RuntimeError("replacement was not given a readiness file")
            # Prove the Windows liveness query works before telling the parent
            # it is safe to tear down.  This handshake would have caught the
            # WinError 6 failure while the old server was still fully online.
            parent_alive = instances.is_alive(args.restart_after_pid)
            if not parent_alive:
                raise RuntimeError(
                    f"cannot verify parent server process {args.restart_after_pid}")
            args.restart_ready_file.parent.mkdir(parents=True, exist_ok=True)
            args.restart_ready_file.write_text(json.dumps({
                "state": "waiting", "pid": os.getpid(),
                "parent_pid": args.restart_after_pid,
                "parent_alive": True,
            }), encoding="utf-8")
            instances.append_lifecycle_event(
                "restart", f"replacement process {os.getpid()} is ready and waiting "
                f"for server {args.restart_after_pid} to exit",
                path=args.restart_lifecycle_file)
        except Exception as exc:
            instances.append_lifecycle_event(
                "error", f"restart replacement could not establish its waiting state: {exc}",
                path=args.restart_lifecycle_file)
            log.exception("restart replacement readiness handshake failed")
            return 1
        if not wait_for_parent_exit(args.restart_after_pid):
            message = (f"restart replacement gave up waiting for server "
                       f"{args.restart_after_pid} to exit")
            instances.append_lifecycle_event("error", message,
                                             path=args.restart_lifecycle_file)
            log.error(message)
            return 1
        instances.append_lifecycle_event(
            "restart", f"previous server {args.restart_after_pid} exited; "
            f"replacement process {os.getpid()} is starting",
            path=args.restart_lifecycle_file)

    # Log to a file as well as the console.
    #
    # Load-bearing when started with pythonw.exe (the taskbar shortcut), which
    # has no console at all: without this, a server that fails to start - bad
    # config, port already in use, a device raising at import - would vanish
    # with no trace anywhere. Rotating so it cannot grow without bound.
    try:
        from logging.handlers import RotatingFileHandler

        if windowless:
            raise RuntimeError("already redirected into the log file")
        fh = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(fh)
        logging.getLogger("reactor").info("--- server starting ---")
    except Exception:
        # Windowless mode, or the file could not be opened. Either way there is
        # nothing useful to say and startup must not depend on it.
        pass

    from .config import load_config

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    gauge = cfg.pressure.scaling
    curve = gauge.preset or f"{gauge.type} gain={gauge.gain} offset={gauge.offset}"

    ell = cfg.ellipsometer

    print(f"  site        : {cfg.site.name}")
    # Three independent timers, three numbers. This used to print current_hz
    # twice and label it "current + MFC", which hid the MFC rate entirely - and
    # that rate is deliberately just ABOVE the row rate (see SiteConfig).
    print(f"  loop rate   : {cfg.site.loop_hz} Hz DAQ / "
          f"{cfg.site.current_hz} Hz current+rows / {cfg.site.mfc_hz} Hz MFC")
    print(f"  pressure    : {cfg.pressure.channel}  curve={curve}")
    print(f"  stage TC    : {cfg.stage_temp.channel or '(disabled)'}"
          f"  type {cfg.stage_temp.tc_type}")
    print(f"  aux inputs  : {', '.join(a.id for a in cfg.aux_inputs) or 'none'}")
    print(f"  gauges      : {', '.join(g.id for g in cfg.gauges) or 'none'}")
    print(f"  valves      : {', '.join(v.id for v in cfg.valves) or 'none'}")
    print(f"  MFCs        : {', '.join(m.id for m in cfg.mfcs) or 'none'}")
    print(f"  instruments : {', '.join(i.id for i in cfg.instruments if i.enabled) or 'none'}")
    def _supply_line(p) -> str:
        if p.driver == "glassman_fl":
            return (f"{p.id}@{p.port} {p.baud}/8N1 addr {p.address} "
                    f"(monitor + HV off)")
        # Port is resolved from the USB serial at connect, so there is nothing
        # useful to print for it here - the serial is the identity.
        if p.sample_bias:
            # Since 2026-08-26 the bias is armed at pre-start and switched by
            # the beam, not held on for the whole run like the coils.
            what = "SAMPLE BIAS: armed at pre-start, output brackets the beam"
        elif p.prestart_output:
            what = "output on at pre-start, off at run end"
        else:
            what = "monitor only"
        return f"{p.id}#{p.usb_serial} {p.model or p.driver} ({what})"

    supplies = [p for p in cfg.power_supplies if p.enabled]
    print(f"  supplies    : {supplies[0] and _supply_line(supplies[0]) if supplies else 'none'}")
    for p in supplies[1:]:
        print(f"                {_supply_line(p)}")
    print(f"  ellipsometer: {f'{ell.host}:{ell.port} (read-only)' if ell.enabled and ell.host else 'disabled'}")
    print(f"  data dir    : {Path(cfg.site.data_dir).resolve()}")

    if args.check:
        print("\nconfig OK")
        return 0

    # 0.0.0.0 is a BIND address, not a browsable one - "http://0.0.0.0:8000/"
    # is not reliably reachable and on Windows usually just fails. Show and open
    # a loopback URL instead; the bind is unchanged, so Tailscale/LAN access
    # still works exactly as before.
    browse_host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    url = f"http://{browse_host}:{args.port}/"
    print("\n  Connecting read-only. Nothing is commanded until you act in the UI.")
    print(f"  interface   : {url}\n")

    import uvicorn

    from .server.app import create_app

    if args.open:
        # After a short delay and off the main thread: the browser is launched
        # before server.run() binds, so opening immediately can land on a
        # connection-refused page. This is the normal path now that the taskbar
        # shortcut passes --open.
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # Served through an explicit uvicorn.Server rather than uvicorn.run() so the
    # UI's "Shut down server" button has something to ask for a graceful
    # shutdown. uvicorn.run() gives no handle on the running server, and killing
    # the process instead would skip the lifespan teardown - i.e. skip
    # Supervisor.stop(), which is what disconnects the devices and aborts a
    # running recipe properly. See /api/server/shutdown in server/app.py.
    #
    # There used to be a Restart button here that re-exec'd the process. It was
    # removed on 2026-08-25: an old instance survived the restart, sat holding
    # COM8-COM12, and the new one came up unable to reach any of them. Stopping
    # cleanly and starting from the shortcut is both simpler and easier to see.
    #: How long uvicorn may wait for open connections to close before it gives
    #: up on them and tears the app down anyway.
    #:
    #: THIS IS THE SHUTDOWN BUG (diagnosed 2026-08-28). uvicorn's default is
    #: None - wait forever - and its drain loop runs BEFORE the lifespan
    #: shutdown. So one WebSocket that never closes (a laptop asleep over
    #: Tailscale, a browser gone without a FIN) parks the entire stop: the
    #: listening socket is released at once, but `Supervisor.stop()` is never
    #: reached, and the process sits there holding the DAQ and COM8-COM12.
    #:
    #: server.log, 2026-08-28: "[command] server shutdown requested from the UI"
    #: at 14:53:20 and then not one further line from that process - no
    #: teardown, no error. The next server started 20 s later and got "resource
    #: is reserved" from the DAQ and "Access is denied" on COM9; the old one was
    #: still there at 14:54:10, when a second shutdown finally taskkilled it.
    #: The silence was uvicorn's own INFO "Waiting for connections to close",
    #: which log_level="warning" suppresses.
    #:
    #: On timeout uvicorn cancels the stragglers and CONTINUES into the lifespan
    #: shutdown, so the devices are still released properly. That is why this is
    #: a timeout and not `force_exit`, which would skip the teardown entirely.
    #:
    #: Cut from 3 s to 1 s on 2026-09-10. It no longer gates anything the
    #: operator is waiting on: /api/server/shutdown runs the device teardown
    #: itself, before it answers, so by the time the drain starts the DAQ and
    #: the COM ports are ALREADY released and this is just the socket tidying
    #: up after a page that has gone away.
    SHUTDOWN_DRAIN_S = 1

    app = create_app(cfg, restart_parent_pid=args.restart_after_pid)
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning",
                       timeout_graceful_shutdown=SHUTDOWN_DRAIN_S))

    #: How long the whole graceful stop gets before the process is ended anyway.
    #:
    #: 20 s until 2026-09-10, and Zach was watching all of it: "20 s hold is way
    #: too long". It was sized for a teardown that had not happened yet. Now the
    #: teardown runs inside the shutdown REQUEST (see /api/server/shutdown), so
    #: everything left after the response is socket cleanup and an interpreter
    #: exit - and if THAT takes more than a moment, waiting longer has never
    #: once helped. The devices are already released either way, so the fallback
    #: costs nothing.
    SHUTDOWN_DEADLINE_S = 3.0

    def request_shutdown() -> None:
        server.should_exit = True      # uvicorn tears down gracefully

        # ...and a hard deadline behind it. Reported 2026-08-27: pressing "Shut
        # down server" greyed the button and did nothing - the process stayed
        # up holding the DAQ and COM8-COM12, so the next server started could
        # not reach a single device ("resource is reserved", "Access is
        # denied"), and it took a SECOND shutdown from the new server to
        # taskkill the old one. The first press can never taskkill it, either:
        # _kill_other_reactor_servers skips this PID and its parent, and the
        # parent IS the other `-m reactor` process (the venv's pythonw.exe
        # launcher shim re-execs the real interpreter as a child).
        #
        # So the graceful path is the ONLY way this instance ends, and it must
        # not be able to hang. A daemon timer ends the process if the teardown
        # has not finished in time. (Until 2026-08-28 this timer could not do
        # that: its first statement referenced a `log` this module never
        # defined, so it raised NameError and never reached the exit below.
        # There is no trace of that in server.log because it had never once
        # been reached - the drain above hung long before 20 s were up.)
        #
        # Forcing an exit here is not a data risk in the way it would be
        # mid-run: the recipe abort and the pre-start abort
        # have already run above (that is the first thing Supervisor.stop does),
        # the MFCs zero their own setpoints the moment this program's sockets
        # close, and valve state is persisted rather than inferred. A zombie
        # holding the hardware is strictly worse than an abrupt exit.
        def _deadline() -> None:
            time.sleep(SHUTDOWN_DEADLINE_S)
            log.error("shutdown did not complete in %.0fs - ending the process "
                      "anyway so the DAQ and the serial ports are released. "
                      "The last 'shutdown: ...' line above names the step that "
                      "hung.", SHUTDOWN_DEADLINE_S)
            _flush()
            os._exit(1)

        threading.Thread(target=_deadline, name="shutdown-deadline",
                         daemon=True).start()

    def _flush() -> None:
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()

    app.state.request_shutdown = request_shutdown

    def request_restart() -> dict[str, int]:
        """Create the waiting child before committing this server to shutdown."""
        return start_restart_handoff(args)

    app.state.request_restart = request_restart

    # Announce this process so another instance's Shut down button can find it
    # without asking Windows (reactor/instances.py). Only a real
    # `python -m reactor` registers - an app built inside a test must never be
    # reachable by the sweep.
    instances.register(args.port)

    # try/finally, not a bare call. THIS IS THE ORPHAN BUG (2026-09-10).
    #
    # uvicorn 0.52's Server.startup() does `logger.error(exc); sys.exit(
    # STARTUP_FAILURE)` when the bind fails. sys.exit raises SystemExit, which
    # propagates straight out of server.run() - so the os._exit(0) below was
    # skipped, and because something in this stack keeps a NON-DAEMON thread
    # alive, the interpreter did not end either. The second copy just sat
    # there.
    #
    # Observed on 2026-09-10: two servers started within the same second, one
    # bound port 8000, the other failed to bind and lingered holding COM10 and
    # COM11 - so `steering` and `grid_bias` could not connect and looked like
    # dead hardware. The comment here used to claim this exact case was
    # covered. It was covered only on the path where run() RETURNS.
    #
    # Whatever comes out of run() - a clean return, a bind failure, a startup
    # error - this process ends, and ends released.
    status = 0
    try:
        server.run()
    except SystemExit as exc:
        # The bind failure above, almost always. Keep the code rather than
        # reporting success - `python -m reactor` is scriptable even if the
        # windowless shortcut never looks.
        status = int(exc.code or 0)
        log.error("server exited during startup (status %s) - most likely "
                  "port %s is already in use by another reactor server",
                  status, args.port)
        if args.restart_after_pid is not None:
            instances.append_lifecycle_event(
                "error", f"replacement server failed during startup with status {status}; "
                f"port {args.port} may still be in use",
                path=args.restart_lifecycle_file)
    except BaseException:
        status = 1
        log.exception("server stopped on an unhandled exception")
        if args.restart_after_pid is not None:
            instances.append_lifecycle_event(
                "error", "replacement server stopped on an unhandled startup exception",
                path=args.restart_lifecycle_file)
    finally:
        instances.unregister()
        # Do NOT fall out of main() and wait for the interpreter to finish:
        # the non-daemon thread above is exactly how an orphan stayed up
        # holding its serial ports. Go now.
        _flush()
        os._exit(status)


if __name__ == "__main__":
    sys.exit(main())
