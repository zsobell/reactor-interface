"""HCPES bundles preserve raw context and analysis-ready qualified data."""
from __future__ import annotations

import asyncio
import csv
import json
import sys

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
    HcpesStabilityGate,
    write_hcpes_campaign,
)
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


def plan(cfg) -> HcpesPlan:
    axes = []
    for mfc in cfg.mfcs:
        axes.append({
            "target": f"mfc:{mfc.id}",
            "mode": "fixed" if mfc.id == "ar" else "locked_zero",
            **({"value": 1.0} if mfc.id == "ar" else {}),
        })
    axes.extend([
        {"target": "supply:stage_bias", "mode": "list", "values": [0, 10]},
        {"target": "supply:collimating", "mode": "fixed", "value": 1.5},
        {"target": "supply:steering", "mode": "fixed", "value": 0.4},
        {"target": "supply:grid_bias", "mode": "fixed", "value": 100},
    ])
    return HcpesPlan(id="recording-test", name="Recording test", axes=axes)


async def main() -> int:
    c = Checker("test_hcpes_recording")
    async with VirtualReactor() as vr:
        resolved = resolve_plan(plan(vr.sup.cfg), vr.sup.cfg)
        point = next(resolved.iter_points())
        root = await vr.sup.recording.start_hcpes_session(
            resolved, "positive-001", 1_700_000_000.0)

        c.section("ordered raw and qualified streams")
        excluded = HcpesObservation(
            point_index=1, captured_at=1_700_000_020.0, elapsed_s=20,
            phase="reignite", reason="plasma below HCPES threshold",
            setpoints=point.setpoints,
            measurements={"inst.ammeter": 0.00005}, qualified=False,
            exclusion_reason="reignite",
            flags={"settled": False, "plasma_present": False},
        )
        qualified = HcpesObservation(
            point_index=1, captured_at=1_700_000_023.0, elapsed_s=23,
            phase="acquire", setpoints=point.setpoints,
            measurements={"inst.ammeter": 0.0002, "pressure": 1.2e-5,
                          "aperture_lifetime_s": 123.25},
            qualified=True, qualified_index=1,
            flags={"settled": True, "plasma_present": True},
        )
        c.check("raw exclusion accepted",
                vr.sup.recording.submit_hcpes_observation(excluded))
        c.check("qualified observation accepted once",
                vr.sup.recording.submit_hcpes_observation(qualified))
        await vr.sup.recording.drain()

        raw_lines = root.joinpath("raw.jsonl").read_text(encoding="utf-8").splitlines()
        qualified_lines = root.joinpath("qualified.jsonl").read_text(
            encoding="utf-8").splitlines()
        raw = [json.loads(line) for line in raw_lines]
        c.check("rows are durable before session close",
                len(raw) == 2 and len(qualified_lines) == 1)
        c.check("raw preserves exact phase and exclusion reason",
                raw[0]["phase"] == "reignite"
                and raw[0]["exclusion_reason"] == "reignite"
                and raw[0]["measurements"]["inst.ammeter"] == 0.00005)
        kept = json.loads(qualified_lines[0])
        c.check("qualified row points back to raw sequence",
                kept["sequence"] == 2 and kept["qualified_index"] == 1)

        c.section("point summary and manifest")
        summary = HcpesPointSummary(
            point_index=1, started_elapsed_s=20, ended_elapsed_s=26,
            setpoints=point.setpoints,
            signed_stage_bias_v=point.signed_stage_bias_v,
            requested_qualified_samples=5, actual_qualified_samples=1,
            excluded_counts={"reignite": 1},
            stage_current_mean_a=0.0002, stage_current_stddev_a=0,
            stage_current_min_a=0.0002, stage_current_max_a=0.0002,
            settled=False, observed_drift_a_per_min=0.0012,
            accessibility="partial", recovery_count=1,
            stability_gates=[HcpesStabilityGate(
                profile="establishment", outcome="timeout", elapsed_s=60,
                stable_window_s=20, maximum_wait_s=60,
                max_drift_a_per_min=0.0001,
                observed_drift_a_per_min=0.0012,
            )],
            measurement_stats={
                "inst.ammeter": {
                    "count": 1, "mean": 0.0002, "stddev": 0,
                    "minimum": 0.0002, "maximum": 0.0002,
                },
                "pressure": {
                    "count": 1, "mean": 1.2e-5, "stddev": 0,
                    "minimum": 1.2e-5, "maximum": 1.2e-5,
                },
                "aperture_lifetime_s": {
                    "count": 1, "mean": 123.25, "stddev": 0,
                    "minimum": 123.25, "maximum": 123.25,
                },
            },
            note="aborted after first accepted sample",
        )
        c.check("point summary accepted", vr.sup.recording.submit_hcpes_point(summary))
        await vr.sup.recording.finish_hcpes_session(
            "aborted", 1_700_000_030.0, "operator abort")
        rows = list(csv.DictReader(root.joinpath("points.csv").open(
            encoding="utf-8", newline="")))
        c.check("summary retains settle/accessibility/recovery metadata",
                len(rows) == 1 and rows[0]["settled"] == "False"
                and rows[0]["accessibility"] == "partial"
                and rows[0]["recovery_count"] == "1"
                and json.loads(rows[0]["stability_gates"])[0]["profile"]
                == "establishment"
                and json.loads(rows[0]["excluded_counts"])["reignite"] == 1)
        c.check("readable point table exposes elapsed time and core telemetry",
                rows[0]["started_elapsed_s"] == "20.0"
                and rows[0]["ended_elapsed_s"] == "26.0"
                and float(rows[0]["chamber_pressure_mean_torr"]) == 1.2e-5
                and float(rows[0]["aperture_lifetime_mean_s"]) == 123.25
                and rows[0]["stage_temperature_mean_c"] == "")
        channel_rows = [json.loads(line) for line in root.joinpath(
            "point_channels.jsonl").read_text(encoding="utf-8").splitlines()]
        c.check("all-channel point file retains nested numeric summaries",
                len(channel_rows) == 1
                and channel_rows[0]["point_index"] == 1
                and channel_rows[0]["channels"]["pressure"] == {
                    "count": 1, "mean": 1.2e-5, "stddev": 0.0,
                    "minimum": 1.2e-5, "maximum": 1.2e-5,
                }
                and channel_rows[0]["channels"]["aperture_lifetime_s"] == {
                    "count": 1, "mean": 123.25, "stddev": 0.0,
                    "minimum": 123.25, "maximum": 123.25,
                })
        timeline = list(csv.DictReader(root.joinpath("timeline.csv").open(
            encoding="utf-8", newline="")))
        readable_points = list(yaml.safe_load_all(root.joinpath(
            "points.yaml").read_text(encoding="utf-8")))
        summary_text = root.joinpath("run_summary.txt").read_text(encoding="utf-8")
        c.check("human-readable files explain the run without decoding JSONL",
                len(timeline) == 2
                and timeline[0]["phase"] == "reignite"
                and timeline[0]["stage_current_mA"] == "0.05"
                and readable_points[0]["point"] == 1
                and readable_points[0]["core_telemetry_means"]
                    ["aperture_lifetime_s"] == 123.25
                and readable_points[0]["outcome"]["stability_gates"][0]["outcome"]
                == "timeout"
                and readable_points[0]["outcome"]["stability_gates"][0]
                    ["max_drift_mA_per_min"] == 0.1
                and readable_points[0]["collection"]["stage_current_mA"]["mean"]
                    == 0.2
                and readable_points[0]["setpoints"]["supply:collimating"]["unit"]
                    == "mA"
                and "Inaccessible or partial conditions: 1" in summary_text
                and "Never-settled conditions: 1" in summary_text)
        manifest = yaml.safe_load(root.joinpath("manifest.yaml").read_text(
            encoding="utf-8"))
        c.check("manifest is readable and final",
                manifest["session"]["status"] == "aborted"
                and manifest["session"]["error"] == "operator abort"
                and manifest["counts"] == {
                    "raw": 2, "qualified": 1, "points": 1,
                    "point_channels": 1, "timeline": 2,
                })
        c.check("recording status exposes completed bundle",
                vr.sup.recording.status()["hcpes"]["status"] == "aborted")

        c.section("immutable linked-polarity campaign")
        negative_plan = clone_opposite_polarity(
            resolved.plan, new_id="recording-test-negative",
            new_name="Recording test negative")
        negative = resolve_plan(negative_plan, vr.sup.cfg)
        negative_point = next(negative.iter_points())
        negative_root = await vr.sup.recording.start_hcpes_session(
            negative, "negative-001", 1_700_000_100.0)
        negative_summary = summary.model_copy(update={
            "setpoints": negative_point.setpoints,
            "signed_stage_bias_v": negative_point.signed_stage_bias_v,
            "actual_qualified_samples": 0,
            "excluded_counts": {"inaccessible": 1},
            "stage_current_mean_a": None,
            "stage_current_stddev_a": None,
            "stage_current_min_a": None,
            "stage_current_max_a": None,
            "measurement_stats": {},
            "accessibility": "inaccessible",
            "note": "condition never recovered",
        })
        vr.sup.recording.submit_hcpes_point(negative_summary)
        await vr.sup.recording.finish_hcpes_session(
            "complete", 1_700_000_130.0)
        campaign = PolarityCampaign(
            id="linked-test", name="Linked test",
            sessions=[
                CampaignSession(
                    session_id="positive-001", polarity=1,
                    plan_signature=resolved.compatibility_signature),
                CampaignSession(
                    session_id="negative-001", polarity=-1,
                    plan_signature=negative.compatibility_signature),
            ],
        )
        campaign_paths = write_hcpes_campaign(
            vr.sup.logger.dir, campaign,
            {"positive-001": root, "negative-001": negative_root},
            1_700_000_200.0)
        combined = list(csv.DictReader(campaign_paths.combined_points.open(
            encoding="utf-8", newline="")))
        c.check("campaign does not alter either source bundle",
                root.joinpath("manifest.yaml").exists()
                and negative_root.joinpath("manifest.yaml").exists())
        c.check("duplicate zero is preserved with negative session first",
                len(combined) == 2
                and [row["source_polarity"] for row in combined] == ["-1", "1"]
                and all(float(row["signed_stage_bias_v"]) == 0 for row in combined))
        campaign_doc = yaml.safe_load(campaign_paths.manifest.read_text(
            encoding="utf-8"))
        c.check("campaign manifest links sources and derived table",
                campaign_doc["campaign"]["complete"] is True
                and campaign_doc["derived"]["row_count"] == 2
                and len(campaign_doc["sessions"]) == 2)

    c.section("validation prevents ambiguous filtered data")
    c.check("excluded rows cannot carry a qualified index", _raises(
        lambda: HcpesObservation(
            captured_at=1, elapsed_s=0, phase="pause", qualified_index=1),
        "excluded observations"))
    c.check("zero-sample point cannot claim statistics", _raises(
        lambda: HcpesPointSummary(
            point_index=1, setpoints={}, signed_stage_bias_v=0,
            requested_qualified_samples=5, actual_qualified_samples=0,
            stage_current_mean_a=1, settled=False,
            accessibility="inaccessible"),
        "cannot have statistics"))
    c.check("zero-sample point cannot claim telemetry", _raises(
        lambda: HcpesPointSummary(
            point_index=1, setpoints={}, signed_stage_bias_v=0,
            requested_qualified_samples=5, actual_qualified_samples=0,
            settled=False, accessibility="inaccessible",
            measurement_stats={"pressure": {
                "count": 1, "mean": 1, "stddev": 0,
                "minimum": 1, "maximum": 1,
            }}),
        "cannot have telemetry"))
    return c.summary()


def _raises(fn, text: str) -> bool:
    try:
        fn()
    except (TypeError, ValueError) as exc:
        return text in str(exc)
    return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
