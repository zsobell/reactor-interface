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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

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
    print(f"  HV supplies : {', '.join(f'{p.id}@{p.port} {p.baud}/8N1 addr {p.address} (monitor + HV off)' for p in cfg.power_supplies if p.enabled) or 'none'}")
    print(f"  ellipsometer: {f'{ell.host}:{ell.port} (read-only)' if ell.enabled and ell.host else 'disabled'}")
    print(f"  data dir    : {Path(cfg.site.data_dir).resolve()}")

    if args.check:
        print("\nconfig OK")
        return 0

    url = f"http://{args.host}:{args.port}/"
    print("\n  Connecting read-only. Nothing is commanded until you act in the UI.")
    print(f"  interface   : {url}\n")

    import uvicorn

    from .server.app import create_app

    if args.open:
        webbrowser.open(url)

    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
