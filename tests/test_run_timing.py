"""Run-clock accuracy: does a cycle take as long as the recipe says, and does
the operator's "est. remaining" tick down like a clock?

Written after a 150-cycle EE-ALD run (Mo-015, 2026-08-21) came out 124 s longer
than the parameters typed into the UI: 16.41 s per cycle against a nominal
16.00. Two causes, both fixed in control/recipe.py and both pinned here:

  * the beam step slept `reignite_settle_s` before its exposure clock started,
    so every beam ran that much longer than asked;
  * its current-check tick was a fixed 0.2 s, so the last tick of a step
    overshot by up to a full tick.

The budget below is per cycle and deliberately tight. Timing on Windows has
1-15 ms of jitter and the virtual reactor adds its own, so this asserts
"accurate to a tenth of a second", which is the requirement, not "exact".

Run directly: python -m tests.test_run_timing
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

#: Per-cycle overrun the run clock is allowed. The old code spent 0.30 s of
#: this budget on the beam step alone before the first valve moved.
TOLERANCE_S = 0.10

P = dict(
    cycles=4, dose_s=0.10, pump_a_s=0.20, beam_s=0.60, pump_b_s=0.10,
    dose_pressure_torr=0.02, min_current_a=5.0e-4, gas_overlap_s=0.0,
    reignite_settle_s=0.20,
)
NOMINAL_CYCLE_S = P["dose_s"] + P["pump_a_s"] + P["beam_s"] + P["pump_b_s"]


async def main() -> int:
    c = Checker("test_run_timing")

    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3        # lit throughout
        tick_task = await autotick(vr, period=0.05)

        c.section("1. a cycle takes as long as the recipe says")
        cycle_marks: list[tuple[int, float]] = []
        etas: list[tuple[float, float]] = []            # (wall, run_remaining_s)
        seen_cycle = 0
        try:
            await vr.sup.start_ald_run(P)
            t_start = time.monotonic()
            t_cycles_end = None
            while vr.sup.recipes.busy:
                pr = vr.sup.recipes.progress
                if pr.phase == "cycling" and pr.cycle != seen_cycle:
                    seen_cycle = pr.cycle
                    cycle_marks.append((pr.cycle, time.monotonic()))
                # Only the cycling phase is what the countdown covers; the
                # teardown (Ar close delay) runs after it and is not counted.
                if pr.phase == "teardown" and t_cycles_end is None:
                    t_cycles_end = time.monotonic()
                rem = vr.sup.recipes.run_remaining_s()
                if rem is not None and pr.phase in ("", "setup", "cycling"):
                    etas.append((time.monotonic(), rem))
                await asyncio.sleep(0.01)
            t_cycles_end = t_cycles_end or time.monotonic()
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

        lengths = [cycle_marks[i + 1][1] - cycle_marks[i][1]
                   for i in range(len(cycle_marks) - 1)]
        c.check("every cycle was observed", len(cycle_marks) == P["cycles"],
                f"{len(cycle_marks)}/{P['cycles']}")
        if lengths:
            worst = max(abs(x - NOMINAL_CYCLE_S) for x in lengths)
            mean = sum(lengths) / len(lengths)
            c.check(f"cycle length within {TOLERANCE_S*1000:.0f} ms of nominal",
                    worst <= TOLERANCE_S,
                    f"nominal {NOMINAL_CYCLE_S:.3f}s, mean {mean:.3f}s, "
                    f"worst error {worst*1000:.0f} ms")
            c.check("no systematic overrun (mean is not biased long)",
                    mean - NOMINAL_CYCLE_S <= TOLERANCE_S,
                    f"mean drift {(mean - NOMINAL_CYCLE_S)*1000:+.0f} ms/cycle")

        # The whole cycling phase, which is what the countdown promises.
        if cycle_marks:
            measured = t_cycles_end - cycle_marks[0][1]      # all cycles
            nominal_total = P["cycles"] * NOMINAL_CYCLE_S
            # The teardown runs after the last cycle, so only compare up to the
            # end of cycling: allow the per-cycle budget times the cycle count.
            c.check("total cycling time tracks cycles x cycle length",
                    abs(measured - nominal_total) <= TOLERANCE_S * P["cycles"],
                    f"nominal {nominal_total:.2f}s, measured {measured:.2f}s, "
                    f"error {(measured - nominal_total)*1000:+.0f} ms over "
                    f"{P['cycles']} cycles")

        c.section("2. est. remaining behaves like a clock")
        c.check("countdown was published while running", len(etas) > 10,
                f"{len(etas)} samples")
        if etas:
            c.check("starts at cycles x cycle length",
                    abs(etas[0][1] - P["cycles"] * NOMINAL_CYCLE_S) <= TOLERANCE_S,
                    f"{etas[0][1]:.3f}s vs {P['cycles'] * NOMINAL_CYCLE_S:.3f}s")
            c.check("never counts upward",
                    all(etas[i + 1][1] <= etas[i][1] + 1e-6
                        for i in range(len(etas) - 1)),
                    "monotonically non-increasing")
            # Ticks down in real time: over the run, the countdown should have
            # fallen by about as much wall time as elapsed.
            d_wall = etas[-1][0] - etas[0][0]
            d_eta = etas[0][1] - etas[-1][1]
            c.check("falls one second per second",
                    abs(d_eta - d_wall) <= TOLERANCE_S * P["cycles"],
                    f"wall {d_wall:.2f}s vs countdown {d_eta:.2f}s")
            c.check("reaches zero by the end of cycling", etas[-1][1] <= TOLERANCE_S,
                    f"ended at {etas[-1][1]:.3f}s")

    c.section("3. the countdown freezes while the plasma is out")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=2, beam_s=1.0))
            # Wait for the beam step, then kill the plasma and watch the clock.
            deadline = time.monotonic() + 5.0
            while (vr.sup.recipes.progress.step_op != "electron_beam"
                   and time.monotonic() < deadline):
                await asyncio.sleep(0.01)
            vr.instruments["ammeter"].value = 0.0        # plasma out
            await asyncio.sleep(0.35)                    # let it be noticed
            frozen_a = vr.sup.recipes.run_remaining_s()
            await asyncio.sleep(0.40)
            frozen_b = vr.sup.recipes.run_remaining_s()
            vr.instruments["ammeter"].value = 1.0e-3     # relight
            # A reignite attempt is pulse + settle (0.30 s here) and the clock
            # only resumes on the tick after it; wait past a whole attempt.
            await asyncio.sleep(0.80)
            moving = vr.sup.recipes.run_remaining_s()
            c.check("countdown holds still while reigniting",
                    frozen_a is not None and frozen_b is not None
                    and abs(frozen_a - frozen_b) <= 0.05,
                    f"{frozen_a:.3f}s -> {frozen_b:.3f}s over 0.40s of dead plasma")
            c.check("countdown resumes once current is back",
                    moving is not None and moving < frozen_b - 0.05,
                    f"{frozen_b:.3f}s -> {moving:.3f}s")
            await vr.sup.abort_recipe()
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
