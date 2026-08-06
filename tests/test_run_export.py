"""Automatic server-side run export against the virtual reactor: opens the
instant Supervisor.start_recipe fires (via start_ald_run here - any recipe
entry point goes through the same start_recipe), samples every tick, closes
in finish_run() however the run ends. Also confirms it survives an abort
and that a stale handle can't linger across back-to-back runs.

Run directly: python -m tests.test_run_export
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
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=1))
            first_path = vr.sup.logger.run_path
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
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

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
