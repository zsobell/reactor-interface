"""EE-ALD recipe against the virtual reactor: the pulsed-beam mode, its gas
scheduling anchored to the exposure clock, and the reignite hint math the UI
mirrors. EE-CVD's equivalents live in test_ee_cvd_recipe.py.

Run directly: python -m tests.test_ee_ald_recipe
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

P = dict(
    cycles=2, dose_s=0.05, pump_a_s=0.30, beam_s=1.0, pump_b_s=0.1,
    dose_pressure_torr=0.02, min_current_a=5.0e-4, gas_overlap_s=0.2,
    h2_gas_enable=True, h2_gas_order="first", h2_gas_pct=40, h2_gas_flow_sccm=5,
    n2_gas_enable=True, n2_gas_order="second", n2_gas_pct=60, n2_gas_flow_sccm=3,
)


async def main() -> int:
    c = Checker("test_ee_ald_recipe")

    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3   # plasma lit throughout

        c.section("1. clean run: beam pulses per cycle, gas leads/handoff off the overlap")
        # beam 1.0s, overlap 0.2, h2 first 40%, n2 second 60%:
        #   h2 on 0.2s before the beam step, off at exposure 0.40
        #   n2 on at 0.40-0.20=0.20, off at 1.00
        tick_task = await autotick(vr, period=0.05)
        beam_writes_before = len(vr.daq.do_writes)
        try:
            await vr.sup.start_ald_run(P)
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

        beam_writes = [w for w in vr.daq.do_writes[beam_writes_before:]
                       if w[1] == "plasma_ground"]
        beam_on_writes = [w for w in beam_writes if w[2] is False]
        c.check("beam turned on once per cycle (2 cycles)", len(beam_on_writes) == 2,
                str(len(beam_on_writes)))
        c.check("beam ends grounded", vr.daq.do_state["plasma_ground"] is True)
        c.check("MFCs zeroed at run end",
                vr.mfcs["h2"].commanded_sccm == 0.0 and vr.mfcs["n2"].commanded_sccm == 0.0)

        c.section("2. plasma drops mid-exposure: reignites, run still completes")
        # _electron_beam's first current check happens ~0.2s AFTER the beam
        # step starts (it sleeps before it polls), not the instant the step
        # begins - so timing the drop off a fixed wall-clock offset from run
        # start is fragile (an earlier version of this test raced it and the
        # drop missed every poll). Wait for progress.beam.lit to actually
        # report True first, so the drop is guaranteed to land on a step
        # that's already polling. beam_s is generous here purely to give the
        # drop/reignite room - the exposure clock pauses during the outage,
        # so a longer budget doesn't mean a longer *drop* is required.
        ammeter = vr.instruments["ammeter"]

        async def wait_lit_then_drop():
            beam = vr.sup.recipes.progress.beam
            while not (beam and beam.get("lit")):
                await asyncio.sleep(0.02)
                beam = vr.sup.recipes.progress.beam
            ammeter.value = 0.0
            await asyncio.sleep(0.5)
            ammeter.value = 1.0e-3

        events_before = len(vr.sup.events)
        tick_task = await autotick(vr, period=0.05)
        drop_task = asyncio.create_task(wait_lit_then_drop())
        try:
            await vr.sup.start_ald_run(dict(P, cycles=1, beam_s=3.0))
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            drop_task.cancel()
            for t in (tick_task, drop_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        flags = [e for e in list(vr.sup.events)[events_before:] if e["kind"] == "flag"]
        c.check("reignite flagged", any("reignit" in e["message"] for e in flags))
        c.check("run still completed (not stuck/errored)",
                vr.sup.recipes.progress.state in ("done", "idle"),
                vr.sup.recipes.progress.state)
        c.check("beam ends grounded after a reignite", vr.daq.do_state["plasma_ground"] is True)

        c.section("3. abort mid-dose: dose valve still closes, beam grounded")
        vr.instruments["ammeter"].value = 1.0e-3
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=50, dose_s=2.0))
            await asyncio.sleep(0.3)      # land inside the (now long) dose step
            await vr.sup.abort_recipe()
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task
        c.check("dose valve closed after abort", vr.daq.do_state["prec1"] is False)
        c.check("beam grounded after abort", vr.daq.do_state["plasma_ground"] is True)
        c.check("state idle after abort", vr.sup.recipes.progress.state == "idle")

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
