"""Pause stops the ACTION, not just the clock, and resume picks up where it left.

Zach, 2026-09-01: "pause doesn't really work. in the purge step the timer keeps
moving, in the e-beam step the beam stays on. God knows what happens if I pause
in the dose step... It needs to stop the current action (e-beam or dose) and
stop the timer. The step should resume with the correct timing on resume."

All three were the same defect: `_pause` was only awaited at a step BOUNDARY and
inside the beam step's tick loop. `_sleep` - which every wait and every dose ran
on - did not look at it at all, so a pump counted straight through a pause; and
the beam step froze its exposure budget while leaving the plasma on the sample.
A paused dose was the worst of them: the valve stayed open for as long as the
operator was away.

What pause does NOT do is also pinned here: the sample bias and the scheduled
gases are left alone, which is the operator's own call (asked and answered the
same day). Nothing else may quietly join in.

Run directly: python -m tests.test_pause
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick, wait_for

#: Slack on a resumed step's remaining time. The clock is exact; the pause is
#: driven from a test task, so a tick of scheduling either side is fair.
TOL_S = 0.15

P = dict(
    cycles=2, dose_s=0.40, pump_a_s=1.20, beam_s=1.00, pump_b_s=0.20,
    dose_pressure_torr=0.02, min_current_a=5.0e-4, gas_overlap_s=0.0,
    reignite_settle_s=0.10, sample_bias_v=12.0,
    mfc1_gas_enable=True, mfc1_gas_order="first", mfc1_gas_pct=100, mfc1_gas_flow_sccm=4.0,
)


def valve_writes(vr, name):
    return [(t, v) for t, k, v in vr.daq.do_writes if k == name]


async def main() -> int:
    c = Checker("test_pause")

    # ---------------------------------------------------------------- 1. wait
    c.section("1. a pause stops the clock in a wait step")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3
        tick = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=1, dose_s=0.05,
                                            pump_a_s=2.0, beam_s=0.10,
                                            pump_b_s=0.05))
            r = vr.sup.recipes
            ok = await wait_for(lambda: r.progress.step_op == "wait"
                                and (r.progress.as_dict()["step_remaining_s"] or 0) > 0.8,
                                timeout=8.0)
            c.check("reached the pump step", ok, r.progress.step_desc or "")
            left = r.progress.as_dict()["step_remaining_s"]
            r.pause()
            await asyncio.sleep(0.8)
            held = r.progress.as_dict()["step_remaining_s"]
            # THE BUG: this used to keep counting down through the pause.
            c.check("step remaining held still across a 0.8s pause",
                    abs(held - left) < TOL_S, f"{left:.2f}s -> {held:.2f}s")
            r.resume()
            await asyncio.sleep(0.4)
            after = r.progress.as_dict()["step_remaining_s"]
            c.check("and resumes counting from where it stopped",
                    after < held - 0.2, f"{held:.2f}s -> {after:.2f}s")
            await r.abort()
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    # ---------------------------------------------------------------- 2. dose
    c.section("2. a pause closes the dose valve and reopens it on resume")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3
        tick = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=1, dose_s=1.5,
                                            pump_a_s=0.1, beam_s=0.1, pump_b_s=0.05))
            r = vr.sup.recipes
            ok = await wait_for(lambda: r.progress.step_op == "dose", timeout=8.0)
            c.check("reached the dose step", ok)
            await asyncio.sleep(0.2)
            c.check("dose valve is open", vr.daq.do_state.get("prec1") is True,
                    str(vr.daq.do_state.get("prec1")))
            r.pause()
            await wait_for(lambda: vr.daq.do_state.get("prec1") is False, timeout=2.0)
            # THE BUG: the valve used to stay open for the whole pause, dumping
            # precursor into the chamber.
            c.check("pausing CLOSED the dose valve",
                    vr.daq.do_state.get("prec1") is False,
                    str(vr.daq.do_state.get("prec1")))
            await asyncio.sleep(0.5)
            c.check("and it stays closed while paused",
                    vr.daq.do_state.get("prec1") is False)
            r.resume()
            await wait_for(lambda: vr.daq.do_state.get("prec1") is True, timeout=2.0)
            c.check("resuming reopened it", vr.daq.do_state.get("prec1") is True)
            await r.abort()
            c.check("and the dose still ends closed",
                    vr.daq.do_state.get("prec1") is False)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    # ---------------------------------------------------------------- 3. beam
    c.section("3. a pause grounds the beam, and the exposure survives it")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3
        tick = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=1, dose_s=0.05,
                                            pump_a_s=0.05, beam_s=2.0,
                                            pump_b_s=0.05))
            r = vr.sup.recipes
            ok = await wait_for(lambda: r.progress.step_op == "electron_beam"
                                and r.progress.beam is not None, timeout=8.0)
            c.check("reached the beam step", ok)
            await asyncio.sleep(0.4)
            # plasma_ground OFF (False) is beam ON.
            c.check("beam is on (plasma ground de-energised)",
                    vr.daq.do_state.get("plasma_ground") is False)
            r.pause()
            await wait_for(lambda: vr.daq.do_state.get("plasma_ground") is True,
                           timeout=2.0)
            # THE BUG: the beam used to keep running on the sample all pause.
            c.check("pausing GROUNDED the beam",
                    vr.daq.do_state.get("plasma_ground") is True,
                    str(vr.daq.do_state.get("plasma_ground")))
            # Sampled twice INSIDE the pause. `progress.beam` publishes the
            # budget as it was at the top of the tick, so it lags a tick behind
            # the value the loop holds - comparing across the pause boundary
            # would measure that lag rather than the freeze.
            await asyncio.sleep(0.1)
            held = (r.progress.beam or {}).get("remaining")
            await asyncio.sleep(0.7)
            still = (r.progress.beam or {}).get("remaining")
            c.check("exposure budget held still while paused",
                    held is not None and still is not None and abs(still - held) < 1e-6,
                    f"{held:.3f}s -> {still:.3f}s over 0.7s")
            # Operator's explicit call: pause touches the beam and the dose,
            # nothing else. The bias supply stays energised, gas keeps flowing.
            bias = vr.sup.supplies["stage_bias"]
            c.check("sample bias was NOT switched off (asked and answered)",
                    bias.output_on is True, f"output_on={bias.output_on}")
            c.check("scheduled H2 was NOT zeroed",
                    (vr.mfcs["mfc1"].commanded_sccm or 0) > 0,
                    f"{vr.mfcs['mfc1'].commanded_sccm} sccm")
            r.resume()
            await wait_for(lambda: vr.daq.do_state.get("plasma_ground") is False,
                           timeout=2.0)
            c.check("resuming re-struck the beam",
                    vr.daq.do_state.get("plasma_ground") is False)
            await wait_for(lambda: (r.progress.beam or {}).get("remaining", still) < still - 0.25,
                           timeout=1.0)
            after = (r.progress.beam or {}).get("remaining")
            c.check("and the exposure resumed from where it stopped",
                    after is not None and after < still - 0.2,
                    f"{still:.2f}s -> {after:.2f}s")
            # A resume must not be read as a dead plasma by the next tick.
            reignites = [e for e in vr.sup.events
                         if "reigniting" in (e.get("message") or "")]
            c.check("the re-strike was not reported as a reignite",
                    not reignites, str(len(reignites)))
            await r.abort()
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
