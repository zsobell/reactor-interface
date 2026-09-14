"""The Ar pneumatic is pulsed open, never opened in one flip.

Zach's request, 2026-08-26:

  "When the Ar pneumatic is opened - any time, including manually through the
   hardware tab and on prestart, it should actuate for 0.05 s 5 times with a
   0.5 s delay inbetween each, so as to let built up Ar into the reactor more
   slowly."

...cut to ONE pulse the same day: "the valves do not actuate fast enough to
prevent the inrush that turns off my pressure gauge. Drop the number of pulses
to 1 before full open."

"any time" is why this lives in Supervisor.set_valve rather than in the
callers, and it is what most of this file is about: the same pulse train has
to come out of a manual open, a pre-start, and a recipe step. The other
load-bearing assertion is that the pulses' CLOSES are not real closes - a
close of an isolation valve zeroes its MFC's setpoint, and these are part of
opening.

Run directly: python -m tests.test_soft_open
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

AR = "ar_pneumatic"

#: Fast stand-in for the operator's 1 x 0.05 s / 0.5 s, with more than one
#: pulse on purpose: the pulse TRAIN is what is under test, and a single pulse
#: cannot tell a repeat count from a hardcoded one. Section 1 checks the real
#: defaults separately.
FAST = {"ar_soft_open_pulses": 4, "ar_soft_open_on_s": 0.02,
        "ar_soft_open_gap_s": 0.05}


def writes(vr, valve: str = AR) -> list[tuple[float, bool]]:
    return [(t, v) for t, key, v in vr.daq.do_writes if key == valve]


async def main() -> int:
    c = Checker("test_soft_open")

    async with VirtualReactor() as vr:
        # ---------------------------------------------------------------- #
        c.section("1. the defaults are the ones the operator asked for")
        c.check("1 pulse", vr.sup.soft_open["pulses"] == 1,
                str(vr.sup.soft_open))
        c.check("0.05 s wide", vr.sup.soft_open["on_s"] == 0.05)
        c.check("0.5 s apart", vr.sup.soft_open["gap_s"] == 0.5)
        c.check("the Ar pneumatic is the valve flagged for it",
                vr.sup._soft_open_plan(AR) is not None)
        c.check("no other valve is", vr.sup._soft_open_plan("prec1") is None)

        # ---------------------------------------------------------------- #
        c.section("2. a manual open is pulsed in and ends OPEN")
        vr.sup.set_soft_open_params(FAST)
        vr.daq.do_writes.clear()
        await vr.sup.set_valve(AR, True, reason="manual")

        w = writes(vr)
        c.check("4 pulses then the final open", len(w) == 9, f"{len(w)} writes")
        c.check("the pulses alternate open/closed",
                [v for _, v in w] == [True, False] * 4 + [True],
                str([v for _, v in w]))
        c.check("the valve is OPEN afterwards",
                vr.daq.do_state[AR] is True
                and vr.sup.valve_state[AR] is True)
        if len(w) == 9:
            gaps = [w[i + 1][0] - w[i][0] for i in range(1, 8, 2)]
            widths = [w[i + 1][0] - w[i][0] for i in range(0, 8, 2)]
            c.check("each pulse is ~on_s wide",
                    all(abs(x - FAST["ar_soft_open_on_s"]) < 0.03 for x in widths),
                    str([round(x, 3) for x in widths]))
            c.check("the gaps are ~gap_s",
                    all(abs(x - FAST["ar_soft_open_gap_s"]) < 0.03 for x in gaps),
                    str([round(x, 3) for x in gaps]))

        c.check("one event describes it, not ten",
                sum(1 for e in vr.sup.events
                    if "soft open" in str(e.get("message", ""))) == 1,
                str([e.get("message") for e in list(vr.sup.events)[-4:]]))

        # ---------------------------------------------------------------- #
        c.section("3. closing is untouched - one flip, as always")
        vr.daq.do_writes.clear()
        await vr.sup.set_valve(AR, False, reason="manual")
        c.check("a single write", writes(vr) and len(writes(vr)) == 1,
                str(writes(vr)))
        c.check("the valve is closed", vr.daq.do_state[AR] is False)

        # ---------------------------------------------------------------- #
        c.section("4. the pulse closes must NOT zero a live Ar setpoint")
        # set_valve zeroes an MFC whose isolation valve closes. The closes in
        # the pulse train are part of OPENING, so they use the quiet write path
        # and that side effect must not fire - otherwise re-opening an already
        # open valve would silently stop the gas.
        await vr.sup.set_valve(AR, True, reason="manual")
        await vr.sup.set_mfc_setpoint("ar", 4.0)
        c.check("Ar flowing at 4 sccm before the re-open",
                vr.mfcs["ar"].commanded_sccm == 4.0,
                str(vr.mfcs["ar"].commanded_sccm))
        await vr.sup.set_valve(AR, True, reason="manual re-open")
        c.check("setpoint survived the pulse train",
                vr.mfcs["ar"].commanded_sccm == 4.0,
                str(vr.mfcs["ar"].commanded_sccm))
        await vr.sup.set_valve(AR, False, reason="manual")
        c.check("a REAL close still zeroes it",
                vr.mfcs["ar"].commanded_sccm == 0.0,
                str(vr.mfcs["ar"].commanded_sccm))

        # ---------------------------------------------------------------- #
        c.section("5. zero pulses turns it back into one plain flip")
        vr.sup.set_soft_open_params({"ar_soft_open_pulses": 0})
        vr.daq.do_writes.clear()
        await vr.sup.set_valve(AR, True, reason="manual")
        c.check("a single write", len(writes(vr)) == 1, str(writes(vr)))
        c.check("still ends OPEN", vr.daq.do_state[AR] is True)
        await vr.sup.set_valve(AR, False, reason="manual")

        # ---------------------------------------------------------------- #
        c.section("6. the settings come from the UI's saved run params")
        got = vr.sup.set_soft_open_params(
            {"ar_soft_open_pulses": "3", "ar_soft_open_on_s": "0.07",
             "ar_soft_open_gap_s": "0.25", "cycles": "150"})
        c.check("parsed from strings, as the UI saves them",
                (got["pulses"], got["on_s"], got["gap_s"]) == (3.0, 0.07, 0.25),
                str(got))
        vr.sup.set_soft_open_params({"ar_soft_open_on_s": "not a number"})
        c.check("junk leaves the previous value alone",
                vr.sup.soft_open["on_s"] == 0.07, str(vr.sup.soft_open))
        vr.sup.set_soft_open_params({})
        c.check("an empty save changes nothing",
                vr.sup.soft_open["pulses"] == 3.0, str(vr.sup.soft_open))

        # ---------------------------------------------------------------- #
        c.section("7. pre-start opens it the same way")
        vr.sup.set_soft_open_params(FAST)
        vr.daq.do_writes.clear()
        vr.instruments["ammeter"].value = 1.0e-3
        tick = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_prestart(dict(
                ar_sccm=4.0, valve_delay_s=0.05, hold_s=0.2,
                min_current_a=5.0e-4, reignite_pulse_s=0.05,
                reignite_settle_s=0.05, dose_pressure_torr=0.02))
            while vr.sup.prestart.get("running"):
                await asyncio.sleep(0.02)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick
        await vr.sup.stop_fill_regulation()

        c.check("pre-start pulsed it too", len(writes(vr)) == 9,
                f"{len(writes(vr))} writes")
        c.check("and left it open", vr.daq.do_state[AR] is True)
        c.check("Ar flowing after it", vr.mfcs["ar"].commanded_sccm == 4.0,
                str(vr.mfcs["ar"].commanded_sccm))

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
