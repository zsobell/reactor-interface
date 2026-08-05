"""Briefly energise ONE digital output line, to find out what it drives.

    python -m tools.pulse_line cDAQ2Mod2/port0/line0
    python -m tools.pulse_line cDAQ2Mod2/port0/line0 --seconds 2

THIS DRIVES HARDWARE. On this system a digital output opens a pneumatic valve.
There is no read-only way to identify an output - a DAQmx output line cannot be
sensed, only driven - so this exists, deliberately separate from the main
program, with as much friction as is useful and no more.

What it does:
  * takes exactly one line, named explicitly on the command line
  * prints what it is about to do and waits for you to type "yes"
  * drives the line high for a short time, then low
  * puts it back low in a `finally`, so an interrupt or a crash still closes it

Before running, satisfy yourself that an unexpected valve opening is harmless:
gas supplies isolated, no precursor, and someone watching the box.
"""

from __future__ import annotations

import argparse
import sys
import time


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("line", help="exactly one line, e.g. cDAQ2Mod2/port0/line0")
    ap.add_argument("--seconds", type=float, default=1.0,
                    help="how long to hold it high (default 1.0, max 5)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (for repeat runs once you "
                         "have established the situation is safe)")
    args = ap.parse_args(argv)

    if "," in args.line or " " in args.line.strip():
        print("one line at a time, please.", file=sys.stderr)
        return 2
    hold = max(0.05, min(5.0, args.seconds))

    print()
    print("=" * 72)
    print(" THIS WILL DRIVE A PHYSICAL OUTPUT")
    print("=" * 72)
    print(f"  line      : {args.line}")
    print(f"  action    : drive HIGH for {hold:g} s, then LOW")
    print()
    print("  On this system that means opening whatever valve is wired to it.")
    print("  Confirm first that gas supplies are isolated and someone is watching")
    print("  the control box.")
    print()

    if not args.yes:
        try:
            if input('  Type "yes" to proceed: ').strip().lower() != "yes":
                print("  aborted.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\n  aborted.")
            return 1

    try:
        import nidaqmx
    except ImportError:
        print("  nidaqmx not installed.", file=sys.stderr)
        return 2

    task = None
    try:
        task = nidaqmx.Task(new_task_name="pulse_line")
        task.do_channels.add_do_chan(args.line)
        print(f"\n  driving HIGH  ({time.strftime('%H:%M:%S')})")
        task.write(True, auto_start=True)
        time.sleep(hold)
    except KeyboardInterrupt:
        print("\n  interrupted")
    except Exception as exc:
        print(f"\n  ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        # Always put the line back low, whatever happened above.
        if task is not None:
            try:
                task.write(False, auto_start=True)
                print(f"  driven LOW    ({time.strftime('%H:%M:%S')})")
            except Exception as exc:
                print(f"  !! COULD NOT DRIVE LOW: {exc}", file=sys.stderr)
                print("  !! CHECK THE VALVE BY HAND.", file=sys.stderr)
            try:
                task.close()
            except Exception:
                pass

    print("\n  If you saw or heard something move, record it in "
          "config/reactor.yaml:")
    print(f'      line: "{args.line}"')
    print("      identified: true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
