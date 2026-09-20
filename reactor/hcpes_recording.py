"""Crash-resilient, human-readable HCPES characterization bundles.

The writer is synchronous by design.  ``RecordingService`` owns it on the
existing single recording worker, so control timing never waits for disk and
all accepted rows retain one deterministic order.
"""
from __future__ import annotations

import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from .control.hcpes_model import PolarityCampaign, ResolvedHcpesPlan


SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
HcpesCompletionStatus = Literal["complete", "aborted", "failed", "interrupted"]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(timespec="milliseconds")


def _safe_id(value: str) -> str:
    cleaned = _SAFE_ID.sub("_", value).strip("._ ")[:80]
    if not cleaned:
        raise ValueError("HCPES session id must contain a filename-safe character")
    return cleaned


class HcpesObservation(BaseModel):
    """One raw interval; qualified observations are mirrored automatically."""

    model_config = ConfigDict(extra="forbid")

    point_index: int | None = Field(default=None, ge=1)
    captured_at: float
    elapsed_s: float = Field(ge=0)
    phase: Literal[
        "setup", "parameter_change", "settle", "acquire", "pause",
        "plasma_loss", "reignite", "recovery", "inaccessible", "cleanup",
        "complete", "abort", "error",
    ]
    reason: str = ""
    changed_target: str | None = None
    requested_value: float | None = None
    actual_value: float | None = None
    setpoints: dict[str, float] = Field(default_factory=dict)
    measurements: dict[str, Any] = Field(default_factory=dict)
    qualified: bool = False
    qualified_index: int | None = Field(default=None, ge=1)
    exclusion_reason: str | None = None
    flags: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _qualified_shape(self) -> "HcpesObservation":
        if self.qualified:
            if self.phase != "acquire":
                raise ValueError("only acquisition observations may be qualified")
            if self.point_index is None or self.qualified_index is None:
                raise ValueError("qualified observations require point and sample indexes")
            if self.exclusion_reason:
                raise ValueError("a qualified observation cannot have an exclusion reason")
        elif self.qualified_index is not None:
            raise ValueError("excluded observations cannot have a qualified sample index")
        return self


class NumericChannelSummary(BaseModel):
    """Compact statistics for one numeric snapshot channel at one condition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=1)
    mean: float
    stddev: float = Field(ge=0)
    minimum: float
    maximum: float

    @model_validator(mode="after")
    def _valid_range(self) -> "NumericChannelSummary":
        values = (self.mean, self.stddev, self.minimum, self.maximum)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("numeric channel statistics must be finite")
        tolerance = max(
            1e-15,
            max(abs(self.minimum), abs(self.mean), abs(self.maximum)) * 1e-12,
        )
        if (self.mean < self.minimum - tolerance
                or self.mean > self.maximum + tolerance):
            raise ValueError("numeric channel mean must be within its range")
        return self


class HcpesStabilityGate(BaseModel):
    """Analysis-visible result of one establishment or parameter stability gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["establishment", "parameter_change"]
    outcome: Literal["settled", "timeout", "plasma_lost"]
    elapsed_s: float = Field(ge=0)
    stable_window_s: float = Field(gt=0)
    maximum_wait_s: float = Field(gt=0)
    max_drift_a_per_min: float = Field(gt=0)
    observed_drift_a_per_min: float | None = None


class HcpesPointSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_index: int = Field(ge=1)
    started_elapsed_s: float | None = Field(default=None, ge=0)
    ended_elapsed_s: float | None = Field(default=None, ge=0)
    setpoints: dict[str, float]
    signed_stage_bias_v: float
    requested_qualified_samples: int = Field(ge=1)
    actual_qualified_samples: int = Field(ge=0)
    excluded_counts: dict[str, int] = Field(default_factory=dict)
    stage_current_mean_a: float | None = None
    stage_current_stddev_a: float | None = None
    stage_current_min_a: float | None = None
    stage_current_max_a: float | None = None
    settled: bool
    observed_drift_a_per_min: float | None = None
    accessibility: Literal["accessible", "recovered", "inaccessible", "partial"]
    recovery_count: int = Field(default=0, ge=0)
    stability_gates: list[HcpesStabilityGate] = Field(default_factory=list)
    measurement_stats: dict[str, NumericChannelSummary] = Field(default_factory=dict)
    note: str = ""

    @model_validator(mode="after")
    def _counts_agree(self) -> "HcpesPointSummary":
        if any(count < 0 for count in self.excluded_counts.values()):
            raise ValueError("excluded counts must be nonnegative")
        if self.actual_qualified_samples > self.requested_qualified_samples:
            raise ValueError("actual qualified samples exceed the requested count")
        stats = (
            self.stage_current_mean_a, self.stage_current_stddev_a,
            self.stage_current_min_a, self.stage_current_max_a,
        )
        if self.actual_qualified_samples == 0 and any(v is not None for v in stats):
            raise ValueError("a point without qualified samples cannot have statistics")
        if self.actual_qualified_samples and any(v is None for v in stats):
            raise ValueError("a point with qualified samples requires all statistics")
        if (self.started_elapsed_s is not None and self.ended_elapsed_s is not None
                and self.ended_elapsed_s < self.started_elapsed_s):
            raise ValueError("point end cannot precede its start")
        if self.actual_qualified_samples == 0 and self.measurement_stats:
            raise ValueError("a point without qualified samples cannot have telemetry")
        if any(channel.count > self.actual_qualified_samples
               for channel in self.measurement_stats.values()):
            raise ValueError("telemetry count exceeds qualified sample count")
        return self


