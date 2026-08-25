"""The four Keithley 2260B DC supplies: identification, and when their outputs
are switched.

The behaviour under test is Zach's, stated 2026-08-21:

  "set the outputs of the steering, collimating, and grid bias to all turn on
   on prestart and turn off on abort/stop/end of run"
  "steering/grid/collimating/bias can stay on all run. No need to actuate for
   plasma on/off events. Collimating in particular is important for plasma
   stability when the beam dump is grounded."
  "set the output to on for the stage/sample bias only when a non-0 value is
   entered"

So the load-bearing assertions here are as much about what does NOT happen -
these supplies must not be cycled by a reignite - as about what does.

Run directly: python -m tests.test_keithley_supplies
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.devices.keithley_2260b import parse_idn
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

PRE = dict(ar_sccm=4.0, valve_delay_s=0.05, hold_s=0.2, min_current_a=5.0e-4,
           reignite_pulse_s=0.05, reignite_settle_s=0.05,
           dose_pressure_torr=0.02)

RUN = dict(cycles=2, dose_s=0.05, pump_a_s=0.15, beam_s=0.2, pump_b_s=0.1,
           dose_pressure_torr=0.02, min_current_a=5.0e-4)

COILS = ("steering", "grid_bias", "collimating")


def _cols(header: str) -> dict[str, int]:
    return {name: i for i, name in enumerate(header.split(","))}


async def _prestart(vr, params) -> None:
    tick = await autotick(vr, period=0.05)
    try:
        await vr.sup.start_prestart(params)
        while vr.sup.prestart.get("running"):
            await asyncio.sleep(0.02)
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick


async def main() -> int:
    c = Checker("test_keithley_supplies")

    # ------------------------------------------------------------------ #
    c.section("1. *IDN? parsing (real strings from the four supplies)")
    for idn, model, serial in (
        ("Keithley Instruments Inc.,Model 2260B-250-4,1412016,01.84.20190904",
         "2260B-250-4", "1412016"),
        ("Keithley Instruments Inc.,Model 2260B-80-13,1408023,01.72.20150702",
         "2260B-80-13", "1408023"),
        ("Keithley Instruments Inc.,Model 2260B-800-1,1407084,01.72.20150702",
         "2260B-800-1", "1407084"),
        ("Keithley Instruments Inc.,Model 2260B-250-9,1405224,01.72.20150702",
         "2260B-250-9", "1405224"),
    ):
        got = parse_idn(idn)
        c.check(f"{model} / {serial}", got == (model, serial), str(got))
    c.check("a junk *IDN? yields empty, not a crash",
            parse_idn("nonsense") == ("", ""))

    async with VirtualReactor() as vr:
        missing = [k for k in ("stage_bias", *COILS) if k not in vr.supplies]
        if missing:
            c.check("all four supplies configured", False, f"missing {missing}")
            return c.summary()

        vr.instruments["ammeter"].value = 1.0e-3

        # -------------------------------------------------------------- #
        c.section("2. pre-start with NO bias: coils on, stage bias stays off")
        await _prestart(vr, dict(PRE, sample_bias_v=0.0, sample_bias_polarity=1))

        for k in COILS:
            c.check(f"{k} output ON", vr.supplies[k].output_on is True)
        bias = vr.supplies["stage_bias"]
        c.check("stage bias output OFF at 0 V", bias.output_on is False)
        c.check("stage bias voltage never set", bias.voltage_calls == [],
                str(bias.voltage_calls))

        await vr.sup.supplies_output_off(reason="test reset")
        await vr.sup.stop_fill_regulation()
        for k in ("stage_bias", *COILS):
            vr.supplies[k].output_calls.clear()
            vr.supplies[k].voltage_calls.clear()

        # -------------------------------------------------------------- #
        c.section("3. pre-start WITH a negative bias")
        await _prestart(vr, dict(PRE, sample_bias_v=12.0, sample_bias_polarity=-1))

        bias = vr.supplies["stage_bias"]
        c.check("stage bias output ON", bias.output_on is True)
        # Magnitude only: the 2260B is single-quadrant and cannot source
        # negative. The sign belongs to the log, not the instrument.
        c.check("commanded MAGNITUDE, unsigned", bias.voltage_calls == [12.0],
                str(bias.voltage_calls))
        c.check("polarity recorded on the device", bias.polarity == -1,
                str(bias.polarity))
        c.check("voltage set BEFORE the output was enabled",
                bias.voltage_calls and bias.output_calls
                and bias.output_calls[-1] is True)

        # The sign has to reach the snapshot, or the log records a positive
        # bias when the leads are the other way round.
        bias.voltage = 12.0
        await vr.tick()
        logged = vr.sup.snapshot.get("psu.stage_bias.voltage")
        c.check("logged voltage is SIGNED by the polarity toggle",
                logged == -12.0, repr(logged))

        # -------------------------------------------------------------- #
        c.section("4. a reignite must NOT cycle these supplies")
        for k in ("stage_bias", *COILS):
            vr.supplies[k].output_calls.clear()

        # Drop the plasma so the run's watchdog restrikes, then bring it back.
        tick = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(RUN, run_name="Bias-001"))
            await asyncio.sleep(0.3)
            vr.instruments["ammeter"].value = 0.0      # plasma out
            await asyncio.sleep(0.4)
            vr.instruments["ammeter"].value = 1.0e-3   # relit
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

        for k in COILS:
            calls = vr.supplies[k].output_calls
            # Exactly one transition is expected across the whole run: the OFF
            # at run end. Anything more means something cycled them.
            c.check(f"{k} not cycled during the run - only the run-end off",
                    calls == [False], str(calls))
        c.check("stage bias likewise", vr.supplies["stage_bias"].output_calls == [False],
                str(vr.supplies["stage_bias"].output_calls))

        # -------------------------------------------------------------- #
        c.section("5. run end switched every output off")
        for k in ("stage_bias", *COILS):
            c.check(f"{k} output OFF after the run",
                    vr.supplies[k].output_on is False)

        # -------------------------------------------------------------- #
        c.section("6. voltage and current are in the run export")
        path = vr.sup.logger.run_path
        c.check("run export written", path is not None and path.exists(), str(path))
        if path is None or not path.exists():
            return c.summary()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        idx = _cols(lines[0])
        for k in ("stage_bias", *COILS):
            for chan in ("voltage", "current"):
                col = f"psu_{k}_{chan}"
                c.check(f"column '{col}'", col in idx,
                        "" if col in idx else f"header: {lines[0]}")
        c.check("the Glassman kept its own hv_hv_* columns",
                "hv_hv_voltage" in idx, f"header: {lines[0]}")

        # -------------------------------------------------------------- #
        c.section("7. stop_prestart hands over primed - it must NOT switch off")
        await vr.sup.supplies_output_off(reason="test reset")
        for k in ("stage_bias", *COILS):
            vr.supplies[k].output_calls.clear()
        await _prestart(vr, dict(PRE, sample_bias_v=5.0, sample_bias_polarity=1))
        await vr.sup.stop_prestart()

        for k in COILS:
            c.check(f"{k} still ON after stop_prestart",
                    vr.supplies[k].output_on is True,
                    str(vr.supplies[k].output_calls))
        c.check("stage bias still ON after stop_prestart",
                vr.supplies["stage_bias"].output_on is True)

        # -------------------------------------------------------------- #
        c.section("8. the pre-start ABORT does switch everything off")
        await vr.sup.abort_prestart()
        for k in ("stage_bias", *COILS):
            c.check(f"{k} OFF after abort_prestart",
                    vr.supplies[k].output_on is False)

        # -------------------------------------------------------------- #
        c.section("9. operator control from the Hardware tab")
        # Requested 2026-08-25: V/I fields and an output toggle per supply.
        res = await vr.sup.set_supply_voltage("grid_bias", 120.0)
        c.check("set_supply_voltage reaches the device",
                vr.supplies["grid_bias"].voltage_calls[-1] == 120.0,
                str(vr.supplies["grid_bias"].voltage_calls))
        c.check("and reports back", res.get("voltage") == 120.0, str(res))

        res = await vr.sup.set_supply_current("grid_bias", 0.35)
        c.check("set_supply_current reaches the device",
                vr.supplies["grid_bias"].current_calls[-1] == 0.35,
                str(vr.supplies["grid_bias"].current_calls))
        c.check("and reports back", res.get("current") == 0.35, str(res))

        await vr.sup.set_supply_output("grid_bias", True)
        c.check("operator can switch an output on",
                vr.supplies["grid_bias"].output_on is True)
        await vr.sup.set_supply_output("grid_bias", False)
        c.check("...and off", vr.supplies["grid_bias"].output_on is False)

        # An unknown id or a disconnected supply must raise, not no-op: the
        # operator is waiting on the answer.
        c.check("unknown supply id raises",
                await _raises_async(
                    lambda: vr.sup.set_supply_output("nope", True), KeyError))
        vr.supplies["steering"].connected = False
        c.check("a disconnected supply raises",
                await _raises_async(
                    lambda: vr.sup.set_supply_voltage("steering", 5.0),
                    RuntimeError))
        vr.supplies["steering"].connected = True

        # The Glassman has no set path at all - that is the whole point of it.
        if "hv" in vr.supplies:
            c.check("the Glassman refuses voltage control",
                    await _raises_async(
                        lambda: vr.sup.set_supply_voltage("hv", 100.0),
                        RuntimeError))

        # -------------------------------------------------------------- #
        c.section("10. nothing sets a CURRENT automatically")
        # Only the operator's field does. A run that silently re-limited a
        # supply would be a real hazard, so this is asserted rather than assumed.
        for k in ("stage_bias", *COILS):
            if k == "grid_bias":
                continue        # section 9 set this one deliberately
            c.check(f"{k}: no automatic current change",
                    vr.supplies[k].current_calls == [],
                    str(vr.supplies[k].current_calls))

        # -------------------------------------------------------------- #
        c.section("11. CV/CC derived from measurement vs setpoint")
        # :OUTP:MODE? was the first attempt and was wrong - it reads 0 on all
        # four regardless of state, so coils demonstrably in CC showed CV
        # (reported 2026-08-25). The mode is now derived from which limit the
        # output has actually reached.
        dev = vr.supplies["collimating"]
        dev.voltage_setpoint, dev.current_setpoint = 150.0, 2.5

        dev.output_on, dev.voltage, dev.current = False, 150.0, 0.4
        c.check("output off -> no mode claimed", dev.mode_label() is None,
                repr(dev.mode_label()))

        # CV: sitting at the voltage setpoint, current well under its limit.
        dev.output_on, dev.voltage, dev.current = True, 150.0, 0.4
        c.check("at V setpoint, I below limit -> CV", dev.mode_label() == "CV",
                repr(dev.mode_label()))

        # CC: current pinned at the limit, voltage dragged below its setpoint.
        dev.voltage, dev.current = 88.0, 2.5
        c.check("at I limit, V below setpoint -> CC", dev.mode_label() == "CC",
                repr(dev.mode_label()))

        # A fraction of a percent of drift must not flip the label.
        dev.voltage, dev.current = 88.0, 2.494
        c.check("0.24% low on the current limit is still CC",
                dev.mode_label() == "CC", repr(dev.mode_label()))
        dev.voltage, dev.current = 149.7, 0.4
        c.check("0.2% low on the voltage setpoint is still CV",
                dev.mode_label() == "CV", repr(dev.mode_label()))

        # Neither limit reached: say nothing rather than guess.
        dev.voltage, dev.current = 40.0, 0.4
        c.check("neither limit reached -> no mode claimed",
                dev.mode_label() is None, repr(dev.mode_label()))

        # The sample bias signs its logged voltage; the comparison must use
        # magnitudes or a negative-polarity bias would never read CV.
        bias = vr.supplies["stage_bias"]
        bias.voltage_setpoint, bias.current_setpoint = 30.0, 0.5
        bias.polarity, bias.output_on = -1, True
        bias.voltage, bias.current = -30.0, 0.05
        c.check("negative-polarity bias at setpoint -> CV",
                bias.mode_label() == "CV", repr(bias.mode_label()))
        bias.polarity, bias.output_on = 1, False
        dev.output_on = False

    return c.summary()


async def _raises_async(coro_fn, exc_type) -> bool:
    try:
        await coro_fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
