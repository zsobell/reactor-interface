"""Read-only HCPES analysis loads sessions, campaigns, telemetry, and raw flags."""
from __future__ import annotations

import asyncio
import sys

from fastapi import FastAPI
import yaml

from reactor.control.hcpes_model import (
    CampaignSession,
    HcpesPlan,
    PolarityCampaign,
    clone_opposite_polarity,
    resolve_plan,
)
from reactor.hcpes_recording import (
    HcpesObservation,
    HcpesPointSummary,
    HcpesSessionWriter,
    HcpesStabilityGate,
    write_hcpes_campaign,
)
from reactor.server.hcpes_analysis import (
    HcpesAnalysisFiles,
    create_hcpes_analysis_router,
)
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, request


def plan(cfg) -> HcpesPlan:
    axes = [
        {"target": "mfc:ar", "mode": "fixed", "value": 1},
        {"target": "supply:stage_bias", "mode": "list", "values": [0, 10]},
        {"target": "supply:collimating", "mode": "fixed", "value": 1.5},
        {"target": "supply:steering", "mode": "fixed", "value": 0.4},
        {"target": "supply:grid_bias", "mode": "fixed", "value": 100},
    ]
    axes.extend(
        {"target": f"mfc:{mfc.id}", "mode": "locked_zero"}
        for mfc in cfg.mfcs if mfc.id != "ar")
    return HcpesPlan(id="analysis", name="Analysis test", axes=axes)


def write_session(root, resolved, session_id, *, status="complete"):
    writer = HcpesSessionWriter(root, resolved, session_id, 1_700_000_000)
    for point in resolved.iter_points():
        elapsed = 20 + point.index
        writer.write_observation(HcpesObservation(
            point_index=point.index, captured_at=1_700_000_000 + elapsed,
            elapsed_s=elapsed - 0.5, phase="settle", reason="current gate",
            setpoints=point.setpoints,
            measurements={"inst.ammeter": 0.001, "pressure": 1e-5},
            exclusion_reason="stability_window"))
        writer.write_observation(HcpesObservation(
            point_index=point.index, captured_at=1_700_000_000 + elapsed,
            elapsed_s=elapsed, phase="acquire", reason="qualified",
            setpoints=point.setpoints,
            measurements={
                "inst.ammeter": 0.001 * point.index,
                "pressure": 1e-5 * point.index,
                "stage.temp": 100 + point.index,
            },
            qualified=True, qualified_index=1))
        writer.write_point(HcpesPointSummary(
            point_index=point.index,
            started_elapsed_s=elapsed - 1, ended_elapsed_s=elapsed,
            setpoints=point.setpoints,
            signed_stage_bias_v=point.signed_stage_bias_v,
            requested_qualified_samples=1, actual_qualified_samples=1,
            stage_current_mean_a=0.001 * point.index,
            stage_current_stddev_a=0,
            stage_current_min_a=0.001 * point.index,
            stage_current_max_a=0.001 * point.index,
            settled=point.index == 1,
            observed_drift_a_per_min=0 if point.index == 1 else 0.002,
            accessibility="accessible",
            stability_gates=[HcpesStabilityGate(
                profile="establishment" if point.index == 1 else "parameter_change",
                outcome="settled" if point.index == 1 else "timeout",
                elapsed_s=20 if point.index == 1 else 10,
                stable_window_s=20 if point.index == 1 else 3,
                maximum_wait_s=60 if point.index == 1 else 10,
                max_drift_a_per_min=0.0001 if point.index == 1 else 0.0003,
                observed_drift_a_per_min=0 if point.index == 1 else 0.002,
            )],
            measurement_stats={
                "inst.ammeter": {
                    "count": 1, "mean": 0.001 * point.index, "stddev": 0,
                    "minimum": 0.001 * point.index,
                    "maximum": 0.001 * point.index,
                },
                "pressure": {
                    "count": 1, "mean": 1e-5 * point.index, "stddev": 0,
                    "minimum": 1e-5 * point.index,
                    "maximum": 1e-5 * point.index,
                },
                "stage.temp": {
                    "count": 1, "mean": 100 + point.index, "stddev": 0,
                    "minimum": 100 + point.index,
                    "maximum": 100 + point.index,
                },
            }))
    writer.finish(status, 1_700_000_100)
    return writer.paths.directory