class HcpesBundlePaths(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: Path
    manifest: Path
    raw: Path
    qualified: Path
    points: Path
    point_channels: Path
    timeline: Path
    readable_points: Path
    summary: Path


class HcpesCampaignPaths(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: Path
    manifest: Path
    combined_points: Path


class HcpesSessionWriter:
    """Worker-owned writer that flushes every completed record."""

    def __init__(
        self,
        root: Path,
        resolved: ResolvedHcpesPlan,
        session_id: str,
        started_at: float,
    ) -> None:
        self.resolved = resolved
        self.session_id = session_id
        self.started_at = started_at
        self.status = "active"
        self.raw_rows = 0
        self.qualified_rows = 0
        self.point_rows = 0
        self.point_channel_rows = 0
        self.timeline_rows = 0
        self._summaries: list[HcpesPointSummary] = []
        folder = Path(root).resolve() / f"HCPES-{_safe_id(session_id)}"
        if folder.exists():
            raise FileExistsError(f"HCPES session directory already exists: {folder}")
        folder.mkdir(parents=True)
        self.paths = HcpesBundlePaths(
            directory=folder,
            manifest=folder / "manifest.yaml",
            raw=folder / "raw.jsonl",
            qualified=folder / "qualified.jsonl",
            points=folder / "points.csv",
            point_channels=folder / "point_channels.jsonl",
            timeline=folder / "timeline.csv",
            readable_points=folder / "points.yaml",
            summary=folder / "run_summary.txt",
        )
        self._raw_fh = self.paths.raw.open("x", encoding="utf-8", newline="")
        self._qualified_fh = self.paths.qualified.open(
            "x", encoding="utf-8", newline="")
        self._points_fh = self.paths.points.open("x", encoding="utf-8", newline="")
        self._point_channels_fh = self.paths.point_channels.open(
            "x", encoding="utf-8", newline="")
        self._timeline_fh = self.paths.timeline.open("x", encoding="utf-8", newline="")
        self._readable_points_fh = self.paths.readable_points.open(
            "x", encoding="utf-8", newline="")
        self._point_fields = [
            "point_index", "started_elapsed_s", "ended_elapsed_s",
            "signed_stage_bias_v",
            *(f"setpoint:{axis.target}" for axis in resolved.plan.axes),
            "requested_qualified_samples", "actual_qualified_samples",
            "excluded_counts", "stage_current_mean_a", "stage_current_stddev_a",
            "stage_current_min_a", "stage_current_max_a", "settled",
            "observed_drift_a_per_min", "accessibility", "recovery_count", "note",
            "stability_gates",
            "chamber_pressure_mean_torr", "stage_temperature_mean_c",
            "aperture_lifetime_mean_s",
            "ar_baratron_mean_torr", "hv_voltage_mean_v", "hv_current_mean_ma",
        ]
        self._point_csv = csv.DictWriter(self._points_fh, fieldnames=self._point_fields)
        self._point_csv.writeheader()
        self._points_fh.flush()
        self._timeline_fields = [
            "sequence", "elapsed_s", "iso_time", "point_index", "phase", "reason",
            "changed_target", "requested_value", "requested_unit", "stage_current_mA", "qualified",
            "qualified_index", "exclusion_reason", "plasma_present",
            "stability_profile", "retry_remaining_s", "settle_remaining_s",
        ]
        self._timeline_csv = csv.DictWriter(
            self._timeline_fh, fieldnames=self._timeline_fields)
        self._timeline_csv.writeheader()
        self._timeline_fh.flush()
        self._readable_points_fh.write(
            "# HCPES condition summaries. Each --- section is one attempted condition.\n")
        self._readable_points_fh.flush()
        self._write_summary()
        self._write_manifest()

    def _manifest(self, *, ended_at: float | None = None, error: str = "") -> dict:
        data = {
            "schema_version": SCHEMA_VERSION,
            "kind": "hcpes_characterization_session",
            "session": {
                "id": self.session_id,
                "status": self.status,
                "started_at_epoch": self.started_at,
                "started_at": _iso(self.started_at),
                "stage_polarity": self.resolved.plan.stage_polarity,
                "plan_signature": self.resolved.compatibility_signature,
            },
            "plan": self.resolved.plan.model_dump(mode="json", exclude_none=True),
            "files": {
                "raw": {
                    "path": self.paths.raw.name,
                    "format": "json-lines",
                    "purpose": "Every interval, transition, exclusion, and recovery attempt",
                },
                "qualified": {
                    "path": self.paths.qualified.name,
                    "format": "json-lines",
                    "purpose": "Acquisition observations accepted into point statistics",
                },
                "points": {
                    "path": self.paths.points.name,
                    "format": "csv",
                    "purpose": "One analysis-ready row per attempted condition",
                },
                "point_channels": {
                    "path": self.paths.point_channels.name,
                    "format": "json-lines",
                    "purpose": (
                        "Nested count/mean/stddev/min/max for every numeric telemetry "
                        "channel during each condition's qualified collection"
                    ),
                },
                "timeline": {
                    "path": self.paths.timeline.name,
                    "format": "csv",
                    "purpose": "Concise human-readable chronological event and timer log",
                },
                "readable_points": {
                    "path": self.paths.readable_points.name,
                    "format": "yaml-multi-document",
                    "purpose": "Human-readable condition-by-condition subsections",
                },
                "summary": {
                    "path": self.paths.summary.name,
                    "format": "plain-text",
                    "purpose": "At-a-glance plan, status, counts, and notable conditions",
                },
            },
            "counts": {
                "raw": self.raw_rows,
                "qualified": self.qualified_rows,
                "points": self.point_rows,
                "point_channels": self.point_channel_rows,
                "timeline": self.timeline_rows,
            },
        }
        if ended_at is not None:
            data["session"].update(ended_at_epoch=ended_at, ended_at=_iso(ended_at))
        if error:
            data["session"]["error"] = error
        return data

    def _write_manifest(self, *, ended_at: float | None = None, error: str = "") -> None:
        # Replace keeps a complete old or new manifest if the process stops mid-write.
        temporary = self.paths.manifest.with_suffix(".yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(self._manifest(ended_at=ended_at, error=error),
                           sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        temporary.replace(self.paths.manifest)

    @staticmethod
    def _json_line(data: dict[str, Any]) -> str:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False) + "\n"

    def _display_setpoint(self, target: str | None, value: Any) -> tuple[Any, str]:
        capability = next(
            (item for item in self.resolved.capabilities if item.target == target), None)
        if capability is None:
            return value, ""
        if capability.quantity == "current" and isinstance(value, (int, float)):
            return float(value) * 1000, "mA"
        return value, capability.unit

    def _readable_setpoints(self, setpoints: dict[str, float]) -> dict[str, Any]:
        readable: dict[str, Any] = {}
        for target, raw_value in setpoints.items():
            value, unit = self._display_setpoint(target, raw_value)
            capability = next(
                (item for item in self.resolved.capabilities if item.target == target), None)
            readable[target] = {
                "label": capability.label if capability is not None else target,
                "value": value,
                "unit": unit,
            }
        return readable

    @staticmethod
    def _readable_gate(gate: HcpesStabilityGate) -> dict[str, Any]:
        row = gate.model_dump(mode="json")
        maximum = row.pop("max_drift_a_per_min", None)
        observed = row.pop("observed_drift_a_per_min", None)
        row["max_drift_mA_per_min"] = (
            maximum * 1000 if maximum is not None else None)
        row["observed_drift_mA_per_min"] = (
            observed * 1000 if observed is not None else None)
        return row

    def write_observation(self, observation: HcpesObservation) -> None:
        if self.status != "active":
            raise RuntimeError("HCPES session is not active")
        record = observation.model_dump(mode="json", exclude_none=True)
        record.update(
            schema_version=SCHEMA_VERSION,
            session_id=self.session_id,
            sequence=self.raw_rows + 1,
            captured_at_iso=_iso(observation.captured_at),
        )
        sequence = self.raw_rows + 1
        line = self._json_line(record)
        self._raw_fh.write(line)
        self._raw_fh.flush()
        self.raw_rows += 1
        flags = observation.flags
        current = observation.measurements.get("inst.ammeter")
        requested_value, requested_unit = self._display_setpoint(
            observation.changed_target, observation.requested_value)
        self._timeline_csv.writerow({
            "sequence": sequence,
            "elapsed_s": observation.elapsed_s,
            "iso_time": _iso(observation.captured_at),
            "point_index": observation.point_index,
            "phase": observation.phase,
            "reason": observation.reason,
            "changed_target": observation.changed_target,
            "requested_value": requested_value,
            "requested_unit": requested_unit,
            "stage_current_mA": (
                float(current) * 1000
                if isinstance(current, (int, float)) and not isinstance(current, bool)
                else ""),
            "qualified": observation.qualified,
            "qualified_index": observation.qualified_index,
            "exclusion_reason": observation.exclusion_reason,
            "plasma_present": flags.get("plasma_present", ""),
            "stability_profile": flags.get("stability_profile", ""),
            "retry_remaining_s": flags.get("retry_remaining_s", ""),
            "settle_remaining_s": flags.get("settle_remaining_s", ""),
        })
        self._timeline_fh.flush()
        self.timeline_rows += 1
        if observation.qualified:
            self._qualified_fh.write(line)
            self._qualified_fh.flush()
            self.qualified_rows += 1

    def write_point(self, summary: HcpesPointSummary) -> None:
        if self.status != "active":
            raise RuntimeError("HCPES session is not active")
        row: dict[str, Any] = {
            "point_index": summary.point_index,
            "started_elapsed_s": summary.started_elapsed_s,
            "ended_elapsed_s": summary.ended_elapsed_s,
            "signed_stage_bias_v": summary.signed_stage_bias_v,
            **{f"setpoint:{axis.target}": summary.setpoints.get(axis.target, "")
               for axis in self.resolved.plan.axes},
            "requested_qualified_samples": summary.requested_qualified_samples,
            "actual_qualified_samples": summary.actual_qualified_samples,
            "excluded_counts": json.dumps(summary.excluded_counts, sort_keys=True,
                                           separators=(",", ":")),
            "stage_current_mean_a": summary.stage_current_mean_a,
            "stage_current_stddev_a": summary.stage_current_stddev_a,
            "stage_current_min_a": summary.stage_current_min_a,
            "stage_current_max_a": summary.stage_current_max_a,
            "settled": summary.settled,
            "observed_drift_a_per_min": summary.observed_drift_a_per_min,
            "accessibility": summary.accessibility,
            "recovery_count": summary.recovery_count,
            "note": summary.note,
            "stability_gates": json.dumps(
                [gate.model_dump(mode="json") for gate in summary.stability_gates],
                sort_keys=True, separators=(",", ":")),
            "chamber_pressure_mean_torr": _channel_mean(
                summary, "pressure"),
            "stage_temperature_mean_c": _channel_mean(
                summary, "stage.temp"),
            "aperture_lifetime_mean_s": _channel_mean(
                summary, "aperture_lifetime_s"),
            "ar_baratron_mean_torr": _channel_mean(
                summary, "gauge.ar_baratron"),
            "hv_voltage_mean_v": _channel_mean(
                summary, "hv.hv.voltage"),
            "hv_current_mean_ma": _channel_mean(
                summary, "hv.hv.current"),
        }
        self._point_csv.writerow(row)
        self._points_fh.flush()
        self.point_rows += 1
        channel_record = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "point_index": summary.point_index,
            "started_elapsed_s": summary.started_elapsed_s,
            "ended_elapsed_s": summary.ended_elapsed_s,
            "channels": {
                key: value.model_dump(mode="json")
                for key, value in sorted(summary.measurement_stats.items())
            },
        }
        self._point_channels_fh.write(self._json_line(channel_record))
        self._point_channels_fh.flush()
        self.point_channel_rows += 1
        readable = {
            "point": summary.point_index,
            "elapsed_s": {
                "start": summary.started_elapsed_s,
                "end": summary.ended_elapsed_s,
            },
            "setpoints": self._readable_setpoints(summary.setpoints),
            "signed_stage_bias_v": summary.signed_stage_bias_v,
            "outcome": {
                "accessibility": summary.accessibility,
                "settled": summary.settled,
                "recovery_attempts": summary.recovery_count,
                "stability_gates": [self._readable_gate(gate)
                                    for gate in summary.stability_gates],
            },
            "collection": {
                "requested_samples": summary.requested_qualified_samples,
                "accepted_samples": summary.actual_qualified_samples,
                "excluded_observations": summary.excluded_counts,
                "stage_current_mA": {
                    "mean": (summary.stage_current_mean_a * 1000
                             if summary.stage_current_mean_a is not None else None),
                    "stddev": (summary.stage_current_stddev_a * 1000
                               if summary.stage_current_stddev_a is not None else None),
                    "minimum": (summary.stage_current_min_a * 1000
                                if summary.stage_current_min_a is not None else None),
                    "maximum": (summary.stage_current_max_a * 1000
                                if summary.stage_current_max_a is not None else None),
                },
            },
            "core_telemetry_means": {
                "chamber_pressure_torr": _channel_mean(summary, "pressure"),
                "stage_temperature_C": _channel_mean(summary, "stage.temp"),
                "aperture_lifetime_s": _channel_mean(
                    summary, "aperture_lifetime_s"),
                "ar_baratron_torr": _channel_mean(summary, "gauge.ar_baratron"),
                "hv_voltage_V": _channel_mean(summary, "hv.hv.voltage"),
                "hv_current_mA": _channel_mean(summary, "hv.hv.current"),
            },
            "all_channel_statistics": "point_channels.jsonl",
            "note": summary.note,
        }
        self._readable_points_fh.write(yaml.safe_dump(
            readable, explicit_start=True, sort_keys=False, allow_unicode=True))
        self._readable_points_fh.flush()
        self._summaries.append(summary)
        self._write_summary()

    def finish(
        self,
        status: HcpesCompletionStatus,
        ended_at: float,
        error: str = "",
    ) -> None:
        if self.status != "active":
            return
        self.status = status
        failures: list[str] = []
        for handle in (
            self._raw_fh, self._qualified_fh, self._points_fh,
            self._point_channels_fh, self._timeline_fh, self._readable_points_fh,
        ):
            for action in (handle.flush, handle.close):
                try:
                    action()
                except Exception as exc:
                    failures.append(f"{type(exc).__name__}: {exc}")
        if failures:
            self.status = "failed"
        detail = "; ".join(part for part in (error, *failures) if part)
        self._write_summary(ended_at=ended_at, error=detail)
        self._write_manifest(ended_at=ended_at, error=detail)
        if failures:
            raise OSError("; ".join(failures))

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self.status == "active",
            "session_id": self.session_id,
            "status": self.status,
            "directory": str(self.paths.directory),
            "manifest": str(self.paths.manifest),
            "raw_rows": self.raw_rows,
            "qualified_rows": self.qualified_rows,
            "point_rows": self.point_rows,
            "point_channel_rows": self.point_channel_rows,
            "timeline_rows": self.timeline_rows,
            "summary": str(self.paths.summary),
            "timeline": str(self.paths.timeline),
            "readable_points": str(self.paths.readable_points),
        }

    def _write_summary(self, *, ended_at: float | None = None, error: str = "") -> None:
        settings = self.resolved.plan.settings
        inaccessible = [
            row.point_index for row in self._summaries
            if row.accessibility in {"inaccessible", "partial"}
        ]
        never_settled = [row.point_index for row in self._summaries if not row.settled]
        lines = [
            "HCPES CHARACTERIZATION SUMMARY",
            "==============================",
            f"Session: {self.session_id}",
            f"Status: {self.status}",
            f"Started: {_iso(self.started_at)}",
        ]
        if ended_at is not None:
            lines.append(f"Ended: {_iso(ended_at)}")
        lines.extend([
            f"Plan: {self.resolved.plan.name} (revision {self.resolved.plan.revision})",
            f"Stage wiring orientation: {'positive' if self.resolved.plan.stage_polarity > 0 else 'negative'}",
            "",
            "PLASMA ESTABLISHMENT / RE-ESTABLISHMENT",
            f"  Stable window: {settings.establishment.stable_window_s:g} s",
            f"  Maximum settle wait: {settings.establishment.maximum_wait_s:g} s",
            f"  Maximum drift: {settings.establishment.max_drift_a_per_min * 1000:g} mA/min",
            f"  Plasma minimum: {settings.plasma_min_current_a * 1000:g} mA",
            f"  Recovery retry budget: {settings.recovery_window_s:g} s",
            f"  Relay pulse / post-pulse wait: {settings.reignite_pulse_s:g} / {settings.reignite_settle_s:g} s",
            "",
            "BETWEEN PARAMETER CHANGES",
            f"  Mode: {settings.condition_settle_mode}",
            f"  Timed delay: {settings.parameter_settle_s:g} s per changed parameter",
            f"  Current stable window: {settings.parameter_change.stable_window_s:g} s",
            f"  Current maximum settle wait: {settings.parameter_change.maximum_wait_s:g} s",
            f"  Current maximum drift: {settings.parameter_change.max_drift_a_per_min * 1000:g} mA/min",
            "",
            "QUALIFIED COLLECTION",
            f"  Samples per condition: {settings.qualified_samples}",
            "  Timing: next fresh instrument readings; no added interval",
            "  Collection duration target: none",
            "",
            "PROGRESS / OUTCOMES",
            f"  Conditions recorded: {len(self._summaries)} / {self.resolved.point_count}",
            f"  Qualified samples: {self.qualified_rows}",
            f"  Inaccessible or partial conditions: {', '.join(map(str, inaccessible)) or 'none'}",
            f"  Never-settled conditions: {', '.join(map(str, never_settled)) or 'none'}",
        ])
        if error:
            lines.extend(["", f"ERROR: {error}"])
        lines.extend([
            "",
            "FILES TO OPEN",
            "  timeline.csv  - concise chronological activity",
            "  points.yaml   - condition-by-condition readable sections",
            "  points.csv    - spreadsheet-ready condition table",
            "  raw.jsonl     - complete machine-readable audit stream",
            "",
        ])
        temporary = self.paths.summary.with_suffix(".txt.tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(self.paths.summary)


def _channel_mean(summary: HcpesPointSummary, key: str) -> float | str:
    stats = summary.measurement_stats.get(key)
    return stats.mean if stats is not None else ""


def write_hcpes_campaign(
    root: Path,
    campaign: PolarityCampaign,
    session_directories: dict[str, Path],
    created_at: float,
) -> HcpesCampaignPaths:
    """Link finished immutable sessions and derive a signed point table.

    Source bundles are only read.  Duplicate zero-bias rows are deliberately
    retained, with the negative-orientation session first, so a polarity-swap
    discontinuity remains visible in analysis.
    """
    expected_ids = {session.session_id for session in campaign.sessions}
    if set(session_directories) != expected_ids:
        raise ValueError("campaign session directories do not match its session ids")

    loaded: list[tuple[Any, Path, dict[str, Any], list[dict[str, str]], list[str]]] = []
    common_fields: list[str] | None = None
    for session in campaign.sessions:
        directory = Path(session_directories[session.session_id]).resolve()
        manifest_path = directory / "manifest.yaml"
        points_path = directory / "points.csv"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        source = manifest.get("session", {})
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported HCPES session schema: {manifest_path}")
        if source.get("status") == "active":
            raise ValueError(f"campaign source session is still active: {session.session_id}")
        if source.get("id") != session.session_id:
            raise ValueError(f"campaign source id mismatch: {session.session_id}")
        if source.get("stage_polarity") != session.polarity:
            raise ValueError(f"campaign source polarity mismatch: {session.session_id}")
        if source.get("plan_signature") != session.plan_signature:
            raise ValueError(f"campaign source plan mismatch: {session.session_id}")
        with points_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
        if common_fields is None:
            common_fields = fields
        elif fields != common_fields:
            raise ValueError("campaign point-summary columns do not match")
        loaded.append((session, directory, manifest, rows, fields))

    root = Path(root).resolve()
    target = root / f"HCPES-campaign-{_safe_id(campaign.id)}"
    if target.exists():
        raise FileExistsError(f"HCPES campaign directory already exists: {target}")
    target.mkdir(parents=True)
    paths = HcpesCampaignPaths(
        directory=target,
        manifest=target / "campaign.yaml",
        combined_points=target / "combined_points.csv",
    )
    output_fields = [
        "source_session", "source_polarity", "source_point", *(common_fields or [])]
    combined: list[tuple[float, int, int, dict[str, Any]]] = []
    session_entries = []
    for session, directory, _manifest, rows, _fields in loaded:
        relative = Path(os.path.relpath(directory, target)).as_posix()
        session_entries.append({
            "id": session.session_id,
            "polarity": session.polarity,
            "status": _manifest["session"]["status"],
            "plan_signature": session.plan_signature,
            "directory": relative,
            "manifest": f"{relative}/manifest.yaml",
        })
        for ordinal, row in enumerate(rows):
            signed = float(row["signed_stage_bias_v"])
            output = {
                "source_session": session.session_id,
                "source_polarity": session.polarity,
                "source_point": row["point_index"],
                **row,
            }
            polarity_order = 0 if session.polarity == -1 else 1
            combined.append((signed, polarity_order, ordinal, output))
    combined.sort(key=lambda item: item[:3])
    with paths.combined_points.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields)
        writer.writeheader()
        writer.writerows(item[3] for item in combined)
        handle.flush()

    campaign_data = {
        "schema_version": SCHEMA_VERSION,
        "kind": "hcpes_polarity_campaign",
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "complete": campaign.complete,
            "created_at_epoch": created_at,
            "created_at": _iso(created_at),
            "plan_signature": campaign.sessions[0].plan_signature,
        },
        "sessions": session_entries,
        "derived": {
            "combined_points": paths.combined_points.name,
            "row_count": len(combined),
            "ordering": "signed_stage_bias_v ascending; negative session before positive at ties",
        },
    }
    paths.manifest.write_text(
        yaml.safe_dump(campaign_data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return paths
