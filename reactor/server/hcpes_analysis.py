"""Read-only HCPES session and polarity-campaign analysis endpoints.

This module deliberately knows nothing about devices or the Supervisor.  It
validates immutable recording bundles below the configured data directory and
returns typed, provenance-rich rows for the Analysis page.
"""
from __future__ import annotations

import contextlib
import csv
import json
import math
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
import yaml

from ..hcpes_recording import SCHEMA_VERSION


def _typed(value: str) -> Any:
    if value == "":
        return None
    if value == "True":
        return True
    if value == "False":
        return False
    try:
        number = float(value)
    except ValueError:
        return value
    return number if math.isfinite(number) else None


def _csv_rows(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{key: _typed(value) for key, value in row.items()}
                for row in reader]
    for row in rows:
        for key in ("excluded_counts", "stability_gates"):
            value = row.get(key)
            if isinstance(value, str):
                with contextlib.suppress(json.JSONDecodeError):
                    row[key] = json.loads(value)
    return fields, rows


class HcpesAnalysisFiles:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()

    def _contained(self, path: Path) -> Path:
        resolved = path.resolve()
        if self.directory not in resolved.parents or not resolved.exists():
            raise HTTPException(404, "no such HCPES source in data directory")
        return resolved

    def _source_dir(self, name: str) -> Path:
        directory = self._contained(self.directory / name)
        if not directory.is_dir():
            raise HTTPException(404, "HCPES source is not a directory")
        return directory

    @staticmethod
    def _yaml(path: Path) -> dict[str, Any]:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise HTTPException(422, f"cannot read HCPES manifest: {exc}") from exc
        if document.get("schema_version") != SCHEMA_VERSION:
            raise HTTPException(422, "unsupported HCPES bundle schema")
        return document

    def entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if not self.directory.exists():
            return entries
        for path in self.directory.rglob("manifest.yaml"):
            with contextlib.suppress(OSError, yaml.YAMLError, KeyError, TypeError):
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if (doc.get("schema_version") != SCHEMA_VERSION
                        or doc.get("kind") != "hcpes_characterization_session"):
                    continue
                session = doc["session"]
                stat = path.stat()
                entries.append({
                    "name": path.parent.relative_to(self.directory).as_posix(),
                    "kind": "session",
                    "title": session.get("id", path.parent.name),
                    "status": session.get("status", "unknown"),
                    "polarity": session.get("stage_polarity"),
                    "started_at": session.get("started_at"),
                    "points": (doc.get("counts") or {}).get("points", 0),
                    "mtime": stat.st_mtime,
                })
        for path in self.directory.rglob("campaign.yaml"):
            with contextlib.suppress(OSError, yaml.YAMLError, KeyError, TypeError):
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if (doc.get("schema_version") != SCHEMA_VERSION
                        or doc.get("kind") != "hcpes_polarity_campaign"):
                    continue
                campaign = doc["campaign"]
                stat = path.stat()
                entries.append({
                    "name": path.parent.relative_to(self.directory).as_posix(),
                    "kind": "campaign",
                    "title": campaign.get("name") or campaign.get("id", path.parent.name),
                    "status": "complete" if campaign.get("complete") else "partial",
                    "polarity": "linked",
                    "started_at": campaign.get("created_at"),
                    "points": (doc.get("derived") or {}).get("row_count", 0),
                    "mtime": stat.st_mtime,
                })
        entries.sort(key=lambda entry: entry["mtime"], reverse=True)
        return entries

    @staticmethod
    def _channel_rows(path: Path) -> dict[int, dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        if not path.exists():
            return rows
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    rows[int(row["point_index"])] = row.get("channels") or {}
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        return rows

    def _session_document(self, directory: Path) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
        manifest = self._yaml(directory / "manifest.yaml")
        if manifest.get("kind") != "hcpes_characterization_session":
            raise HTTPException(422, "not an HCPES session bundle")
        points_name = ((manifest.get("files") or {}).get("points") or {}).get(
            "path", "points.csv")
        points_path = self._contained(directory / points_name)
        fields, points = _csv_rows(points_path)
        channels_name = ((manifest.get("files") or {}).get("point_channels") or {}).get(
            "path", "point_channels.jsonl")
        channel_path = (directory / channels_name).resolve()
        if self.directory not in channel_path.parents:
            raise HTTPException(422, "HCPES channel file escapes the data directory")
        channel_rows = self._channel_rows(channel_path)
        session = manifest.get("session") or {}
        for order, row in enumerate(points, 1):
            point = int(row.get("point_index") or order)
            row.update(
                source_session=session.get("id"),
                source_polarity=session.get("stage_polarity"),
                source_point=point,
                acquisition_order=order,
                channel_stats=channel_rows.get(point, {}),
            )
        return manifest, fields, points

    def load(self, name: str) -> dict[str, Any]:
        directory = self._source_dir(name)
        if (directory / "campaign.yaml").exists():
            return self._load_campaign(name, directory)
        manifest, fields, points = self._session_document(directory)
        session = manifest.get("session") or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "session",
            "name": name,
            "title": session.get("id", directory.name),
            "compatible": True,
            "compatibility_issues": [],
            "session": session,
            "plan": manifest.get("plan") or {},
            "fields": fields,
            "points": points,
            "raw_sources": [{
                "session_id": session.get("id"),
                "directory": name,
                "polarity": session.get("stage_polarity"),
            }],
        }

    def _load_campaign(self, name: str, directory: Path) -> dict[str, Any]:
        document = self._yaml(directory / "campaign.yaml")
        if document.get("kind") != "hcpes_polarity_campaign":
            raise HTTPException(422, "not an HCPES polarity campaign")
        derived_name = (document.get("derived") or {}).get(
            "combined_points", "combined_points.csv")
        fields, points = _csv_rows(self._contained(directory / derived_name))
        sessions = document.get("sessions") or []
        signatures = {session.get("plan_signature") for session in sessions}
        expected = (document.get("campaign") or {}).get("plan_signature")
        issues: list[str] = []
        if len(sessions) != 2:
            issues.append("campaign does not contain exactly two polarity sessions")
        if len(signatures) != 1 or expected not in signatures:
            issues.append("source plan signatures do not match")
        polarities = {session.get("polarity") for session in sessions}
        if polarities != {-1, 1}:
            issues.append("campaign does not contain one negative and one positive session")

        channels: dict[tuple[str, int], dict[str, Any]] = {}
        acquisition_orders: dict[tuple[str, int], int] = {}
        raw_sources = []
        acquisition_offsets: dict[str, int] = {}
        next_offset = 0
        plan: dict[str, Any] = {}
        source_fields: list[str] | None = None
        for session in sessions:
            session_id = str(session.get("id") or "")
            source_dir = self._contained(directory / str(session.get("directory") or ""))
            source_manifest, current_fields, source_points = self._session_document(source_dir)
            actual = source_manifest.get("session") or {}
            if actual.get("id") != session_id:
                issues.append(f"source id does not match campaign entry: {session_id}")
            if actual.get("stage_polarity") != session.get("polarity"):
                issues.append(f"source polarity does not match campaign entry: {session_id}")
            if actual.get("plan_signature") != session.get("plan_signature"):
                issues.append(f"source plan does not match campaign entry: {session_id}")
            if source_fields is None:
                source_fields = current_fields
            elif source_fields != current_fields:
                issues.append("source point-summary columns do not match")
            if not plan:
                plan = source_manifest.get("plan") or {}
            acquisition_offsets[session_id] = next_offset
            next_offset += len(source_points)
            for row in source_points:
                point = int(row["source_point"])
                channels[(session_id, point)] = row["channel_stats"]
                acquisition_orders[(session_id, point)] = (
                    acquisition_offsets[session_id]
                    + int(row["acquisition_order"]))
            raw_sources.append({
                "session_id": session_id,
                "directory": source_dir.relative_to(self.directory).as_posix(),
                "polarity": session.get("polarity"),
            })
        for signed_order, row in enumerate(points, 1):
            session_id = str(row.get("source_session") or "")
            point = int(row.get("source_point") or row.get("point_index") or signed_order)
            row["signed_order"] = signed_order
            row["acquisition_order"] = acquisition_orders.get(
                (session_id, point), acquisition_offsets.get(session_id, 0) + point)
            row["channel_stats"] = channels.get((session_id, point), {})
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "campaign",
            "name": name,
            "title": (document.get("campaign") or {}).get("name", directory.name),
            "compatible": not issues,
            "compatibility_issues": issues,
            "campaign": document.get("campaign") or {},
            "sessions": sessions,
            "plan": plan,
            "fields": fields,
            "points": points,
            "raw_sources": raw_sources,
        }

    def raw(
        self,
        name: str,
        *,
        session_id: str = "",
        point_index: int | None = None,
        mode: Literal["all", "omitted", "qualified"] = "all",
        limit: int = 10000,
    ) -> dict[str, Any]:
        source = self.load(name)
        candidates = source["raw_sources"]
        if session_id:
            candidates = [row for row in candidates if row["session_id"] == session_id]
        if len(candidates) != 1:
            raise HTTPException(400, "choose one campaign source session")
        selected = candidates[0]
        directory = self._source_dir(selected["directory"])
        manifest = self._yaml(directory / "manifest.yaml")
        raw_name = ((manifest.get("files") or {}).get("raw") or {}).get(
            "path", "raw.jsonl")
        path = self._contained(directory / raw_name)
        rows: list[dict[str, Any]] = []
        matched = 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if point_index is not None and row.get("point_index") != point_index:
                    continue
                if mode == "qualified" and not row.get("qualified"):
                    continue
                if mode == "omitted" and row.get("qualified"):
                    continue
                matched += 1
                if len(rows) < limit:
                    rows.append(row)
        return {
            "source_session": selected["session_id"],
            "mode": mode,
            "point_index": point_index,
            "matched": matched,
            "truncated": matched > len(rows),
            "rows": rows,
        }


def create_hcpes_analysis_router(files: HcpesAnalysisFiles) -> APIRouter:
    router = APIRouter()

    @router.get("/api/hcpes/analysis/sources")
    def sources():
        return {"dir": str(files.directory), "sources": files.entries()}

    @router.get("/api/hcpes/analysis/source")
    def source(name: str):
        return files.load(name)

    @router.get("/api/hcpes/analysis/raw")
    def raw(
        name: str,
        session_id: str = "",
        point_index: int | None = Query(default=None, ge=1),
        mode: Literal["all", "omitted", "qualified"] = "all",
        limit: int = Query(default=10000, ge=1, le=50000),
    ):
        return files.raw(
            name, session_id=session_id, point_index=point_index,
            mode=mode, limit=limit)

    return router
