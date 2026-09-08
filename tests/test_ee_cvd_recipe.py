"""EE-CVD recipe (reactor/control/recipe.py: build_cvd_recipe, RecipeRunner)
against the virtual reactor. Covers: continuous beam on/off, reignite on
plasma loss, the cycle-clock gas schedule freezing during a reignite while
the dose valve never freezes, and beam-off on abort.

Goes in through Supervisor.start_cvd_run(params), not RecipeRunner.start()
directly - that matters. An earlier ad hoc version of this test called
recipes.start() directly and every "MFCs zeroed at run end" check passed
for the wrong reason: it bypassed _start_built_run, which is the thing that
actually arms _run_end_cleanup. Going through the same entry point the UI
uses is what makes this test worth trusting.

Run directly: python -m tests.test_ee_cvd_recipe
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

sys.path.insert(0, ".")

from reactor.control.recipe import build_ald_recipe, build_cvd_recipe
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

P = dict(
    cycles=3, dose_s=0.10, pump_a_s=0.40, dose_pressure_torr=0.02,
    min_current_a=5.0e-4, gas_overlap_s=0.05,
    h2_gas_enable=True, h2_gas_order="first", h2_gas_pct=40, h2_gas_flow_sccm=5,
    n2_gas_enable=True, n2_gas_order="second", n2_gas_pct=60, n2_gas_flow_sccm=3,
)


async def run_cvd(vr, params, *, drop_after=None, drop_for=None, abort_after=None):
    """Start an EE-CVD run the same way the UI does (Supervisor.start_cvd_run),
    autoticking telemetry throughout, optionally forcing a plasma dropout or
    an abort at a scheduled wall-clock offset."""
    tick_task = await autotick(vr, period=0.05)
    ammeter = vr.instruments["ammeter"]

    async def dropper():
        await asyncio.sleep(drop_after)
        ammeter.value = 0.0
        await asyncio.sleep(drop_for)
        ammeter.value = 1.0e-3

    jobs = []
    try:
        await vr.sup.start_cvd_run(params)
        if drop_after is not None:
            jobs.append(asyncio.create_task(dropper()))
        if abort_after is not None:
            await asyncio.sleep(abort_after)
            await vr.sup.abort_recipe()
        while vr.sup.recipes.busy:
            await asyncio.sleep(0.02)
    finally:
        tick_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick_task
        for j in jobs:
            j.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await j


async def main() -> int:
    c = Checker("test_ee_cvd_recipe")

    c.section("1. structure (pure functions, no Supervisor needed)")
    r = build_cvd_recipe(P)
    c.check("cycle is dose + pump A only",
            [s.op for s in r.steps] == ["dose", "wait"], str([s.op for s in r.steps]))
    c.check("mode is cvd", r.mode == "cvd")
    c.check("beam_start in setup", "beam_start" in [s.op for s in r.setup])
    c.check("beam_stop in teardown", "beam_stop" in [s.op for s in r.teardown])
    c.check("cycle_seconds = dose + pumpA", abs(r.cycle_seconds() - 0.5) < 1e-9)
    ald = build_ald_recipe(P)
    c.check("ALD still has 4 steps and mode=ald",
            [s.op for s in ald.steps] == ["dose", "wait", "electron_beam", "wait"]
            and ald.mode == "ald")

    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3   # plasma lit throughout

        c.section("2. clean run, plasma always lit")
        writes_before = len(vr.daq.do_writes)
        await run_cvd(vr, P)
        beam_writes = [w for w in vr.daq.do_writes[writes_before:] if w[1] == "plasma_ground"]
        # One strike at setup, plus the end-of-run park: a completed run is
        # deliberately left in beam-ON mode at the operator's request. Abort is
        # unaffected (teardown is skipped) - section 4 still pins grounded.
        c.check("beam struck at setup + parked at end of run (two closed writes)",
                sum(1 for t, k, v in beam_writes if v is False) == 2)
        c.check("completed run parks in beam-ON mode",
                vr.daq.do_state["plasma_ground"] is False)
        c.check("MFCs zeroed at run end",
                vr.mfcs["h2"].commanded_sccm == 0.0 and vr.mfcs["n2"].commanded_sccm == 0.0)
        c.check("fill valve closed at run end", vr.sup.valve_state["rpm_top"] is False)

        c.section("3. plasma drops out mid-run: reignites, beam still ends grounded")
        events_before = len(vr.sup.events)
        writes_before = len(vr.daq.do_writes)
        await run_cvd(vr, dict(P, cycles=6), drop_after=0.6, drop_for=0.8)
        flags = [e for e in list(vr.sup.events)[events_before:] if e["kind"] == "flag"]
        c.check("reignite flagged", any("reignit" in e["message"] for e in flags))
        reignite_pulses = [w for w in vr.daq.do_writes[writes_before:] if w[1] == "plasma_ground"]
        c.check("reignite pulsed the switch more than the single setup/teardown pair",
                len(reignite_pulses) > 2, f"{len(reignite_pulses)} plasma_ground writes")
        c.check("beam still parks in beam-ON mode after a reignite",
                vr.daq.do_state["plasma_ground"] is False)

        c.section("4. abort mid-run: beam grounded, everything cleaned up")
        vr.instruments["ammeter"].value = 1.0e-3
        await run_cvd(vr, dict(P, cycles=50), abort_after=0.4)
        c.check("beam grounded after abort", vr.daq.do_state["plasma_ground"] is True)
        c.check("state is idle after abort", vr.sup.recipes.progress.state == "idle",
                vr.sup.recipes.progress.state)
        c.check("watchdog task stopped", vr.sup.recipes._beam_task is None)
        c.check("MFCs zeroed after abort",
                vr.mfcs["h2"].commanded_sccm == 0.0 and vr.mfcs["n2"].commanded_sccm == 0.0)

        c.section("5. no gas scheduled: no MFC writes at all, beam still grounded")
        NP = dict(P, cycles=2, h2_gas_enable=False, n2_gas_enable=False)
        vr.instruments["ammeter"].value = 1.0e-3
        events_before = len(vr.sup.events)
        await run_cvd(vr, NP)
        # finish_run() unconditionally zeroes every MFC regardless of gas
        # scheduling, so the meaningful check is that no MFC *event* fired at
        # all except that one end-of-run zeroing - not the final value, which
        # would be 0.0 either way and couldn't catch a wrongly-scheduled gas.
        mfc_events = [e["message"] for e in list(vr.sup.events)[events_before:]
                     if e["kind"] == "mfc"]
        c.check("no scheduling writes beyond end-of-run zeroing",
                all("-> 0.00" in m for m in mfc_events), str(mfc_events))
        c.check("beam parked in beam-ON mode", vr.daq.do_state["plasma_ground"] is False)

        c.section("6. realistic timings: gas windows land where the UI predicts")
        # dose 0.05 + pump A 4.0 = 4.05s cycle, overlap 0.5, h2 first 40%,
        # n2 second 60% -> handoff 1.62, second on 1.12, second off 4.05,
        # h2 re-arms at 3.55 (see docs/RUN_PROGRAM.md's gas-scheduling section)
        RP = dict(P, cycles=2, dose_s=0.05, pump_a_s=4.0, gas_overlap_s=0.5,
                  h2_gas_pct=40, n2_gas_pct=60)
        vr.instruments["ammeter"].value = 1.0e-3
        runner = vr.sup.recipes
        marks = []
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_cvd_run(RP)
            while runner.busy:
                marks.append((round(runner._cycle_clock, 2),
                              vr.mfcs["h2"].commanded_sccm, vr.mfcs["n2"].commanded_sccm))
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

        def edges(idx):
            out, prev = [], None
            for ic, h2, n2 in marks:
                v = (h2, n2)[idx]
                if v != prev:
                    out.append((ic, v))
                    prev = v
            return out

        off_h2 = [ic for ic, v in edges(0) if v == 0.0 and ic > 1.0]
        on_n2 = [ic for ic, v in edges(1) if v == 3.0]
        on_h2 = [ic for ic, v in edges(0) if v == 5.0 and ic > 2.0]
        c.check("h2 turns off near the 1.62s handoff",
                any(abs(ic - 1.62) < 0.35 for ic in off_h2), str(off_h2))
        c.check("n2 turns on near 1.12s (handoff - overlap)",
                any(abs(ic - 1.12) < 0.35 for ic in on_n2), str(on_n2))
        c.check("h2 re-arms near 3.55s (second_off - overlap)",
                any(abs(ic - 3.55) < 0.35 for ic in on_h2), str(on_h2))

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
