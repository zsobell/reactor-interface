"""Pure HCPES plan, expansion, estimate, and campaign coverage."""
from __future__ import annotations

import asyncio

from reactor.config import load_config
from reactor.control.hcpes_model import (
    CampaignSession,
    HcpesPlan,
    HcpesSettings,
    PolarityCampaign,
    StabilityProfile,
    SweepAxis,
    capability_catalog,
    clone_opposite_polarity,
    preview_values,
    resolve_plan,
)
from tests._support import Checker


def plan_payload() -> dict:
    return {
        "schema_version": 1,
        "id": "cathode-map",
        "name": "Cathode map",
        "stage_polarity": 1,
        "axes": [
            {"target": "mfc:ar", "mode": "linear", "start": 1, "stop": 2, "step": 1},
            {"target": "supply:stage_bias", "mode": "list", "values": [0, 10]},
            {"target": "supply:collimating", "mode": "fixed", "value": 1.5},
            {"target": "supply:steering", "mode": "fixed", "value": 0.4},
            {"target": "supply:grid_bias", "mode": "list", "values": [100, 200]},
            {"target": "mfc:mfc1", "mode": "locked_zero"},
            {"target": "mfc:mfc2", "mode": "locked_zero"},
        ],
    }


def raises(fn, text: str) -> bool:
    try:
        fn()
    except (TypeError, ValueError) as exc:
        return text in str(exc)
    return False


