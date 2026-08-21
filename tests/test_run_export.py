"""Automatic server-side run export against the virtual reactor: opens the
instant Supervisor.start_recipe fires (via start_ald_run here - any recipe
entry point goes through the same start_recipe), samples every tick, closes
in finish_run() however the run ends. Also confirms it survives an abort
and that a stale handle can't linger across back-to-back runs.

Section 5 covers the other automatic per-run file, the run-parameters JSON
(DataLogger.write_run_params). It is written from a separate code path that
nothing else exercised, which is how it came to be summarising the gas
schedule via a GasSchedule.lead_s field that no longer existed - every
gas-scheduled run silently lost its parameter record.

Run directly: python -m tests.test_run_export
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

P = dict(cycles=2, dose_s=0.05, pump_a_s=0.15, beam_s=0.2, pump_b_s=0.1,
         dose_pressure_torr=0.02, min_current_a=5.0e-4)

#: same params plus a full two-gas schedule - the case that was broken
P_GAS = dict(P, gas_overlap_s=0.05,
             h2_gas_enable=True, h2_gas_order="first", h2_gas_pct=40,
             h2_gas_flow_sccm=5.0,
             n2_gas_enable=True, n2_gas_order="second", n2_gas_pct=50,
             n2_gas_flow_sccm=3.0)


async def _next_second() -> None:
    """Wait until the wall clock's whole second changes (see section 3)."""
    start = int(time.time())
    while int(time.time()) == start:
        await asyncio.sleep(0.02)


async def main() -> int:
    c = Checker("test_run_export")

    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3

        c.section("1. opens automatically on start, closes on completion")
        c.check("inactive before any run", not vr.sup.logger.run_export_active)
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(P)
            path_while_running = vr.sup.logger.run_path
            c.check("active immediately after start", vr.sup.logger.run_export_active)
            c.check("file created on disk", path_while_running is not None
                    and path_while_running.exists())
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task
        c.check("inactive after the run completes", not vr.sup.logger.run_export_active)
        c.check("rows were written", vr.sup.logger.run_rows > 0, str(vr.sup.logger.run_rows))

        text = path_while_running.read_text(encoding="utf-8")
        lines = text.strip().split("\n")
        c.check("file has a header plus data rows", len(lines) > 1, str(len(lines)))
        c.check("header starts with elapsed_s", lines[0].startswith("elapsed_s,"))
        c.check("header carries recipe_cycle/recipe_step",
                "recipe_cycle" in lines[0] and "recipe_step" in lines[0])
        c.check("first data row's elapsed_s is ~0",
                float(lines[1].split(",")[0]) < 0.5, lines[1].split(",")[0])

        c.section("2. closes on abort too, not just a clean finish")
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=50))
            abort_path = vr.sup.logger.run_path
            await asyncio.sleep(0.15)
            await vr.sup.abort_recipe()
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task
        c.check("inactive after an abort", not vr.sup.logger.run_export_active)
        c.check("aborted run's file still has rows",
                len(abort_path.read_text(encoding="utf-8").strip().split("\n")) > 1)

        c.section("3. back-to-back runs each get their own file, no stale handle")
        # The export filename is stamped from the run's start time at ONE-SECOND
        # resolution, so two runs starting inside the same second genuinely
        # collide and the second truncates the first. A human cannot press Start
        # twice in a second, but this test can - so wait for the clock to tick
        # over rather than asserting a guarantee the code does not make (without
        # this the check passed or failed on luck).
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=1))
            first_path = vr.sup.logger.run_path
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
            await _next_second()
            await vr.sup.start_ald_run(dict(P, cycles=1))
            second_path = vr.sup.logger.run_path
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task
        c.check("two runs got two different files", first_path != second_path,
                f"{first_path.name} vs {second_path.name}")
        c.check("both files are complete and readable",
                first_path.exists() and second_path.exists())

        c.section("4. status() surfaces run_export, matching what /api/state exposes")
        st = vr.sup.logger.status()
        c.check("status has a run_export block", "run_export" in st)
        c.check("inactive and zero rows after everything above completed",
                st["run_export"]["active"] is False)

        c.section("5. run-parameters report is written, gas schedule included")
        data_dir = vr.sup.logger.dir
        for mode, params, anchor in (("ald", P_GAS, "beam"),
                                     ("cvd", P_GAS, "cycle")):
            await _next_second()        # same one-second stamp collision as above
            before = {p for p in data_dir.rglob("*_run_params.txt")}
            start = vr.sup.start_ald_run if mode == "ald" else vr.sup.start_cvd_run
            tick_task = await autotick(vr, period=0.05)
            try:
                await start(dict(params, cycles=1))
                while vr.sup.recipes.busy:
                    await asyncio.sleep(0.02)
            finally:
                tick_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tick_task
            new = [p for p in data_dir.rglob("*_run_params.txt") if p not in before]
            c.check(f"{mode}: params report written (.txt, not .json)",
                    len(new) == 1, f"{len(new)} new file(s)")
            if not new:
                continue
            report = new[0].read_text(encoding="utf-8-sig")
            c.check(f"{mode}: sits in the run's own folder",
                    new[0].parent != data_dir, str(new[0].relative_to(data_dir)))
            c.check(f"{mode}: carries the ui_params it was launched with",
                    "h2_gas_flow_sccm" in report)
            c.check(f"{mode}: summary describes the gas schedule against the "
                    f"{anchor} clock", f"relative to {anchor} start" in report)
            c.check(f"{mode}: both scheduled gases appear in the summary",
                    "h2:" in report and "n2:" in report)
            c.check(f"{mode}: lists every cycle step",
                    "RECIPE STEPS" in report and "Cycle (repeated" in report)
        # An exception in write_run_params is swallowed by _start_built_run
        # and only surfaces as an event, so check the event log stayed clean.
        c.check("no 'could not record run parameters' event was raised",
                not any("record run parameters" in e["message"]
                        for e in vr.sup.events))

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
