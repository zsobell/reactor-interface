"""Find an XP Glassman FL supply on a serial port. READ-ONLY.

    .venv\\Scripts\\python.exe -m tools.probe_glassman
    .venv\\Scripts\\python.exe -m tools.probe_glassman --port COM8
    .venv\\Scripts\\python.exe -m tools.probe_glassman --all-ports

Sends only `Q` (request monitors) and `V` (software version). Both are read
commands. Neither can change a setpoint and neither can enable high voltage,
so this is safe to run against a live supply at any time - the FL manual is
explicit that a query is answered "while still in LOCAL control mode ... at
any time".

WHY THIS EXISTS
===============
Bringing this supply up took a day, and the whole delay was one wrong
assumption: that its baud rate and address matched the documented defaults
(9600 / address 0) because every DIP switch on the unit reads "down". They do
not. It answers at **19200, address 1**.

The search that missed it swept addresses 0-F at 9600, then baud rates
1200-115200 at addresses 0 and F only. Both sweeps looked thorough. Neither
ever tested 19200 with address 1, and the silence was mistaken for a hardware
fault.

So this tool sweeps the **full cross-product** - every FL-supported baud rate
against every valid address - and does it by default. If a supply ever goes
quiet, run this before suspecting the hardware.

The COM port is not stable either: a TI TUSB3410 driver reinstall moved this
supply from COM7 to COM8. With no --port, this probes every serial port that
looks like a candidate.
"""

from __future__ import annotations

import argparse
import sys
import time

# Manual 102002-168 Rev H: the FL supports exactly these four rates (9600 is
# the documented default), and the address byte is one hex digit, 0-7.
BAUD_RATES = (9600, 19200, 4800, 2400)
ADDRESSES = range(8)

SOH = 0x01
CR = 0x0D


def build(address: int, body: bytes) -> bytes:
    """SOH + address + body + checksum(body) + CR. Checksum excludes SOH/address."""
    return (bytes([SOH]) + f"{address:01X}".encode() + body
            + f"{sum(body) % 256:02X}".encode() + bytes([CR]))


def candidate_ports() -> list[str]:
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    # A Glassman on USB shows up as a TI TUSB3410 bridge; on RS-232 it is
    # whatever adapter you plugged it into. Put the TI bridges first, but
    # return everything - the RS-232 path is equally valid.
    def rank(p):
        hw = (p.hwid or "").upper()
        return (0 if ("0451" in hw or "TUSB" in (p.description or "").upper())
                else 1)

    return [p.device for p in sorted(ports, key=rank)]


def describe_ports() -> None:
    from serial.tools import list_ports

    print("Serial ports present:")
    found = False
    for p in sorted(list_ports.comports(), key=lambda x: x.device):
        found = True
        print(f"  {p.device:6s} {p.description}")
        print(f"         {p.hwid}")
    if not found:
        print("  none")
    print()


def try_one(ser, address: int) -> tuple[bytes, bytes]:
    """Send Q then V at this address. Returns the two raw replies."""
    ser.reset_input_buffer()
    ser.write(build(address, b"Q"))
    time.sleep(0.05)
    q = ser.read_until(bytes([CR]), 32)

    ser.reset_input_buffer()
    ser.write(build(address, b"V"))
    time.sleep(0.05)
    v = ser.read_until(bytes([CR]), 32)
    return q, v


def decode(q: bytes, v: bytes) -> str:
    """Best-effort human summary of a hit, using the real decoder."""
    try:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        from reactor.devices.glassman_fl import (parse_query_response,
                                                 parse_version_response)
    except Exception:
        return ""
    out = []
    try:
        r = parse_query_response(q)
        out.append(f"V={r.voltage_counts:#05x} I={r.current_counts:#05x} "
                   f"arcs={r.arc_count} hv_on={r.hv_on} remote={r.remote} "
                   f"faults={r.fault_names() or 'none'}")
    except Exception as exc:
        out.append(f"(query undecodable: {exc})")
    try:
        out.append(f"firmware {parse_version_response(v)}")
    except Exception:
        pass
    return "  ".join(out)


def probe_port(port: str, bauds, addresses, settle: float) -> list[tuple]:
    import serial

    hits = []
    for baud in bauds:
        try:
            ser = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                                timeout=0.6, write_timeout=2.0)
        except Exception as exc:
            print(f"  @{baud:6d}  cannot open: {type(exc).__name__}: {exc}")
            time.sleep(settle)
            continue
        try:
            time.sleep(0.15)
            marks = []
            for address in addresses:
                q, v = try_one(ser, address)
                if q or v:
                    hits.append((port, baud, address, q, v))
                    marks.append(f"\033[1m{address}!\033[0m" if sys.stdout.isatty()
                                 else f"{address}!")
                else:
                    marks.append(".")
            print(f"  @{baud:6d}  addr " + " ".join(marks))
        finally:
            ser.close()
        # The TUSB3410 bridge wedges ("device is not ready") if you cycle
        # open/close too fast. Give it a moment between rates.
        time.sleep(settle)
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only probe for an XP Glassman FL power supply.")
    ap.add_argument("--port", help="probe only this port (e.g. COM8)")
    ap.add_argument("--all-ports", action="store_true",
                    help="probe every serial port, not just likely candidates")
    ap.add_argument("--baud", type=int, action="append",
                    help="probe only this baud rate (repeatable)")
    ap.add_argument("--address", type=int, action="append",
                    help="probe only this address 0-7 (repeatable)")
    ap.add_argument("--settle", type=float, default=0.4,
                    help="seconds between port opens (default 0.4)")
    args = ap.parse_args(argv)

    try:
        import serial  # noqa: F401
    except ImportError:
        print("pyserial not installed  ->  pip install pyserial")
        return 2

    describe_ports()

    if args.port:
        ports = [args.port]
    else:
        ports = candidate_ports()
        if not args.all_ports:
            ports = ports[:6]
    bauds = args.baud or BAUD_RATES
    addresses = args.address or ADDRESSES

    print(f"Probing {len(ports)} port(s) x {len(bauds)} baud x "
          f"{len(list(addresses))} address(es).  Q and V only - nothing is set.\n")

    all_hits = []
    for port in ports:
        print(f"{port}:")
        all_hits.extend(probe_port(port, bauds, addresses, args.settle))
        print()

    print("=" * 70)
    if not all_hits:
        print("No reply anywhere.")
        print()
        print("Before concluding the supply is faulty, check:")
        print("  * the USB (J3) or RS-232 (J2 IN) cable is actually seated;")
        print("  * the supply is powered on;")
        print("  * the rear DB-25 (J1) interlock jumper is in - without it the")
        print("    supply faults, though it should still answer a query;")
        print("  * S2-10 selects RS232 (down), and no Ethernet adapter is fitted")
        print("    at J4 - Ethernet, when enabled, DISABLES both USB and RS232;")
        print("  * the COM port number has not moved (a driver reinstall has")
        print("    done that here before).")
        return 1

    for port, baud, address, q, v in all_hits:
        print(f"FOUND  {port}  {baud} 8N1  address {address}")
        print(f"   Q -> {q.hex(' ').upper() or '<none>'}   {q!r}")
        print(f"   V -> {v.hex(' ').upper() or '<none>'}   {v!r}")
        info = decode(q, v)
        if info:
            print(f"   {info}")
        print()
        print("   Put this in config/reactor.yaml under power_supplies:")
        print(f"       port: \"{port}\"")
        print(f"       baud: {baud}")
        print(f"       address: {address}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
