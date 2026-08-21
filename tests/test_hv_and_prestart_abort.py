"""The two actuation changes of 2026-08-21, against the virtual reactor:

  1. HV is commanded OFF whenever a run ends - completed, aborted, or crashed.
     This is the only command the program ever sends the plasma supply, and it
     must not disturb the voltage/current levels Zach set on the front panel.
  2. `abort_prestart` undoes a pre-start in one call, and stays usable AFTER the
     plasma has struck (which `stop_prestart` does not - it ends the sequence
     and deliberately leaves the tool primed).

The beam relay ends DE-ENERGISED after an abort. That is the "beam on" sense,
and it is correct: the relay box runs off a 9 V battery that drains only while
the relay is energised, so at rest it belongs off (docs/HARDWARE.md).

Run directly: python -m tests.test_hv_and_prestart_abort
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick, wait_for

P = dict(
    cycles=2, dose_s=0.05, pump_a_s=0.10, beam_s=0.30, pump_b_s=0.05,
    dose_pressure_torr=0.02, min_current_a=5.0e-4, ar_close_delay_s=0.1,
)
PRE = dict(
    ar_sccm=4.0, valve_delay_s=0.05, hold_s=0.2, dose_pressure_torr=0.02,
    min_current_a=5.0e-4, reignite_pulse_s=0.05, reignite_settle_s=0.05,
)


def last_state(vr, valve: str):
    """Last commanded state of `valve`, from the DAQ write log."""
    for w in reversed(vr.daq.do_writes):
        if w[1] == valve:
            return w[2]
    return None


async def main() -> int:
    c = Checker("test_hv_and_prestart_abort")

    # ------------------------------------------------------------------ 1
    c.section("1. a completed run commands HV off, without moving the levels")
    async with VirtualReactor() as vr:
        hv = vr.supplies["hv"]
        hv.voltage, hv.current, hv.hv_on = 320.0, 200.0, True
        vr.instruments["ammeter"].value = 1.0e-3
        tick = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(P)
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick
        c.check("HV off was commanded exactly once", hv.hv_off_calls == 1,
                f"{hv.hv_off_calls} call(s)")
        c.check("HV reads off afterwards", hv.hv_on is False)
        c.check("front-panel voltage program untouched", hv.voltage == 320.0,
                f"{hv.voltage} V")
        c.check("front-panel current program untouched", hv.current == 200.0,
                f"{hv.current} mA")

    # ------------------------------------------------------------------ 2
    c.section("2. an aborted run commands HV off too")
    async with VirtualReactor() as vr:
        hv = vr.supplies["hv"]
        hv.voltage, hv.current, hv.hv_on = 900.0, 150.0, True
        vr.instruments["ammeter"].value = 1.0e-3
        tick = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(P, cycles=50))
            await wait_for(lambda: vr.sup.recipes.progress.phase == "cycling", 5.0)
            await vr.sup.abort_recipe()
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick
        c.check("HV off was commanded on abort", hv.hv_off_calls == 1,
                f"{hv.hv_off_calls} call(s)")
        c.check("levels still untouched after an abort",
                (hv.voltage, hv.current) == (900.0, 150.0),
                f"{hv.voltage} V / {hv.current} mA")

    # ------------------------------------------------------------------ 3
    c.section("3. abort_prestart undoes a pre-start that already struck")
    async with VirtualReactor() as vr:
        hv = vr.supplies["hv"]
        hv.hv_on = True
        vr.instruments["ammeter"].value = 1.0e-3        # strikes immediately
        tick = await autotick(vr, period=0.02)
        try:
            await vr.sup.start_prestart(PRE)
            struck = await wait_for(lambda: vr.sup.prestart.get("done"), 5.0)
            c.check("pre-start reached its struck-and-held end state", struck)
            c.check("pre-start finished, so Stop is no longer available",
                    vr.sup.prestart.get("running") is False)
            # The primed state Stop leaves behind, and Abort has to undo.
            c.check("Ar isolation valve is open before the abort",
                    last_state(vr, "ar_pneumatic") is True)
            c.check("fill regulation is running before the abort",
                    vr.sup.regulator.get("running") is True)
            c.check("beam relay is energised (grounded) before the abort",
                    last_state(vr, "plasma_ground") is True)

            await vr.sup.abort_prestart()

            c.check("Ar flow set to zero", vr.mfcs["ar"].commanded_sccm == 0.0,
                    f"{vr.mfcs['ar'].commanded_sccm} sccm")
            c.check("Ar isolation valve closed",
                    last_state(vr, "ar_pneumatic") is False)
            c.check("fill regulation stopped",
                    vr.sup.regulator.get("running") is False)
            c.check("fill valve closed", last_state(vr, "rpm_top") is False)
            c.check("beam relay de-energised (9 V battery at rest)",
                    last_state(vr, "plasma_ground") is False)
            c.check("HV commanded off", hv.hv_off_calls == 1 and hv.hv_on is False,
                    f"{hv.hv_off_calls} call(s)")
            c.check("pre-start no longer reports itself primed",
                    vr.sup.prestart.get("done") is False
                    and vr.sup.prestart.get("running") is False)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    # ------------------------------------------------------------------ 4
    c.section("4. abort_prestart also works mid-strike (plasma never lights)")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 0.0           # never strikes
        tick = await autotick(vr, period=0.02)
        try:
            await vr.sup.start_prestart(PRE)
            await wait_for(lambda: vr.sup.prestart.get("strikes", 0) >= 2, 5.0)
            c.check("still retrying, as instructed (no timeout)",
                    vr.sup.prestart.get("running") is True,
                    f"{vr.sup.prestart.get('strikes')} strikes")
            await vr.sup.abort_prestart()
            c.check("sequence stopped", vr.sup.prestart.get("running") is False)
            c.check("Ar isolation valve closed",
                    last_state(vr, "ar_pneumatic") is False)
            c.check("fill regulation stopped",
                    vr.sup.regulator.get("running") is False)
            c.check("beam relay de-energised",
                    last_state(vr, "plasma_ground") is False)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
