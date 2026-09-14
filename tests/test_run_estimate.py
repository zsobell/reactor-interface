"""POST /api/run/estimate answers "how long would this run take?" before a run.

Zach, 2026-08-28: "make the estimated time display at all times - no reason to
only have it display on run start." The Run tab's est. remaining / finish slots
used to read "—" until Start was pressed, so checking that 150 cycles of these
parameters fit in the afternoon meant starting them.

The point of doing it on the SERVER is that there is only one piece of
arithmetic. The endpoint builds the same recipe the run would build, so the
estimate is the number the countdown starts from by construction, not a browser
copy of it - the countdown was moved out of the browser in 2026-08-21 for
exactly that reason (see RecipeRunner.run_remaining_s). What this file pins is
that identity, plus the two ways it is asked for a number it cannot give.

Run directly: python -m tests.test_run_estimate
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from reactor.control.recipe import build_ald_recipe, build_cvd_recipe
from reactor.server import app as app_mod
from tests._support import Checker, asgi_call

#: A plausible run, in the units the browser posts (runParams() in index.html).
P = dict(
    cycles=150, dose_s=0.05, pump_a_s=10.0, beam_s=5.0, pump_b_s=10.0,
    dose_pressure_torr=0.02, min_current_a=5.0e-4,
    fill_pulse_on_s=0.10, fill_pulse_off_s=0.30, tolerance_frac=0.20,
    reignite_pulse_s=0.10, reignite_settle_s=0.15, ar_close_delay_s=10.0,
    sample_bias_v=0.0, sample_bias_polarity=1,
    gas_overlap_s=0.5,
    mfc1_gas_enable=True, mfc1_gas_order="first", mfc1_gas_pct=50, mfc1_gas_flow_sccm=5,
    mfc2_gas_enable=True, mfc2_gas_order="second", mfc2_gas_pct=50, mfc2_gas_flow_sccm=5,
)


async def main() -> int:
    c = Checker("test_run_estimate")
    app = app_mod.create_app()

    c.section("1. EE-ALD: cycles x the four step durations")
    st, body = await asgi_call(app, "POST", "/api/run/estimate", dict(P, mode="ald"))
    want_cycle = P["dose_s"] + P["pump_a_s"] + P["beam_s"] + P["pump_b_s"]
    c.check("200", st == 200, f"{st} {body}")
    c.check(f"cycle_s == {want_cycle}", body.get("cycle_s") == want_cycle,
            str(body.get("cycle_s")))
    c.check("total_s == cycles x cycle_s",
            body.get("total_s") == want_cycle * P["cycles"],
            f"{body.get('total_s')} vs {want_cycle * P['cycles']}")
    # The identity that matters: same builder, same number as the live run.
    c.check("it is the recipe's own cycle_seconds, not a second sum",
            body.get("cycle_s") == build_ald_recipe(P).cycle_seconds())

    c.section("2. EE-CVD is shorter - no beam step, no pump B")
    st, body = await asgi_call(app, "POST", "/api/run/estimate", dict(P, mode="cvd"))
    want_cycle = P["dose_s"] + P["pump_a_s"]
    c.check("200", st == 200, f"{st} {body}")
    c.check(f"cycle_s == {want_cycle} (dose + pump A only)",
            body.get("cycle_s") == want_cycle, str(body.get("cycle_s")))
    c.check("it is the CVD builder's own cycle_seconds",
            body.get("cycle_s") == build_cvd_recipe(P).cycle_seconds())
    c.check("total_s == cycles x cycle_s",
            body.get("total_s") == want_cycle * P["cycles"], str(body.get("total_s")))

    c.section("3. teardown is excluded, exactly as the countdown excludes it")
    # ar_close_delay_s is a teardown wait. If it leaked into the estimate, the
    # idle number would be bigger than the countdown a run then starts at.
    st, body = await asgi_call(
        app, "POST", "/api/run/estimate", dict(P, mode="ald", ar_close_delay_s=600.0))
    c.check("a 10-minute Ar close delay does not change it",
            body.get("total_s") == (P["dose_s"] + P["pump_a_s"] + P["beam_s"]
                                    + P["pump_b_s"]) * P["cycles"],
            str(body.get("total_s")))

    c.section("4. a half-edited run must not 500 or throw the panel away")
    # One gas on "simultaneous" alone is refused at Start (and _build_gas_schedules
    # raises) - but it is also just what the operator has typed so far. The
    # estimate answers 200 with no number and the panel shows a dash.
    st, body = await asgi_call(
        app, "POST", "/api/run/estimate",
        dict(P, mode="ald", mfc1_gas_order="simultaneous"))
    c.check("200 with total_s null", st == 200 and body.get("total_s") is None,
            f"{st} {body}")
    c.check("and says why", bool(body.get("error")), str(body.get("error"))[:60])

    # Empty body = the fields as they load before anything is typed. Defaults,
    # not a 422: the panel should show a number on first paint.
    st, body = await asgi_call(app, "POST", "/api/run/estimate", {})
    c.check("an empty body estimates the builder defaults",
            st == 200 and body.get("total_s") == build_ald_recipe({}).cycle_seconds()
            * build_ald_recipe({}).cycles, f"{st} {body}")

    c.section("5. the route the Stop pre-start button used is gone")
    # Removed 2026-08-28 with the button: Abort is the one way out of a
    # pre-start, and Supervisor.stop_prestart is reached through it. Read off
    # the route table rather than by calling them - /abort really does command
    # valves and MFCs, which a test has no business doing.
    paths = {getattr(r, "path", None) for r in app.routes}
    c.check("/api/prestart/stop is not registered",
            "/api/prestart/stop" not in paths)
    c.check("/api/prestart/abort still is", "/api/prestart/abort" in paths)
    c.check("/api/run/estimate is registered", "/api/run/estimate" in paths)

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
