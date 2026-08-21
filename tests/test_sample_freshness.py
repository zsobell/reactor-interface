"""Run export records measurements, not carried-forward copies of them.

Channels are sampled by three independent loops at different rates (DAQ
site.loop_hz, instruments site.current_hz, MFCs site.mfc_hz). A row is written
every instrument tick, so most rows have no fresh DAQ or MFC reading. Those
cells must be EMPTY rather than repeating the last value - otherwise the file
claims measurements that never happened, and a plot drawn from it shows a
sample rate the tool never achieved.

This is the file-side half of the fix for the "compiled data is too sparse"
report: the other half was moving the MFC poll off the telemetry loop, where a
0.45-0.9 s HTTP read was throttling every sample to ~2.1 Hz.

Run directly: python -m tests.test_sample_freshness
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

P = dict(cycles=2, dose_s=0.05, pump_a_s=0.15, beam_s=0.2, pump_b_s=0.1,
         dose_pressure_torr=0.02, min_current_a=5.0e-4)


def _cols(header: str) -> dict[str, int]:
    return {name: i for i, name in enumerate(header.split(","))}


async def main() -> int:
    c = Checker("test_sample_freshness")

    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3

        c.section("1. a run export is written with per-channel freshness")
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, run_name="Fresh-001"))
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

        path = vr.sup.logger.run_path
        c.check("run export written", path is not None and path.exists(),
                str(path))
        if path is None or not path.exists():
            return c.summary()

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        idx = _cols(lines[0])
        rows = [ln.split(",") for ln in lines[1:]]
        c.check("has rows", len(rows) > 5, f"{len(rows)} rows")

        # The commanded valve flags are state, not measurements: they describe
        # what the program has told the hardware and are true at every instant,
        # so they must be present on every single row.
        for col in ("dosing", "beam_on"):
            filled = sum(1 for r in rows if r[idx[col]] != "")
            c.check(f"state column '{col}' filled on every row",
                    filled == len(rows), f"{filled}/{len(rows)}")

        # tick() advances all three loops together, so every channel is fresh on
        # every row here - that is the harness, not the code under test.
        for col in ("pressure", "inst_ammeter"):
            if col not in idx:
                continue
            vals = [r[idx[col]] for r in rows]
            c.check(f"'{col}' filled when every loop ticks together",
                    all(v != "" for v in vals),
                    f"{sum(1 for v in vals if v != '')}/{len(vals)} filled")

        c.check("filename starts with the run name",
                path.name.startswith("Fresh-001_"), path.name)

        # -- the real staggering ------------------------------------------- #
        # On hardware the instrument loop runs ~5 Hz while the DAQ runs ~2 Hz
        # and the MFCs ~1 Hz, so most rows have no fresh DAQ or MFC reading.
        # Drive that here by calling _current_cycle repeatedly WITHOUT the other
        # two: those channels must go blank instead of repeating themselves.
        c.section("2. a channel that wasn't resampled is logged blank")
        await vr.sup.start_ald_run(dict(P, cycles=1, run_name="Stagger-001"))
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

        amm = [r[idx2["inst_ammeter"]] for r in rows2]
        c.check("instrument channel fresh on every row",
                all(v != "" for v in amm),
                f"{sum(1 for v in amm if v != '')}/{len(amm)} filled")

        for col in ("pressure", "mfc_ar"):
            if col not in idx2:
                continue
            vals = [r[idx2[col]] for r in rows2]
            blanks = sum(1 for v in vals if v == "")
            filled = len(vals) - blanks
            c.check(f"'{col}' is blank on rows where it wasn't sampled",
                    blanks > 0, f"{blanks}/{len(vals)} blank")
            c.check(f"'{col}' still records the rows where it WAS sampled",
                    filled > 0, f"{filled}/{len(vals)} filled")

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