async def main() -> int:
    c = Checker("test_hcpes_model")
    cfg = load_config("config/reactor.yaml")

    c.section("configured target registry")
    catalog = capability_catalog(cfg)
    targets = {axis["target"]: axis for axis in catalog["axes"]}
    c.check("catalog schema is stable", catalog["schema_version"] == 1)
    c.check("requested supplies have typed quantities",
            targets["supply:stage_bias"]["quantity"] == "voltage"
            and targets["supply:grid_bias"]["quantity"] == "voltage"
            and targets["supply:collimating"]["quantity"] == "current"
            and targets["supply:steering"]["quantity"] == "current")
    c.check("all configured MFCs are represented",
            all(f"mfc:{mfc.id}" in targets for mfc in cfg.mfcs))
    c.check("only background MFCs may be locked zero",
            not targets["mfc:ar"]["locked_zero_allowed"]
            and targets["mfc:mfc1"]["locked_zero_allowed"])

    c.section("defaults and deterministic lazy expansion")
    plan = HcpesPlan.model_validate(plan_payload())
    resolved = resolve_plan(plan, cfg)
    defaults = HcpesSettings()
    c.check("approved HCPES defaults",
            defaults.establishment.trend_window_s == 5
            and defaults.establishment.stable_window_s == 20
            and defaults.establishment.maximum_wait_s == 60
            and defaults.establishment.max_drift_a_per_min == 0.0001
            and defaults.parameter_change.trend_window_s == 3
            and defaults.parameter_change.stable_window_s == 3
            and defaults.parameter_change.maximum_wait_s == 10
            and defaults.parameter_change.max_drift_a_per_min == 0.0003
            and defaults.parameter_settle_s == 3
            and defaults.condition_settle_mode == "time"
            and defaults.plasma_min_current_a == 0.0001
            and defaults.recovery_window_s == 30
            and defaults.reignite_pulse_s == 1
            and defaults.reignite_settle_s == 1
            and defaults.qualified_samples == 5)
    points = list(resolved.iter_points())
    c.check("cartesian point count is exact", resolved.point_count == 8)
    c.check("plan order is outer to inner",
            points[0].setpoints["mfc:ar"] == 1
            and points[0].setpoints["supply:grid_bias"] == 100
            and points[1].setpoints["supply:grid_bias"] == 200
            and points[4].setpoints["mfc:ar"] == 2)
    c.check("locked background flows remain explicit zeroes",
            all(point.setpoints["mfc:mfc1"] == 0
                and point.setpoints["mfc:mfc2"] == 0 for point in points))
    c.check("legacy plans migrate startup values from point one",
            resolved.initial_setpoints == points[0].setpoints
            and plan.initial_setpoints["mfc:ar"] == 1
            and "mfc:mfc1" not in plan.initial_setpoints)
    estimate = resolved.estimate()
    c.check("preview counts changed-only writes and qualified samples",
            estimate.parameter_changes == 16
            and estimate.qualified_samples == 40
            and estimate.best_case_s == 58
            and estimate.startup_timeout_case_s == 93,
            str(estimate.model_dump()))
    current_mode = plan.model_copy(deep=True)
    current_mode.settings.condition_settle_mode = "current"
    current_estimate = resolve_plan(current_mode, cfg).estimate()
    c.check("current-mode estimate uses independent establishment and parameter gates",
            current_estimate.best_case_s == 67
            and current_estimate.startup_timeout_case_s == 130,
            str(current_estimate.model_dump()))
    independent = HcpesPlan.model_validate({
        **plan_payload(),
        "initial_setpoints": {
            "mfc:ar": 5,
            "supply:stage_bias": 20,
            "supply:collimating": 1.5,
            "supply:steering": 0.4,
            "supply:grid_bias": 100,
        },
    })
    independent_resolved = resolve_plan(independent, cfg)
    c.check("independent startup adds the point-one transition",
            independent_resolved.initial_transition_changes == 2
            and independent_resolved.parameter_change_count == 18
            and independent_resolved.estimate().best_case_s == 64,
            str(independent_resolved.estimate().model_dump()))
    independent.settings.condition_settle_mode = "current"
    c.check("current settling gates a changed first sweep point",
            resolve_plan(independent, cfg).estimate().best_case_s == 73
            and resolve_plan(independent, cfg).estimate().startup_timeout_case_s == 140)

    huge = SweepAxis(target="mfc:ar", mode="linear",
                     start=0, stop=1_000_000, step=1)
    c.check("large linear axes stay indexable and bounded in preview",
            huge.count == 1_000_001
            and preview_values(huge, 3) == [0.0, 1.0, 2.0])

    c.section("fail-closed plan validation")
    missing = plan.model_copy(deep=True)
    missing.axes = [axis for axis in missing.axes if axis.target != "mfc:mfc1"]
    c.check("every background MFC is resolved", raises(
        lambda: resolve_plan(missing, cfg), "missing HCPES axis"))
    locked_ar = plan.model_copy(deep=True)
    locked_ar.axes[0] = SweepAxis(target="mfc:ar", mode="locked_zero")
    locked_ar.initial_setpoints.pop("mfc:ar")
    c.check("Ar cannot use background locked-zero mode", raises(
        lambda: resolve_plan(locked_ar, cfg), "cannot be locked"))
    c.check("partial linear endpoints are rejected", raises(
        lambda: SweepAxis(target="mfc:ar", mode="linear",
                          start=0, stop=1, step=0.3), "exactly"))
    c.check("initial block must cover every active control", raises(
        lambda: HcpesPlan.model_validate({
            **plan_payload(), "initial_setpoints": {"mfc:ar": 5},
        }), "must match every non-locked axis"))
    c.check("stability window must fit maximum wait", raises(
        lambda: StabilityProfile(
            stable_window_s=40, maximum_wait_s=30,
            max_drift_a_per_min=0.0001), "cover the full"))
    c.check("deadline includes trend acquisition plus stable hold", raises(
        lambda: StabilityProfile(
            trend_window_s=3, stable_window_s=2, maximum_wait_s=4,
            max_drift_a_per_min=0.0001), "cover the full"))
    migrated = HcpesSettings.model_validate({
        "stability_window_s": 7,
        "stability_max_wait_s": 9,
        "max_drift_a_per_min": 0.002,
    })
    c.check("saved flat settings migrate into establishment only",
            migrated.establishment.stable_window_s == 7
            and migrated.establishment.trend_window_s == 2
            and migrated.establishment.maximum_wait_s == 9
            and migrated.establishment.max_drift_a_per_min == 0.002
            and migrated.parameter_change == HcpesSettings().parameter_change)
    cadence_migrated = HcpesSettings.model_validate({"sample_interval_s": 12})
    c.check("obsolete sample interval is ignored when loading saved plans",
            "sample_interval_s" not in cadence_migrated.model_dump())
    c.check("unknown schema versions fail", raises(
        lambda: HcpesPlan.model_validate({**plan_payload(), "schema_version": 999}),
        "Input should be 1"))
    unsupported_prestart = plan.model_copy(
        update={"prestart_recipe_id": "future-hcpes-prestart"})
    c.check("unknown pre-start profiles fail closed", raises(
        lambda: resolve_plan(unsupported_prestart, cfg),
        "unsupported HCPES pre-start profile"))

    c.section("linked polarity campaigns")
    negative = clone_opposite_polarity(
        plan, new_id="cathode-map-negative", new_name="Cathode map negative")
    resolved_negative = resolve_plan(negative, cfg)
    c.check("opposite clone changes only identity and orientation",
            negative.stage_polarity == -1 and negative.revision == 1
            and resolved.compatibility_signature
            == resolved_negative.compatibility_signature)
    negative_points = list(resolved_negative.iter_points())
    c.check("physical magnitude is preserved and recorded value is signed",
            negative_points[2].setpoints["supply:stage_bias"] == 10
            and negative_points[2].signed_stage_bias_v == -10)
    signature = resolved.compatibility_signature
    campaign = PolarityCampaign(
        id="cathode-polarities", name="Cathode polarities",
        sessions=[
            CampaignSession(session_id="positive-run", polarity=1,
                            plan_signature=signature),
            CampaignSession(session_id="negative-run", polarity=-1,
                            plan_signature=signature),
        ],
    )
    c.check("matched opposite sessions complete a campaign", campaign.complete)
    c.check("mismatched plans cannot be silently stitched", raises(
        lambda: PolarityCampaign(
            id="bad-campaign", name="Bad campaign",
            sessions=[
                CampaignSession(session_id="positive-run", polarity=1,
                                plan_signature="0" * 64),
                CampaignSession(session_id="negative-run", polarity=-1,
                                plan_signature="1" * 64),
            ]), "matching HCPES plans"))

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
