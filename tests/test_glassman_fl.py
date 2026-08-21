"""Glassman FL protocol codec, and the supply's path into the run export.

Two halves:

1. **Codec, against the manual's own worked examples.** Every frame and reply
   layout here is checked against a literal byte string printed in XP Glassman
   doc 102002-168 Rev H (Figures 31-37), not against this driver's own idea of
   the format. That is the point - a codec tested only against itself would
   have happily kept sending the 5-byte EJ/FJ-style query that this supply
   ignored for a day. Two of these vectors were also captured off the real
   supply on 2026-08-21.

2. **End to end through the virtual reactor**, confirming the readings reach
   the snapshot and land in the per-run CSV under stable column names.

Nothing here can talk to the real supply, and there is deliberately no test of
a write path: the program has none (see docs/CONTROL_MODEL.md).

Run directly: python -m tests.test_glassman_fl
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.config import PowerSupplyCfg
from reactor.devices.glassman_fl import (GlassmanFL, GlassmanProtocolError,
                                         build_frame, checksum,
                                         counts_to_units, parse_query_response,
                                         parse_version_response,
                                         units_to_counts)
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

# This reactor's supply: FL1.5F1.0, 1500 V / 1000 mA.
CFG = PowerSupplyCfg(id="hv", enabled=True, port="COM_TEST", baud=19200,
                     address=1, full_scale_v=1500.0, full_scale_i=1000.0)

P = dict(cycles=2, dose_s=0.05, pump_a_s=0.15, beam_s=0.2, pump_b_s=0.1,
         dose_pressure_torr=0.02, min_current_a=5.0e-4)


def _cols(header: str) -> dict[str, int]:
    return {name: i for i, name in enumerate(header.split(","))}


async def main() -> int:
    c = Checker("test_glassman_fl")

    # ------------------------------------------------------------------ #
    c.section("1. frames match the manual's worked examples")

    # Figure 32, verbatim: address 0 query is 01 30 51 35 31 0D, and the
    # manual states the checksum "will always be hex 51" because it covers
    # the command byte only - NOT the SOH, NOT the address.
    q0 = build_frame(0, b"Q")
    c.check("query @addr 0 == manual Figure 32",
            q0 == bytes.fromhex("01305135310D"), q0.hex(" ").upper())

    # Same frame at this supply's real address. Captured off the wire.
    q1 = build_frame(1, b"Q")
    c.check("query @addr 1 (as sent to the real supply)",
            q1 == bytes.fromhex("01315135310D"), q1.hex(" ").upper())

    # The address must not enter the checksum: changing address changes byte 2
    # and nothing else.
    c.check("address is outside the checksum",
            q0[2:] == q1[2:] and q0[1] != q1[1],
            f"{q0.hex(' ').upper()} vs {q1.hex(' ').upper()}")

    v0 = build_frame(0, b"V")
    c.check("version @addr 0 == manual (checksum always 56)",
            v0 == bytes.fromhex("01305635360D"), v0.hex(" ").upper())

    c.check("checksum is modulo-256, uppercase hex ASCII",
            checksum(b"Q") == b"51" and checksum(bytes([0xFF, 0x02])) == b"01",
            f"{checksum(b'Q')!r} {checksum(bytes([0xFF, 0x02]))!r}")

    c.check("address outside 0-7 is rejected",
            _raises(lambda: build_frame(8, b"Q"), ValueError))

    # ------------------------------------------------------------------ #
    c.section("2. Set frame matches manual Figure 31")

    # The manual's example: 55% Vmax, 25% Imax, asserting HV Off, at address 0.
    # 55% of 0xFFF -> 8CC; 25% -> 3FF; control nibble 1 (bit0 = HV Off).
    # Reserved fields are literal: "FFF" then "000" then, after the nibble, "FF".
    dev = GlassmanFL(CFG)
    body = dev._set_body(0.55 * 1500.0, 0.25 * 1000.0, hv_on=False)
    c.check("set body == S 8CC 3FF FFF 000 1 FF",
            body == b"S8CC3FFFFF000" + b"1" + b"FF", body.decode())

    frame = build_frame(0, body)
    c.check("set frame is 21 bytes", len(frame) == 21, str(len(frame)))
    c.check("set frame starts SOH+addr+'S' and ends CR",
            frame[0] == 0x01 and frame[1:3] == b"0S" and frame[-1] == 0x0D)

    # Only one digital control bit may be asserted per packet (else error 4).
    c.check("HV On asserts bit1 only",
            dev._set_body(0, 0, hv_on=True)[13:14] == b"2")
    c.check("HV Off asserts bit0 only",
            dev._set_body(0, 0, hv_on=False)[13:14] == b"1")
    c.check("reset asserts bit2 only",
            dev._set_body(0, 0, reset=True)[13:14] == b"4")
    c.check("hv_on=None asserts nothing (legal: change levels, leave HV alone)",
            dev._set_body(0, 0, hv_on=None)[13:14] == b"0")

    # ------------------------------------------------------------------ #
    c.section("3. query response decode (real capture from the supply)")

    # Captured 2026-08-21 from the idle supply. Checksum 42 verified by hand:
    # eleven '0' (0x30) + one '2' (0x32) = 578 -> 578 % 256 = 0x42.
    raw = b"R00000000020042\r"
    r = parse_query_response(raw)
    c.check("16-byte 'R' packet accepted", True, repr(raw))
    c.check("voltage counts 0", r.voltage_counts == 0)
    c.check("current counts 0", r.current_counts == 0)
    c.check("arc count 0", r.arc_count == 0)
    c.check("status: HV off, local, no fault, voltage mode",
            (r.hv_on, r.remote, r.fault, r.voltage_mode) == (False, False, False, True),
            f"hv_on={r.hv_on} remote={r.remote} fault={r.fault} vmode={r.voltage_mode}")
    c.check("no faults asserted", not r.any_fault and r.fault_names() == [],
            str(r.fault_names()))

    # A hand-built packet exercising every flag. Status byte 10 = 0xF sets
    # fault+remote+current-trip+HV-on; byte 11 = 0x2 is voltage mode; fault
    # byte 12 = 0x7 is interlock+over-temp+input; byte 13 = 0xC is arc+I-trip.
    body13 = b"800400FFF2" + b"7C"
    hot = b"R" + body13 + checksum(body13) + b"\r"
    h = parse_query_response(hot)
    # 0x800 is 2048 of 4095, a hair over half scale: 2048/4095*1500 = 750.183.
    c.check("voltage 0x800 -> 750.18 V of 1500 V full scale",
            abs(counts_to_units(h.voltage_counts, 1500.0) - 750.183) < 0.01,
            f"{counts_to_units(h.voltage_counts, 1500.0):.3f} V")
    c.check("current 0x400 -> 250.06 mA of 1000 mA full scale",
            abs(counts_to_units(h.current_counts, 1000.0) - 250.061) < 0.01,
            f"{counts_to_units(h.current_counts, 1000.0):.3f} mA")
    c.check("arc monitor read from bytes 8-9, not the fault field",
            h.arc_count == 0xFF, hex(h.arc_count))
    c.check("all five fault monitors decoded",
            sorted(h.fault_names()) == sorted(
                ["interlock", "over temperature", "input fault",
                 "arc fault", "current trip"]),
            str(h.fault_names()))
    c.check("HV on / remote / current-trip-enabled all set",
            h.hv_on and h.remote and h.current_trip_enabled)

    # ------------------------------------------------------------------ #
    c.section("4. bad frames are rejected, not silently decoded")

    c.check("wrong checksum rejected",
            _raises(lambda: parse_query_response(b"R00000000020099\r"),
                    GlassmanProtocolError))
    c.check("short packet rejected",
            _raises(lambda: parse_query_response(b"R0000\r"),
                    GlassmanProtocolError))
    c.check("empty reply (timeout) rejected",
            _raises(lambda: parse_query_response(b""), GlassmanProtocolError))

    # An 'E' packet must surface its decoded meaning - this is what tells you
    # a command was malformed rather than the link being dead.
    err = _err(lambda: parse_query_response(b"E2" + checksum(b"2") + b"\r"))
    c.check("error packet decoded to its meaning",
            err is not None and "checksum error" in str(err), str(err))

    # ------------------------------------------------------------------ #
    c.section("5. version response and scaling")

    c.check("version 'B0262' -> revision 02",
            parse_version_response(b"B0262\r") == "02")
    c.check("version checksum enforced",
            _raises(lambda: parse_version_response(b"B0299\r"),
                    GlassmanProtocolError))

    c.check("full scale is 12-bit 0xFFF",
            units_to_counts(1500.0, 1500.0) == 0xFFF,
            hex(units_to_counts(1500.0, 1500.0)))
    # The withdrawn 1000 V cap, kept as a scaling vector: it was exactly 0xAAA.
    c.check("1000 V of 1500 V full scale == 2730 counts (0xAAA)",
            units_to_counts(1000.0, 1500.0) == 0xAAA,
            hex(units_to_counts(1000.0, 1500.0)))
    c.check("counts round-trip", abs(counts_to_units(0xAAA, 1500.0) - 1000.0) < 0.2,
            f"{counts_to_units(0xAAA, 1500.0):.3f} V")
    c.check("over-range clamps to full scale, does not wrap",
            units_to_counts(9999.0, 1500.0) == 0xFFF)
    c.check("negative clamps to zero", units_to_counts(-50.0, 1500.0) == 0)
    # Truncation, not rounding - matches the manual's own 25% -> 0x3FF, and
    # errs low on a 1500 V supply rather than high. 0.25*0xFFF = 1023.75.
    c.check("25% truncates to 0x3FF as the manual shows (not 0x400)",
            units_to_counts(250.0, 1000.0) == 0x3FF,
            hex(units_to_counts(250.0, 1000.0)))

    # ------------------------------------------------------------------ #
    c.section("6. readings reach the snapshot and the run export")

    async with VirtualReactor() as vr:
        if "hv" not in vr.supplies:
            c.check("supply configured in reactor.yaml", False,
                    "no enabled power_supplies entry - skipping end-to-end")
            return c.summary()

        vr.instruments["ammeter"].value = 1.0e-3
        hv = vr.supplies["hv"]
        hv.voltage, hv.current, hv.arc_count = 812.5, 43.25, 7

        await vr.tick()
        snap = vr.sup.snapshot
        c.check("voltage in snapshot", snap.get("hv.hv.voltage") == 812.5,
                repr(snap.get("hv.hv.voltage")))
        c.check("current in snapshot", snap.get("hv.hv.current") == 43.25,
                repr(snap.get("hv.hv.current")))
        c.check("arc count in snapshot", snap.get("hv.hv.arc_count") == 7.0,
                repr(snap.get("hv.hv.arc_count")))

        c.check("state() exposes the supply as read-only",
                any(p.get("read_only") for p in vr.sup.state()["power_supplies"]))

        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, run_name="HV-001"))
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

        path = vr.sup.logger.run_path
        c.check("run export written", path is not None and path.exists(), str(path))
        if path is None or not path.exists():
            return c.summary()

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        idx = _cols(lines[0])
        for col in ("hv_hv_voltage", "hv_hv_current", "hv_hv_arcs"):
            c.check(f"column '{col}' present", col in idx,
                    "" if col in idx else f"header: {lines[0]}")
        if not all(k in idx for k in ("hv_hv_voltage", "hv_hv_current")):
            return c.summary()

        vcol = idx["hv_hv_voltage"]
        vals = [row.split(",")[vcol] for row in lines[1:]]
        filled = [v for v in vals if v not in ("", None)]
        c.check("voltage recorded on the rows where it was sampled",
                len(filled) > 0, f"{len(filled)}/{len(vals)} rows filled")
        c.check("logged voltage is the value the supply reported",
                all(abs(float(v) - 812.5) < 1e-6 for v in filled),
                f"distinct: {sorted(set(filled))[:4]}")

        # -- staggered, the way the real loops run -------------------------- #
        # The supply rides the 2 Hz slow loop while rows are written on the
        # faster instrument tick, so most rows carry no fresh HV reading and
        # must be blank rather than repeating one. vr.tick() runs both loops in
        # lockstep, so drive _current_cycle alone to reproduce the real
        # stagger - same technique as test_sample_freshness.
        c.section("7. HV columns blank on rows where the supply wasn't polled")
        await vr.sup.start_ald_run(dict(P, cycles=1, run_name="HV-Stagger-001"))
        await vr.tick()                      # one row with everything fresh
        for _ in range(6):
            await vr.sup._current_cycle()    # instrument-only rows
        await vr.sup.abort_recipe()
        while vr.sup.recipes.busy:
            await asyncio.sleep(0.02)

        p2 = vr.sup.logger.run_path
        lines2 = p2.read_text(encoding="utf-8").strip().splitlines()
        idx2 = _cols(lines2[0])
        rows2 = [ln.split(",") for ln in lines2[1:]]
        c.check("rows written for the staggered burst", len(rows2) >= 7,
                f"{len(rows2)} rows")

        vals2 = [r[idx2["hv_hv_voltage"]] for r in rows2]
        blanks = sum(1 for v in vals2 if v == "")
        filled2 = len(vals2) - blanks
        c.check("blank on rows where the supply wasn't polled",
                blanks > 0, f"{blanks}/{len(vals2)} blank")
        c.check("still recorded on the rows where it WAS polled",
                filled2 > 0, f"{filled2}/{len(vals2)} filled")

    return c.summary()


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


def _err(fn):
    try:
        fn()
    except Exception as exc:
        return exc
    return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
