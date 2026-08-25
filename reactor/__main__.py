"""Entry point.

    python -m reactor --check     validate the config and exit
    python -m reactor             connect to the reactor and serve the interface

There is no simulation mode. Connecting is read-only: opening a session never
writes a setpoint or moves a valve, so it is safe to start this against the live
tool and just watch the readings.
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reactor", description=__doc__)
    ap.add_argument("-c", "--config", type=Path, default=None)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address; 0.0.0.0 (default) exposes on all "
                         "interfaces incl. Tailscale/LAN, 127.0.0.1 is localhost-only")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open a browser on start")
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
            import os
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
    print(f"  loop rate   : {cfg.site.loop_hz} Hz DAQ / {cfg.site.current_hz} Hz "
          f"current + MFC")
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
        what = "output on at pre-start, off at run end" if p.prestart_output \
            else "monitor only"
        role = ", SAMPLE BIAS" if p.sample_bias else ""
        return f"{p.id}#{p.usb_serial} {p.model or p.driver} ({what}{role})"

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

    import os

    import uvicorn

    from .server.app import create_app

    if args.open:
        # After a short delay and off the main thread: the browser is launched
        # before server.run() binds, so opening immediately can land on a
        # connection-refused page. This is the normal path now that the taskbar
        # shortcut passes --open.
        import threading

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
    app = create_app(cfg)
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))

    shutdown = {"wanted": False}

    def request_shutdown() -> None:
        shutdown["wanted"] = True
        server.should_exit = True      # uvicorn tears down gracefully

    app.state.request_shutdown = request_shutdown

    server.run()

    if shutdown["wanted"]:
        # server.run() has returned, so the lifespan teardown has completed and
        # every device is disconnected. Do NOT fall out of main() and wait for
        # the interpreter to finish: something in this stack keeps a non-daemon
        # thread alive, which is exactly how the orphaned instance above stayed
        # up holding its serial ports. Go now.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
