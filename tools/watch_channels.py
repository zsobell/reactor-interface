"""Watch every DAQ input and report what changes. READ-ONLY.

    python -m tools.watch_channels
    python -m tools.watch_channels --seconds 300 --threshold 0.02

This is the identification workhorse. It takes a baseline, then polls every
digital input line and every analog voltage input, printing a line whenever
something moves. Go and flip a valve, or change a pressure, and it tells you
which channel responded.

Drives nothing. It creates input tasks only, so it cannot actuate a valve.

Needs the LabVIEW VI closed - DAQmx gives one program exclusive use of a
module's inputs.
"""

from __future__ import annotations

import argparse
import sys
import time


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=600.0,
                    help="how long to watch (default 600; Ctrl-C to stop early)")
    ap.add_argument("--threshold", type=float, default=0.05,
                    help="volts of change on an analog input worth reporting "
                         "(default 0.05)")
    ap.add_argument("--rate", type=float, default=4.0, help="polls per second")
    args = ap.parse_args(argv)

    try:
        import nidaqmx
        from nidaqmx.system import System
    except ImportError:
        print("nidaqmx not installed.", file=sys.stderr)
        return 2

    try:
        devices = list(System.local().devices)
    except Exception as exc:
        print(f"could not reach the DAQmx driver: {exc}", file=sys.stderr)
        return 2

    di_lines: list[str] = []
    ai_chans: list[str] = []
    for dev in devices:
        di_lines.extend(ln.name for ln in dev.di_lines)
        try:
            rngs = list(dev.ai_voltage_rngs)
            # Only true voltage modules; a thermocouple module is millivolt-scale
            # and its raw volts are not useful here.
            if rngs and max(rngs) >= 1.0:
                ai_chans.extend(c.name for c in dev.ai_physical_chans)
        except Exception:
            pass

    print(f"watching {len(di_lines)} digital inputs and {len(ai_chans)} analog inputs")
    print(f"analog threshold: {args.threshold:g} V   duration: {args.seconds:g} s")
    print("go and change something. Ctrl-C to stop.\n")

    di_task = ai_task = None
    try:
        if di_lines:
            di_task = nidaqmx.Task(new_task_name="watch_di")
            for name in di_lines:
                di_task.di_channels.add_di_chan(name)
        if ai_chans:
            ai_task = nidaqmx.Task(new_task_name="watch_ai")
            for name in ai_chans:
                ai_task.ai_channels.add_ai_voltage_chan(name, min_val=-10.0, max_val=10.0)

        def sample():
            di = []
            ai = []
            if di_task is not None:
                v = di_task.read()
                di = v if isinstance(v, list) else [v]
            if ai_task is not None:
                v = ai_task.read()
                ai = v if isinstance(v, list) else [v]
            return di, ai

        base_di, base_ai = sample()
        print(f"baseline taken at {time.strftime('%H:%M:%S')}")
        for name, v in zip(di_lines, base_di):
            if v:
                print(f"   (already HIGH: {name})")
        print()

        last_di = list(base_di)
        deadline = time.time() + args.seconds
        period = 1.0 / max(0.5, args.rate)
        changes = 0

        while time.time() < deadline:
            time.sleep(period)
            di, ai = sample()
            stamp = time.strftime("%H:%M:%S")

            for name, was, now in zip(di_lines, last_di, di):
                if bool(was) != bool(now):
                    changes += 1
                    print(f"{stamp}  DIGITAL  {name:<28} "
                          f"{'low -> HIGH' if now else 'HIGH -> low'}")
            last_di = list(di)

            for name, b, now in zip(ai_chans, base_ai, ai):
                if abs(now - b) >= args.threshold:
                    changes += 1
                    print(f"{stamp}  ANALOG   {name:<28} "
                          f"{b:+.4f} -> {now:+.4f} V   (delta {now - b:+.4f})")
                    # Re-baseline so a sustained change reports once, not forever.
                    base_ai[ai_chans.index(name)] = now

        print(f"\ndone: {changes} change(s) seen")
    except KeyboardInterrupt:
        print("\nstopped")
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        for t in (di_task, ai_task):
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
