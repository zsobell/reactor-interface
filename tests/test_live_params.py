"""Run parameters can be changed while the run is running, and the report says so.

Zach, 2026-09-01: "I need to be able to change parameters mid run. the N2 MFC
was set to 0.6 for -017, and that was below the actuation threshold. I couldn't
increase it, and there was no warning it was out of bounds either."

Two separate failures in that sentence, both covered here:

  * a recipe was a snapshot taken at Start. The Run tab's fields stayed
    editable during a run and simply went nowhere, so a flow that was wrong
    stayed wrong for the whole run.
  * nothing compared a commanded value with its measurement. The fill pressure
    had that check; nothing else did. The operator's own call on what the check
    should be: "just a warning that the setpoint doesn't match the measured
    flow value during a run. This applies already to the precursor pressure."

Neither is a limit. A value is written as typed and a disagreement is reported,
never refused - see docs/CONTROL_MODEL.md.

Run directly: python -m tests.test_live_params
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.datalog import format_run_params
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick, wait_for

P = dict(
    cycles=6, dose_s=0.05, pump_a_s=0.30, beam_s=0.30, pump_b_s=0.10,
    dose_pressure_torr=0.02, min_current_a=5.0e-4, gas_overlap_s=0.0,
    fill_pulse_on_s=0.10, fill_pulse_off_s=0.30, tolerance_frac=0.20,
    reignite_pulse_s=0.05, reignite_settle_s=0.05, ar_close_delay_s=0.05,
    mfc2_gas_enable=True, mfc2_gas_order="first", mfc2_gas_pct=100, mfc2_gas_flow_sccm=0.6,
)


async def main() -> int:
    c = Checker("test_live_params")

    async with VirtualReactor() as vr:
        sup = vr.sup
        vr.instruments["ammeter"].value = 1.0e-3
        tick = await autotick(vr, period=0.05)
        try:
            await sup.start_ald_run(dict(P))
            r = sup.recipes
            await wait_for(lambda: r.progress.cycle >= 1, timeout=8.0)

            c.section("1. the N2 case: raise a gas flow mid-run")
            c.check("run started at 0.6 sccm",
                    r.recipe.gas_schedules[0].flow_sccm == 0.6,
                    str(r.recipe.gas_schedules[0].flow_sccm))
            # Wait until the gas is actually flowing, so the re-issue path runs.
            await wait_for(lambda: (vr.mfcs["mfc2"].commanded_sccm or 0) > 0, timeout=8.0)
            got = await sup.update_run_params(dict(P, mfc2_gas_flow_sccm=8.0))
            c.check("the change was accepted",
                    got.get("changed") == ["mfc2_gas_flow_sccm"], str(got))
            c.check("the RUNNING recipe now carries it",
                    r.recipe.gas_schedules[0].flow_sccm == 8.0,
                    str(r.recipe.gas_schedules[0].flow_sccm))
            # THE POINT: a gas already flowing follows immediately, not at the
            # next window - the operator is watching a flow that is wrong now.
            ok = await wait_for(
                lambda: abs((vr.mfcs["mfc2"].commanded_sccm or 0) - 8.0) < 1e-6,
                timeout=3.0)
            c.check("and the MFC was re-commanded straight away", ok,
                    f"{vr.mfcs['mfc2'].commanded_sccm} sccm")

            c.section("1b. switching a gas OFF mid-run actually stops it")
            # Zach, 2026-09-09: he unticked a gas and it kept being commanded to
            # its old flow every cycle, with 0 in every field. An unticked gas
            # simply vanishes from the freshly built recipe, and apply_params
            # only ever looked at gases present in BOTH - so the live schedule
            # object survived untouched and kept cycling.
            off = await sup.update_run_params(
                dict(P, mfc2_gas_flow_sccm=8.0, mfc2_gas_enable=False))
            c.check("the run no longer schedules it",
                    [g.mfc for g in r.recipe.gas_schedules] == [],
                    str([g.mfc for g in r.recipe.gas_schedules]))
            c.check("the runner's own list agrees", r._gas_schedules == [],
                    str(r._gas_schedules))
            zeroed = await wait_for(
                lambda: (vr.mfcs["mfc2"].commanded_sccm or 0) == 0, timeout=3.0)
            c.check("and it was shut off at once, not left flowing", zeroed,
                    f"{vr.mfcs['mfc2'].commanded_sccm} sccm")
            # ...and stays off: the old bug re-commanded it at the next window.
            await asyncio.sleep(0.6)
            c.check("and stays off through the following cycles",
                    (vr.mfcs["mfc2"].commanded_sccm or 0) == 0,
                    f"{vr.mfcs['mfc2'].commanded_sccm} sccm")
            c.check("switching it back on is picked up too",
                    len((await sup.update_run_params(
                        dict(P, mfc2_gas_flow_sccm=8.0)))["changed"]) >= 0
                    and [g.mfc for g in r.recipe.gas_schedules] == ["mfc2"],
                    str([g.mfc for g in r.recipe.gas_schedules]))

            c.section("2. timings and the countdown follow")
            before = r.run_total_s()
            await sup.update_run_params(dict(P, mfc2_gas_flow_sccm=8.0, pump_a_s=1.30))
            c.check("cycle length was re-derived",
                    abs(r._cycle_len - (0.05 + 1.30 + 0.30 + 0.10)) < 1e-6,
                    f"{r._cycle_len:.2f}s")
            c.check("est. remaining is based on the new length",
                    r.run_total_s() > before, f"{before:.1f}s -> {r.run_total_s():.1f}s")

            c.section("3. the fill regulator is retuned, not restarted")
            c.check("regulator still running", sup.regulator.get("running") is True)
            await sup.update_run_params(
                dict(P, mfc2_gas_flow_sccm=8.0, pump_a_s=1.30, dose_pressure_torr=0.05))
            c.check("new target reached the live regulator",
                    abs(sup.regulator["target_torr"] - 0.05) < 1e-9,
                    str(sup.regulator["target_torr"]))
            c.check("and it was never stopped and restarted",
                    sup.regulator.get("running") is True)

            c.section("4. lowering the cycle count ends the run early")
            # Operator's call when asked: finish the cycle in progress, then
            # stop with the full teardown - never a half cycle in the data.
            now = r.progress.cycle
            await sup.update_run_params(
                dict(P, mfc2_gas_flow_sccm=8.0, pump_a_s=1.30,
                     dose_pressure_torr=0.05, cycles=now))
            ended = await wait_for(lambda: not r.busy, timeout=25.0)
            c.check("the run ended", ended, r.progress.state)
            c.check("after finishing the cycle it was on",
                    r.progress.cycle == now, f"cycle {r.progress.cycle} of {now}")
            c.check("state is done, not aborted", r.progress.state == "done",
                    r.progress.state)

            c.section("5. every change is in the parameters report")
            changes = sup.runs.changes
            keys = [ch["key"] for ch in changes]
            c.check("every edit recorded in order (both gas switches too)",
                    keys == ["mfc2_gas_flow_sccm", "mfc2_gas_enable",
                             "mfc2_gas_enable", "pump_a_s",
                             "dose_pressure_torr", "cycles"], str(keys))
            c.check("each carries elapsed seconds and a cycle number",
                    all(ch["elapsed_s"] >= 0 and ch["cycle"] >= 1 for ch in changes),
                    str([(round(ch["elapsed_s"], 1), ch["cycle"]) for ch in changes]))
            text = format_run_params(sup.runs.params, r.recipe, "Mo-017",
                                     changes=changes)
            c.check("the report has a CHANGES section",
                    "CHANGES DURING THE RUN" in text)
            c.check("naming the parameter, the old value and the new one",
                    "mfc2_gas_flow_sccm" in text and "0.6" in text and "8" in text)
            c.check("and it still reports the run it ended up being",
                    "PARAMETERS SET IN THE UI" in text)
        finally:
            with contextlib.suppress(Exception):
                await sup.recipes.abort()
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    # ------------------------------------------------------------- warnings
    c.section("6. a setpoint its measurement disagrees with is flagged")
    async with VirtualReactor() as vr:
        sup = vr.sup
        tick = await autotick(vr, period=0.05)
        try:
            sup.flag_tolerance = 0.20
            await sup.set_valve("ar_pneumatic", True, reason="test")
            await sup.set_mfc_setpoint("mfc2", 5.0)
            vr.mfcs["mfc2"].flow_sccm = 5.0
            await vr.tick()
            # Inside the settle window nothing is judged - an MFC on its way to
            # a new value is not a mismatch.
            c.check("no flag while settling", sup.setpoint_flags() == [],
                    str(sup.setpoint_flags()))
            sup._setpoint_changed.clear()
            await vr.tick()
            c.check("no flag when the flow matches", sup.setpoint_flags() == [],
                    str(sup.setpoint_flags()))

            # The Mo-017 shape: commanded a flow the valve never delivers.
            vr.mfcs["mfc2"].flow_sccm = 0.0
            await vr.tick()
            flags = sup.setpoint_flags()
            c.check("flagged when the flow does not follow the setpoint",
                    len(flags) == 1 and flags[0]["id"] == "mfc2", str(flags))
            c.check("the flag carries both numbers",
                    flags and flags[0]["commanded"] == 5.0
                    and flags[0]["measured"] == 0.0, str(flags[:1]))

            # "they stay up only when they are out of bounds"
            vr.mfcs["mfc2"].flow_sccm = 5.0
            await vr.tick()
            c.check("and clears itself the moment it comes back",
                    sup.setpoint_flags() == [], str(sup.setpoint_flags()))

            # Warn-only: the setpoint was written as typed, not refused.
            c.check("the setpoint was never refused or changed",
                    vr.mfcs["mfc2"].commanded_sccm == 5.0,
                    str(vr.mfcs["mfc2"].commanded_sccm))

            # A supply in CONSTANT CURRENT is meant to sit below its voltage
            # setpoint - that is what CC is. Flagging it produced a warning that
            # stood for entire runs (Zach, 2026-09-09: "of course they're not,
            # they're on CC mode").
            c.section("7. a CC supply is not flagged for being below its volts")
            psu = vr.supplies["collimating"]
            psu.output_on = True
            psu.voltage_setpoint, psu.current_setpoint = 100.0, 2.0
            psu.voltage, psu.current = 40.0, 2.0        # current-limited
            sup._setpoint_changed.clear()
            c.check("the virtual supply really is in CC",
                    psu.mode_label() == "CC", str(psu.mode_label()))
            c.check("no flag while it is current-limited",
                    [f for f in sup.setpoint_flags() if f["id"] == "collimating"] == [],
                    str(sup.setpoint_flags()))
            psu.voltage, psu.current = 40.0, 0.5        # off the current limit
            c.check("but a CV supply that misses its volts still is",
                    any(f["id"] == "collimating" for f in sup.setpoint_flags()),
                    str(sup.setpoint_flags()))
            psu.output_on = False
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
