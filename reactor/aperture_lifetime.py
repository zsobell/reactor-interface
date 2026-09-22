"""Durable, read-only-with-respect-to-hardware HCPES aperture bookkeeping.

The aperture has no identity or runtime feedback of its own.  This module only
integrates an observed beam-capable predicate supplied by :class:`Supervisor`;
it never reads or commands a device.  Durations use a monotonic clock while
human-facing installation/replacement labels use UTC wall timestamps.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any
from uuid import uuid4

from .control.clock import Clock


SCHEMA_VERSION = 1
CHECKPOINT_EVERY_S = 30.0


class ApertureLifetimeError(RuntimeError):
    """The durable aperture record is unavailable or invalid."""


class ApertureLifetimeConflict(ApertureLifetimeError):
    """A replacement request names an aperture that is no longer current."""


def _iso_utc(timestamp: float) -> str:
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ApertureLifetimeError("wall clock did not return a finite timestamp")
    return (datetime.fromtimestamp(float(timestamp), timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _valid_timestamp(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApertureLifetimeError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApertureLifetimeError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ApertureLifetimeError(f"{field} must include a timezone")
    return value


def _runtime(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApertureLifetimeError(f"{field} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ApertureLifetimeError(f"{field} must be finite and non-negative")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApertureLifetimeError(f"{field} must be a non-empty string")
    return value


class ApertureLifetime:
    """Own the versioned aperture record and integrate observed active spans."""

    def __init__(self, path: Path, *, clock: Clock | None = None,
                 checkpoint_every_s: float = CHECKPOINT_EVERY_S) -> None:
        self.path = Path(path)
        self.clock = clock or Clock()
        self.checkpoint_every_s = max(1.0, float(checkpoint_every_s))
        self._document: dict[str, Any] | None = None
        self._active: bool | None = None
        self._last_elapsed: float | None = None
        self._last_checkpoint_elapsed: float | None = None
        self.load_error = ""
        self.persistence_error = ""
        try:
            if self.path.exists():
                self._document = self._validate(
                    json.loads(self.path.read_text(encoding="utf-8")))
            else:
                now = _iso_utc(self.clock.wall())
                self._document = {
                    "schema_version": SCHEMA_VERSION,
                    "current": self._new_current(now),
                    "history": [],
                }
        except Exception as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _new_current(installed_at: str) -> dict[str, Any]:
        return {
            "id": f"aperture-{uuid4().hex}",
            "installed_at": installed_at,
            "runtime_s": 0.0,
            "last_observed_at": None,
            "last_observed_active": None,
            "observation_gap_since": installed_at,
            "observation_gap_reason": "waiting for a valid Glassman HV observation",
        }

    def _validate(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ApertureLifetimeError("aperture record must be a JSON object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ApertureLifetimeError(
                f"unsupported aperture schema version {raw.get('schema_version')!r}")
        current = raw.get("current")
        history = raw.get("history")
        if not isinstance(current, dict) or not isinstance(history, list):
            raise ApertureLifetimeError("aperture record requires current and history")
        normalized_current = {
            "id": _identifier(current.get("id"), "current.id"),
            "installed_at": _valid_timestamp(
                current.get("installed_at"), "current.installed_at"),
            "runtime_s": _runtime(current.get("runtime_s"), "current.runtime_s"),
            "last_observed_at": _valid_timestamp(
                current.get("last_observed_at"), "current.last_observed_at", nullable=True),
            "last_observed_active": current.get("last_observed_active"),
            "observation_gap_since": _valid_timestamp(
                current.get("observation_gap_since"),
                "current.observation_gap_since", nullable=True),
            "observation_gap_reason": str(current.get("observation_gap_reason") or ""),
        }
        if normalized_current["last_observed_active"] not in (True, False, None):
            raise ApertureLifetimeError(
                "current.last_observed_active must be true, false, or null")
        normalized_history = []
        seen = {normalized_current["id"]}
        for index, row in enumerate(history):
            if not isinstance(row, dict):
                raise ApertureLifetimeError(f"history[{index}] must be an object")
            item = {
                "id": _identifier(row.get("id"), f"history[{index}].id"),
                "installed_at": _valid_timestamp(
                    row.get("installed_at"), f"history[{index}].installed_at"),
                "replaced_at": _valid_timestamp(
                    row.get("replaced_at"), f"history[{index}].replaced_at"),
                "runtime_s": _runtime(
                    row.get("runtime_s"), f"history[{index}].runtime_s"),
            }
            if item["id"] in seen:
                raise ApertureLifetimeError(f"duplicate aperture id {item['id']!r}")
            seen.add(item["id"])
            normalized_history.append(item)
        return {
            "schema_version": SCHEMA_VERSION,
            "current": normalized_current,
            "history": normalized_history,
        }

    def _require_document(self) -> dict[str, Any]:
        if self._document is None:
            raise ApertureLifetimeError(
                "aperture record is unavailable; preserve and repair the existing file: "
                + (self.load_error or "unknown load error"))
        return self._document

    def _write(self, document: dict[str, Any]) -> None:
        """Replace the record atomically without exposing a partial JSON file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _accrue(self, now_elapsed: float) -> None:
        document = self._require_document()
        if self._last_elapsed is None:
            self._last_elapsed = now_elapsed
            return
        delta = now_elapsed - self._last_elapsed
        self._last_elapsed = now_elapsed
        if not math.isfinite(delta) or delta < 0:
            self._active = None
            current = document["current"]
            current["observation_gap_since"] = _iso_utc(self.clock.wall())
            current["observation_gap_reason"] = "monotonic clock moved backward"
            return
        if self._active is True:
            document["current"]["runtime_s"] += delta

    def observe(self, active: bool | None, *, reason: str = "") -> None:
        """Integrate one already-observed beam state; never inspect hardware."""
        document = self._require_document()
        now_elapsed = float(self.clock.elapsed())
        if not math.isfinite(now_elapsed):
            raise ApertureLifetimeError("monotonic clock did not return a finite value")
        previous = self._active
        self._accrue(now_elapsed)
        current = document["current"]
        now_wall = _iso_utc(self.clock.wall())
        if active is None:
            self._active = None
            current["observation_gap_since"] = (
                current.get("observation_gap_since") or now_wall)
            current["observation_gap_reason"] = reason or "beam state is unknown"
        else:
            self._active = bool(active)
            current["last_observed_at"] = now_wall
            current["last_observed_active"] = self._active
            current["observation_gap_since"] = None
            current["observation_gap_reason"] = ""

        due = (previous != self._active
               or self._last_checkpoint_elapsed is None
               or (self._active is True
                   and now_elapsed - self._last_checkpoint_elapsed
                   >= self.checkpoint_every_s))
        if due:
            self.checkpoint()

    def checkpoint(self, *, force: bool = False) -> bool:
        """Persist accumulated state; write errors remain visible and retryable."""
        document = self._require_document()
        now_elapsed = float(self.clock.elapsed())
        if force:
            self._accrue(now_elapsed)
        try:
            self._write(document)
        except Exception as exc:
            self.persistence_error = f"{type(exc).__name__}: {exc}"
            return False
        self.persistence_error = ""
        self._last_checkpoint_elapsed = now_elapsed
        return True

    def replace(self, expected_id: str) -> dict[str, Any]:
        """Archive the named aperture exactly once and begin a new record."""
        document = self._require_document()
        if not expected_id:
            raise ApertureLifetimeConflict("expected aperture id is required")
        current = document["current"]
        if current["id"] != expected_id:
            if any(row["id"] == expected_id for row in document["history"]):
                return self.snapshot()
            raise ApertureLifetimeConflict(
                "the displayed aperture is no longer current; refresh before confirming")
        if self._active is True:
            raise ApertureLifetimeConflict(
                "cannot replace the aperture record while beam timing is active")

        now_elapsed = float(self.clock.elapsed())
        self._accrue(now_elapsed)
        replaced_at = _iso_utc(self.clock.wall())
        candidate = copy.deepcopy(document)
        old = candidate["current"]
        candidate["history"].append({
            "id": old["id"],
            "installed_at": old["installed_at"],
            "replaced_at": replaced_at,
            "runtime_s": old["runtime_s"],
        })
        candidate["current"] = self._new_current(replaced_at)
        try:
            self._write(candidate)
        except Exception as exc:
            self.persistence_error = f"{type(exc).__name__}: {exc}"
            raise ApertureLifetimeError(
                f"could not save aperture replacement: {self.persistence_error}") from exc
        self._document = candidate
        self._active = None
        self._last_elapsed = now_elapsed
        self._last_checkpoint_elapsed = now_elapsed
        self.persistence_error = ""
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        if self._document is None:
            return {
                "available": False,
                "active": None,
                "error": self.load_error or "aperture record unavailable",
                "persistence_error": self.persistence_error,
                "history": [],
            }
        document = copy.deepcopy(self._document)
        current = document["current"]
        runtime = float(current["runtime_s"])
        if self._active is True and self._last_elapsed is not None:
            delta = float(self.clock.elapsed()) - self._last_elapsed
            if math.isfinite(delta) and delta > 0:
                runtime += delta
        current["runtime_s"] = runtime
        current["runtime_h"] = runtime / 3600.0
        for row in document["history"]:
            row["runtime_h"] = row["runtime_s"] / 3600.0
        return {
            "available": True,
            "schema_version": SCHEMA_VERSION,
            "active": self._active,
            "current": current,
            "history": document["history"],
            "persistence_error": self.persistence_error,
        }
