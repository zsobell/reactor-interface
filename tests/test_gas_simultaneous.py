"""Simultaneous gas scheduling: two gases sharing the whole window.

Zach's request, 2026-08-26:

  "add an option for simultaneous MFC use. if more than one MFC selects
   simultaneous then they both run together for the whole e-beam cycle. the %
   gets grayed out, and the flow rate set for each gets used. if only one
   selects simultaneous an error pops up on start and the run doesn't start."

Three things to pin down, then: that the window is not divided (no handoff
happens mid-exposure, which is the whole difference from First/Second), that
each gas keeps its OWN flow rate, and that exactly one gas set to simultaneous
refuses the run rather than quietly running as a 100% "first".

The greying-out of % is UI-side and not testable here; it is asserted in the
browser check rather than in this file.

Run directly: python -m tests.test_gas_simultaneous
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.control.recipe import build_ald_recipe, build_cvd_recipe
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

H2_FLOW = 5.0
N2_FLOW = 3.0

BOTH = dict(
    cycles=2, dose_s=0.05, pump_a_s=0.6, beam_s=0.8, pump_b_s=0.3,
    dose_pressure_torr=0.02, min_current_a=5.0e-4, gas_overlap_s=0.2,
    reignite_pulse_s=0.05, reignite_settle_s=0.05,
    mfc1_gas_enable=True, mfc1_gas_order="simultaneous",
    mfc1_gas_pct=40, mfc1_gas_flow_sccm=H2_FLOW,
    mfc2_gas_enable=True, mfc2_gas_order="simultaneous",
    mfc2_gas_pct=60, mfc2_gas_flow_sccm=N2_FLOW,
)


def transitions(mfc) -> list[tuple[float, float]]:
    """The setpoint history with repeats collapsed.

    The runner deliberately re-asserts a gas it believes is already on (the
    lead-in task sets it, then the beam step sets it again), so the raw call
    list double-counts. What a schedule is about is the CHANGES.
    """
    out: list[tuple[float, float]] = []
    for t, sccm in mfc.setpoint_calls:
        if out and out[-1][1] == sccm:
            continue
        out.append((t, sccm))
    return out


def raises(fn) -> str:
    try:
        fn()
    except ValueError as exc:
        return str(exc)
    return ""


async def run_to_completion(vr, coro_fn) -> None:
    """Run a recipe to the end with the plasma lit throughout.

    The ammeter is set and PUBLISHED before the run starts. Setting it after
    autotick() and trusting the first tick to land first is a race: if the beam
    step's opening current check runs before any tick has copied the value into
    the snapshot, the beam reads as out, the runner pulses a reignite, and that
    pulse looks exactly like an extra beam window to the pairing below - which
    made this file fail about one suite run in five.
    """
    vr.instruments["ammeter"].value = 1.0e-3
    await vr.tick()                      # value reaches sup.snapshot
    tick = await autotick(vr, period=0.05)
    try:
        await coro_fn()
        while vr.sup.recipes.busy:
            await asyncio.sleep(0.02)
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick


async def main() -> int:
    c = Checker("test_gas_simultaneous")

    # ------------------------------------------------------------------ #
    c.section("1. simultaneous is a pairing - one on its own is refused")
    lone = raises(lambda: build_ald_recipe(dict(BOTH, mfc2_gas_order="second")))
    c.check("one simultaneous + one second is refused", "Simultaneous" in lone, lone)
    # No device has reported a gas here (no supervisor), so the message names
    # the CHANNELS. With a gas known it says "NH3" instead - never a static one.
    c.check("...and the message names both lines and the way out",
            "MFC 1" in lone and "MFC 2" in lone and "First or Second" in lone, lone)

    off = raises(lambda: build_ald_recipe(dict(BOTH, mfc2_gas_enable=False)))
    c.check("a simultaneous gas with the other one switched OFF is refused too",
            "Simultaneous" in off, off)

    c.check("both simultaneous builds fine", raises(lambda: build_ald_recipe(BOTH)) == "")
    c.check("and in EE-CVD too", raises(lambda: build_cvd_recipe(BOTH)) == "")

    # The pre-existing rule is untouched: two gases cannot share First.
    both_first = raises(lambda: build_ald_recipe(
        dict(BOTH, mfc1_gas_order="first", mfc2_gas_order="first")))
    c.check("two gases still cannot share First",
            "at most one gas" in both_first, both_first)

    r = build_ald_recipe(BOTH)
    c.check("both schedules carry their own flow",
            sorted(g.flow_sccm for g in r.gas_schedules) == [N2_FLOW, H2_FLOW],
            str([(g.mfc, g.flow_sccm) for g in r.gas_schedules]))

    async with VirtualReactor() as vr:
        h2, n2 = vr.mfcs["mfc1"], vr.mfcs["mfc2"]

        # -------------------------------------------------------------- #
        c.section("2. EE-ALD: both cover the whole exposure, at their own flow")
        vr.daq.do_writes.clear()
        await run_to_completion(vr, lambda: vr.sup.start_ald_run(dict(BOTH)))

        for mfc, want in ((h2, H2_FLOW), (n2, N2_FLOW)):
            tr = transitions(mfc)
            # The recipe's setup zeroes every scheduled gas first (they only
            # flow proximal to the beam, never for the whole run), so the
            # sequence opens with a 0. Then 2 cycles: on, off, on, off.
            # Anything more is a handoff, which must not exist in this mode.
            c.check(f"{mfc.id}: one on/off pair per cycle",
                    [v for _t, v in tr] == [0.0, want, 0.0, want, 0.0],
                    str([round(v, 2) for _t, v in tr]))
            c.check(f"{mfc.id}: ran at its OWN flow, not a shared one",
                    all(v in (0.0, want) for _t, v in tr),
                    str([round(v, 2) for _t, v in tr]))

        # Nothing may switch BETWEEN the beam coming on and going off - that is
        # exactly what "not divided" means.
        beams = []
        started = None
        for t, key, value in vr.daq.do_writes:
            if key != "plasma_ground":
                continue
            if value is False and started is None:
                started = t
            elif value is True and started is not None:
                beams.append((started, t))
                started = None
        # One window per cycle. A reignite would pulse the plasma-ground line
        # mid-step and show up here as an extra window, so this doubles as an
        # assertion that the plasma stayed lit - which the checks below assume.
        c.check("two beam windows ran, no reignite", len(beams) == 2,
                f"{len(beams)} windows - a reignite splits one")
        inside = [(m.id, round(t, 3), v) for m in (h2, n2)
                  for t, v in transitions(m)
                  for on, offt in beams if on < t < offt]
        c.check("no gas switched mid-exposure", inside == [], str(inside))

        # And they were up BEFORE the beam struck, led in by the overlap - the
        # same lead-in a "first" gas gets, since these also start with the beam.
        #
        # Asserted as a WINDOW, not as overlap +/- a hair. The lead-in is a
        # scheduled task and the beam is a step boundary, so this measures two
        # independent wall-clock events against each other: on a loaded machine
        # Windows can slip either by more than 100 ms, and a tight bound here
        # flaked once in five suite runs. The window still fails on both real
        # regressions - no lead at all (early ~ 0) and leading from the wrong
        # anchor (the whole 0.65 s runway, not the 0.2 s overlap).
        overlap = BOTH["gas_overlap_s"]
        runway = BOTH["dose_s"] + BOTH["pump_a_s"]
        for mfc in (h2, n2):
            first_on = next(t for t, v in transitions(mfc) if v > 0)
            early = beams[0][0] - first_on if beams else None
            c.check(f"{mfc.id}: on before the beam struck, by about the overlap",
                    early is not None and 0.08 < early < (overlap + runway) / 2,
                    f"{early:.3f}s early vs {overlap}s overlap "
                    f"(runway {runway}s)" if early else "no beam")

        c.check("both zeroed at run end",
                h2.commanded_sccm == 0.0 and n2.commanded_sccm == 0.0,
                f"h2={h2.commanded_sccm} n2={n2.commanded_sccm}")

        # -------------------------------------------------------------- #
        c.section("3. EE-CVD: on once for the run, not cycled per cycle")
        await vr.sup.stop_fill_regulation()
        h2.setpoint_calls.clear()
        n2.setpoint_calls.clear()
        vr.daq.do_writes.clear()

        await run_to_completion(vr, lambda: vr.sup.start_cvd_run(
            dict(BOTH, cycles=3, pump_a_s=0.3, ar_close_delay_s=0.1)))

        for mfc, want in ((h2, H2_FLOW), (n2, N2_FLOW)):
            tr = [v for _t, v in transitions(mfc)]
            # The beam is on for the whole run, so the window is the whole run:
            # setup's zero, one on at the start, one off at the end. Three
            # cycles must not produce three pairs.
            c.check(f"{mfc.id}: exactly one on then one off for the whole run",
                    tr == [0.0, want, 0.0], str([round(v, 2) for v in tr]))

        c.check("both zeroed at run end",
                h2.commanded_sccm == 0.0 and n2.commanded_sccm == 0.0,
                f"h2={h2.commanded_sccm} n2={n2.commanded_sccm}")

        # -------------------------------------------------------------- #
        c.section("4. the run really is refused, not just the recipe build")
        await vr.sup.stop_fill_regulation()
        bad = dict(BOTH, mfc2_gas_order="second")
        try:
            await vr.sup.start_ald_run(bad)
            refused = ""
        except ValueError as exc:
            refused = str(exc)
        c.check("start_ald_run raises", "Simultaneous" in refused, refused)
        c.check("and no run was started", not vr.sup.recipes.busy,
                vr.sup.recipes.progress.state)

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
