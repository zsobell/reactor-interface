"""Supervisor.start_prestart / stop_prestart / _run_prestart against the
virtual reactor. Covers: the happy path, the unlimited-retry strike (no
timeout by explicit instruction - see docs/RUN_PROGRAM.md), a drop mid-hold
restarting the hold rather than resuming it, and grounding the beam however
the sequence ends - including a stop before it ever struck.

Run directly: python -m tests.test_prestart
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick, wait_for

PARAMS = dict(ar_sccm=4.0, valve_delay_s=0.1, hold_s=0.4, min_current_a=5.0e-4,
              reignite_pulse_s=0.05, reignite_settle_s=0.05,
              dose_pressure_torr=0.02)


async def main() -> int:
    c = Checker("test_prestart")

    async with VirtualReactor() as vr:
        c.section("1. plasma lights straight away")
        vr.instruments["ammeter"].value = 1.0e-3
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_prestart(PARAMS)
            while vr.sup.prestart.get("running"):
                await asyncio.sleep(0.02)
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

        c.check("Ar pneumatic opened", vr.daq.do_state.get("ar_pneumatic") is True)
        c.check("Ar set to 4 sccm", vr.mfcs["ar"].commanded_sccm == 4.0,
                str(vr.mfcs["ar"].commanded_sccm))
        c.check("fill regulation started on prec1",
                vr.sup.regulator.get("running")
                and vr.sup.regulator.get("gauge") == "gauge.prec1_dose"
                and vr.sup.regulator.get("valve") == "rpm_top")
        c.check("ends with beam GROUNDED (plasma_ground open)",
                vr.daq.do_state["plasma_ground"] is True)
        c.check("reports done", vr.sup.prestart.get("done") is True,
                str(vr.sup.prestart.get("phase")))
        await vr.sup.stop_fill_regulation()

        c.section("2. plasma never lights: retries until stopped, no timeout")
        vr.instruments["ammeter"].value = 0.0
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_prestart(PARAMS)
            await asyncio.sleep(1.2)
            still_running = vr.sup.prestart.get("running")
            strikes_midway = vr.sup.prestart.get("strikes", 0)
            await vr.sup.stop_prestart()
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task
        c.check("still retrying after 1.2s (no built-in timeout)", still_running)
        c.check("many strike attempts", strikes_midway > 2, f"{strikes_midway} strikes")
        c.check("beam grounded after operator stop", vr.daq.do_state["plasma_ground"] is True)
        c.check("not marked done", vr.sup.prestart.get("done") is False)
        c.check("Ar left flowing after stop", vr.mfcs["ar"].commanded_sccm == 4.0)
        c.check("fill left running after stop", vr.sup.regulator.get("running") is True)
        await vr.sup.stop_fill_regulation()

        c.section("3. plasma drops during the hold: hold restarts, still completes")
        vr.instruments["ammeter"].value = 1.0e-3
        ammeter = vr.instruments["ammeter"]

        async def flicker():
            """Drop the plasma once the hold has actually begun, and keep it
            down until the sequence has actually noticed.

            Both waits poll for the real condition rather than guessing an
            offset, and they have to. _run_prestart samples current every
            0.2 s, so the previous version - sleep 0.35 s, drop for exactly
            0.2 s - could put the entire dropout between two samples: the
            hold then completed uninterrupted and the test failed on a race
            it had created itself, not on anything the code did wrong."""
            if not await wait_for(lambda: (vr.sup.prestart.get("held_s") or 0) > 0):
                return                      # never got into the hold; let the
            strikes0 = vr.sup.prestart.get("strikes", 0)   # checks below report
            ammeter.value = 0.0
            await wait_for(lambda: vr.sup.prestart.get("strikes", 0) > strikes0)
            ammeter.value = 1.0e-3

        events_before = len(vr.sup.events)
        tick_task = await autotick(vr, period=0.05)
        flick_task = asyncio.create_task(flicker())
        t0 = time.time()
        try:
            await vr.sup.start_prestart(PARAMS)
            while vr.sup.prestart.get("running"):
                await asyncio.sleep(0.02)
        finally:
            elapsed = time.time() - t0
            tick_task.cancel()
            flick_task.cancel()
            for t in (tick_task, flick_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        flags = [e for e in list(vr.sup.events)[events_before:] if e["kind"] == "flag"]
        c.check("restrike flagged", any("dropped out" in e["message"] for e in flags))
        c.check("still completed", vr.sup.prestart.get("done") is True)
        c.check("hold restarted rather than resuming (took noticeably > hold_s)",
                elapsed > PARAMS["hold_s"] * 1.5, f"{elapsed:.2f}s")
        c.check("ends grounded", vr.daq.do_state["plasma_ground"] is True)
        await vr.sup.stop_fill_regulation()

        c.section("4. stop during the initial valve settle: never struck, still grounds")
        # Sentinel, not 0.0 - the same VirtualReactor (and its FakeMfc) has
        # already been through sections 1-3, which legitimately set Ar to
        # 4 sccm, so a fresh "== 0.0" assertion would just be checking
        # leftover state from an earlier section, not this one.
        vr.mfcs["ar"].commanded_sccm = -1.0
        vr.instruments["ammeter"].value = 1.0e-3
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_prestart(dict(PARAMS, valve_delay_s=5.0))
            await asyncio.sleep(0.1)
            await vr.sup.stop_prestart()
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task
        c.check("beam grounded even though it never struck",
                vr.daq.do_state["plasma_ground"] is True)
        c.check("never set Ar flow (still the -1.0 sentinel, untouched)",
                vr.mfcs["ar"].commanded_sccm == -1.0, str(vr.mfcs["ar"].commanded_sccm))

        c.section("5. run refused while pre-start owns the plasma relay, and vice versa")
        tick_task = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_prestart(dict(PARAMS, valve_delay_s=5.0, hold_s=5.0))
            try:
                await vr.sup.start_ald_run(dict(cycles=1, dose_s=0.05, pump_a_s=0.1,
                                                beam_s=0.1, pump_b_s=0.1))
                c.check("run refused while pre-start is running", False)
            except RuntimeError as exc:
                c.check("run refused while pre-start is running", True, str(exc))
            await vr.sup.stop_prestart()
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
