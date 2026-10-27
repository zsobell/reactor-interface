"""Durable, read-only-to-the-UI sensor scrollback for Hardware popup plots.

One compact snapshot every five seconds is written on the existing recording
worker. Queries use an independent SQLite connection and bounded projection,
so a long manual time range does not load the whole archive into memory.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


class SensorArchive:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS samples ("
                       "t REAL PRIMARY KEY, values_json TEXT NOT NULL)")
            db.commit()

    def append(self, row: dict[str, Any]) -> None:
        self.append_many((row,))

    def append_many(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert idempotently, including during a one-time live-history backfill."""
        def packed(row: dict[str, Any]) -> tuple[float, str]:
            values = {key: (float(value) if isinstance(value, (float, int))
                            and not isinstance(value, bool) and math.isfinite(value)
                            else None)
                      for key, value in row.items() if key != "t"}
            return (float(row["t"]), json.dumps(values, separators=(",", ":")))

        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA synchronous=FULL")
            before = db.total_changes
            db.executemany(
                "INSERT OR IGNORE INTO samples (t, values_json) VALUES (?, ?)",
                (packed(row) for row in rows),
            )
            db.commit()
            return db.total_changes - before

    def earliest(self) -> float | None:
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            found = db.execute("SELECT t FROM samples ORDER BY t LIMIT 1").fetchone()
            return float(found[0]) if found else None

    def latest(self) -> float | None:
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            found = db.execute("SELECT t FROM samples ORDER BY t DESC LIMIT 1").fetchone()
            return float(found[0]) if found else None

    def query(self, column: str, start: float, end: float, *,
              buckets: int = 700) -> list[list[float | None]]:
        """Keep first/last, extrema and a missing marker in each time bucket."""
        if not self.path.exists() or not end > start or buckets < 1:
            return []
        width = (end - start) / buckets
        selected: dict[int, dict[str, tuple[float, float | None]]] = {}
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute(
                "SELECT t, values_json FROM samples WHERE t >= ? AND t <= ? ORDER BY t",
                (start, end),
            )
            for t, payload in rows:
                value = json.loads(payload).get(column)
                value = (float(value) if isinstance(value, (float, int))
                         and not isinstance(value, bool) and math.isfinite(value)
                         else None)
                point = (float(t), value)
                index = min(buckets - 1, int((t - start) / width))
                bucket = selected.setdefault(index, {})
                bucket.setdefault("first", point)
                bucket["last"] = point
                if value is None:
                    bucket.setdefault("gap", point)
                else:
                    if "min" not in bucket or value < bucket["min"][1]:
                        bucket["min"] = point
                    if "max" not in bucket or value > bucket["max"][1]:
                        bucket["max"] = point
        result: list[list[float | None]] = []
        for index in sorted(selected):
            points = {point[0]: point for point in selected[index].values()}
            result.extend([*points[t]] for t in sorted(points))
        return result
