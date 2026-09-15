"""The sample bias brackets the electron beam instead of running all run.

Zach's request, 2026-08-26:

  "the sample bias thermocouple issue has become untenable. We need the sample
   bias to trigger 0.2 s before the e-beam and turn off 0.2 s after."

So the load-bearing assertions are the timings around each beam, and the
things that must NOT happen: the bias must not be up during pump A / pump B
(that is the whole point - a clean stage thermocouple while the beam is off),
a reignite must not cycle it, and bracketing the beam must not make a cycle
any longer than the sum of its step durations.

Run directly: python -m tests.test_sample_bias_bracket
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick, wait_for

LEAD = 0.30
TRAIL = 0.25

RUN = dict(cycles=3, dose_s=0.05, pump_a_s=0.9, beam_s=0.5, pump_b_s=0.7,
           dose_pressure_torr=0.02, min_current_a=5.0e-4,
           reignite_pulse_s=0.05, reignite_settle_s=0.05,
           sample_bias_v=30.0, sample_bias_polarity=-1,
           sample_bias_lead_s=LEAD, sample_bias_trail_s=TRAIL)


def beam_windows(vr, switch: str = "plasma_ground") -> list[tuple[float, float]]:
    """(beam_on_t, beam_off_t) for every beam window, in order.

    Pairing has to walk the edges rather than slice them: the recipe's SETUP
    grounds the beam before the first cycle, and the teardown parks the relay
    de-energised (beam-on mode) at the end, so the sequence both starts with an
    OFF and ends with an unmatched ON.
    """
    windows: list[tuple[float, float]] = []
    started: float | None = None
    for t, on in beam_edges(vr, switch):
        if on:
            started = t
        elif started is not None:
            windows.append((started, t))
            started = None
    return windows


def beam_edges(vr, switch: str = "plasma_ground") -> list[tuple[float, bool]]:
    """(t, beam_on) for every write to the plasma-ground line, de-duplicated.

    Beam ON is the switch OFF (the relay rests de-energised - see
    docs/HARDWARE.md), so the sense is inverted here once and never again.
    """
    edges: list[tuple[float, bool]] = []
    for t, key, value in vr.daq.do_writes:
        if key != switch:
            continue
        beam_on = not value
        if edges and edges[-1][1] == beam_on:
            continue                    # re-drive of the same state
        edges.append((t, beam_on))
    return edges


async def run_ald(vr, params: dict, *, drop_plasma_at: float | None = None) -> None:
    """Run an EE-ALD recipe to completion, optionally dropping the plasma for
    0.3 s once the run is `drop_plasma_at` seconds old (to force a reignite)."""
    tick = await autotick(vr, period=0.05)
    try:
        vr.instruments["ammeter"].value = 1.0e-3
        await vr.sup.start_ald_run(dict(params))
        if drop_plasma_at is not None:
            await asyncio.sleep(drop_plasma_at)
            vr.instruments["ammeter"].value = 0.0
            await asyncio.sleep(0.3)
            vr.instruments["ammeter"].value = 1.0e-3
        while vr.sup.recipes.busy:
            await asyncio.sleep(0.02)
    finally:
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick


async def main() -> int:
    c = Checker("test_sample_bias_bracket")

    async with VirtualReactor() as vr:
        if "stage_bias" not in vr.supplies:
            c.check("sample-bias supply configured", False)
            return c.summary()
        bias = vr.supplies["stage_bias"]

        # ---------------------------------------------------------------- #
        c.section("1. EE-ALD: the bias leads each beam and trails it off")
        await run_ald(vr, RUN)

        pairs = beam_windows(vr)
        c.check("three beams ran", len(pairs) == 3, f"{len(pairs)} beam windows")

        bias_on = [t for t, on in bias.output_events if on]
        bias_off = [t for t, on in bias.output_events if not on]
        # Pre-start was not run here, so the only OFFs are the trailing ones
        # plus the run-end sweep in finish_run.
        c.check("one bias ON per beam", len(bias_on) == 3,
                f"{len(bias_on)} ONs: {[round(t, 2) for t in bias_on]}")

        if len(bias_on) == 3 and len(pairs) == 3:
            leads = [beam - b for b, (beam, _) in zip(bias_on, pairs)]
            c.check("bias comes up ~LEAD before every beam",
                    all(abs(x - LEAD) < 0.12 for x in leads),
                    f"leads {[round(x, 3) for x in leads]} vs {LEAD}")
            # The trailing OFFs are the first three; finish_run adds one more.
            trails = [b - beam for b, (_, beam) in zip(bias_off, pairs)]
            c.check("bias goes down ~TRAIL after every beam",
                    len(trails) == 3 and all(abs(x - TRAIL) < 0.12 for x in trails),
                    f"trails {[round(x, 3) for x in trails]} vs {TRAIL}")
            # The point of the exercise: the stage is unenergised for most of
            # the cycle, so the stage TC reads clean while the beam is off.
            biased = sum(off - on for on, off in zip(bias_on, bias_off))
            lit = sum(off - on for on, off in pairs)
            c.check("bias is up only around the beams",
                    biased < lit + 3 * (LEAD + TRAIL) + 0.3,
                    f"{biased:.2f}s biased vs {lit:.2f}s of beam")

        c.check("voltage programmed ONCE for the run, as a magnitude",
                bias.voltage_calls == [30.0], str(bias.voltage_calls))
        c.check("lead orientation recorded on the device", bias.polarity == -1,
                str(bias.polarity))
        c.check("bias OFF when the run ends", bias.output_on is False)

        # ---------------------------------------------------------------- #
        c.section("2. a reignite must NOT cycle the bias")
        bias.output_calls.clear()
        bias.output_events.clear()
        bias.voltage_calls.clear()
        vr.daq.do_writes.clear()
        await vr.sup.stop_fill_regulation()

        await run_ald(vr, dict(RUN, cycles=2, beam_s=0.8), drop_plasma_at=1.2)
        flags = [e for e in vr.sup.events
                 if "reignit" in str(e.get("message", "")).lower()]
        c.check("the plasma really did drop and restrike", bool(flags),
                f"{len(flags)} reignite events")
        # Two beams -> two ONs and two OFFs, plus finish_run's sweep. A bias
        # that chased the reignite pulse would show more.
        c.check("still exactly one bias ON per beam",
                bias.output_calls.count(True) == 2,
                str(bias.output_calls))

        # ---------------------------------------------------------------- #
        c.section("3. bracketing the beam does not lengthen a cycle")
        bias.output_events.clear()
        vr.daq.do_writes.clear()
        await vr.sup.stop_fill_regulation()

        params = dict(RUN, cycles=3)
        nominal = (params["dose_s"] + params["pump_a_s"]
                   + params["beam_s"] + params["pump_b_s"])
        tick = await autotick(vr, period=0.05)
        try:
            vr.instruments["ammeter"].value = 1.0e-3
            await vr.sup.start_ald_run(params)
            await wait_for(lambda: vr.sup.recipes.progress.phase == "cycling")
            t0 = asyncio.get_running_loop().time()
            await wait_for(lambda: vr.sup.recipes.progress.cycle == 3, timeout=20)
            per_cycle = (asyncio.get_running_loop().time() - t0) / 2
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick
        c.check("cycle length is still the sum of its steps",
                abs(per_cycle - nominal) < 0.15,
                f"{per_cycle:.3f}s measured vs {nominal:.3f}s nominal")

        # ---------------------------------------------------------------- #
        c.section("4. zero bias: the supply is never switched on at all")
        bias.output_calls.clear()
        bias.output_events.clear()
        bias.voltage_calls.clear()
        await vr.sup.stop_fill_regulation()

        await run_ald(vr, dict(RUN, cycles=2, sample_bias_v=0.0))
        c.check("no bias ON commanded", True not in bias.output_calls,
                str(bias.output_calls))
        c.check("no voltage written either", bias.voltage_calls == [],
                str(bias.voltage_calls))

        # ---------------------------------------------------------------- #
        c.section("5. an abort mid-beam drops the bias immediately")
        bias.output_calls.clear()
        bias.output_events.clear()
        await vr.sup.stop_fill_regulation()

        tick = await autotick(vr, period=0.05)
        try:
            vr.instruments["ammeter"].value = 1.0e-3
            await vr.sup.start_ald_run(dict(RUN, cycles=5, beam_s=3.0))
            got = await wait_for(lambda: bias.output_on is True, timeout=10)
            c.check("bias came up for the first beam", got)
            await vr.sup.abort_recipe()
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick
        c.check("bias OFF right after the abort", bias.output_on is False)
        # A queued flip that fired after the run would re-energise the stage.
        await asyncio.sleep(LEAD + TRAIL + 0.2)
        c.check("and nothing queued brings it back", bias.output_on is False,
                str(bias.output_calls))

        # ---------------------------------------------------------------- #
        c.section("6. EE-CVD: the bias brackets the run-long beam")
        bias.output_calls.clear()
        bias.output_events.clear()
        bias.voltage_calls.clear()
        vr.daq.do_writes.clear()
        await vr.sup.stop_fill_regulation()

        tick = await autotick(vr, period=0.05)
        try:
            vr.instruments["ammeter"].value = 1.0e-3
            await vr.sup.start_cvd_run(dict(
                RUN, cycles=2, pump_a_s=0.4, ar_close_delay_s=0.1))
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

        windows = beam_windows(vr)
        bias_on = [t for t, on in bias.output_events if on]
        bias_off = [t for t, on in bias.output_events if not on]
        c.check("one beam window for the whole run", len(windows) == 1,
                f"{len(windows)} windows")
        c.check("the bias came up once, not once per cycle",
                len(bias_on) == 1, f"{len(bias_on)} ONs")
        if bias_on and windows:
            c.check("it led the strike by ~LEAD",
                    abs((windows[0][0] - bias_on[0]) - LEAD) < 0.15,
                    f"{windows[0][0] - bias_on[0]:.3f}s vs {LEAD}")
            # beam_stop grounds the beam, then the trail runs, then the bias
            # drops - before the end-of-run sequence that follows it.
            c.check("and trailed the beam off by ~TRAIL",
                    bool(bias_off) and abs((bias_off[0] - windows[0][1]) - TRAIL) < 0.15,
                    f"off at {windows[0][1]:.2f}, "
                    f"bias {[round(t, 2) for t in bias_off]}")
        c.check("bias OFF when the run ends", bias.output_on is False)

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