async def main() -> int:
    c = Checker("test_hcpes_analysis")
    async with VirtualReactor() as vr:
        root = vr.sup.logger.dir
        positive = resolve_plan(plan(vr.sup.cfg), vr.sup.cfg)
        negative = resolve_plan(clone_opposite_polarity(
            positive.plan, new_id="analysis-negative",
            new_name="Analysis negative"), vr.sup.cfg)
        positive_dir = write_session(root, positive, "analysis-positive")
        negative_dir = write_session(root, negative, "analysis-negative")
        partial_dir = write_session(root, positive, "analysis-partial", status="aborted")
        campaign = PolarityCampaign(
            id="analysis-linked", name="Analysis linked",
            sessions=[
                CampaignSession(
                    session_id="analysis-positive", polarity=1,
                    plan_signature=positive.compatibility_signature),
                CampaignSession(
                    session_id="analysis-negative", polarity=-1,
                    plan_signature=negative.compatibility_signature),
            ])
        campaign_paths = write_hcpes_campaign(
            root, campaign,
            {"analysis-positive": positive_dir, "analysis-negative": negative_dir},
            1_700_000_200)

        files = HcpesAnalysisFiles(root)
        app = FastAPI()
        app.include_router(create_hcpes_analysis_router(files))

        c.section("source discovery and typed point data")
        status, data = await request(app, "/api/hcpes/analysis/sources")
        c.check("sessions, partial bundle, and campaign are discoverable",
                status == 200 and len(data["sources"]) == 4
                and any(row["status"] == "aborted" for row in data["sources"])
                and any(row["kind"] == "campaign" for row in data["sources"]))
        partial_name = partial_dir.relative_to(root).as_posix()
        status, data = await request(
            app, f"/api/hcpes/analysis/source?name={partial_name}")
        point = data["points"][0]
        c.check("partial sessions retain typed summaries and all-channel stats",
                status == 200 and data["kind"] == "session"
                and data["session"]["status"] == "aborted"
                and point["point_index"] == 1
                and point["settled"] is True
                and point["stability_gates"][0]["profile"] == "establishment"
                and point["channel_stats"]["stage.temp"]["mean"] == 101)

        c.section("linked polarity campaign")
        campaign_name = campaign_paths.directory.relative_to(root).as_posix()
        status, data = await request(
            app, f"/api/hcpes/analysis/source?name={campaign_name}")
        c.check("compatible campaign loads as one signed series",
                status == 200 and data["compatible"] is True
                and [row["signed_stage_bias_v"] for row in data["points"]]
                    == [-10, 0, 0, 10]
                and [row["source_polarity"] for row in data["points"]][1:3]
                    == [-1, 1])
        c.check("campaign points retain source and telemetry provenance",
                all(row["source_session"] and row["source_point"]
                    and row["channel_stats"].get("pressure")
                    for row in data["points"])
                and len({row["acquisition_order"] for row in data["points"]}) == 4)

        c.section("raw inspector and containment")
        status, data = await request(
            app, "/api/hcpes/analysis/raw?"
            f"name={campaign_name}&session_id=analysis-positive&mode=omitted")
        c.check("raw inspector exposes filtered intervals and reasons",
                status == 200 and data["matched"] == 2
                and all(row["exclusion_reason"] == "stability_window"
                        for row in data["rows"]))
        status, _ = await request(
            app, "/api/hcpes/analysis/source?name=../outside")
        c.check("source path escape is refused", status == 404)

        c.section("incompatible campaign is flagged")
        manifest = yaml.safe_load(campaign_paths.manifest.read_text(encoding="utf-8"))
        manifest["sessions"][1]["plan_signature"] = "tampered"
        campaign_paths.manifest.write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        status, data = await request(
            app, f"/api/hcpes/analysis/source?name={campaign_name}")
        c.check("mismatched sources are not silently presented as compatible",
                status == 200 and data["compatible"] is False
                and data["compatibility_issues"])
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
