"""Persistent display identity for new analysis run bundles."""
from __future__ import annotations

import json
from pathlib import Path


META_SUFFIX = "_analysis.json"


def read_identity(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("schema_version") == 1
                and isinstance(data.get("name"), str) and data["name"]
                and isinstance(data.get("date"), str)
                and isinstance(data.get("number"), int) and data["number"] >= 1):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return None


def next_number(root: Path, name: str, date: str, *, exclude: Path | None = None) -> int:
    used = set()
    for path in root.rglob(f"*{META_SUFFIX}"):
        if path == exclude:
            continue
        identity = read_identity(path)
        if identity and identity["name"].casefold() == name.casefold() and identity["date"] == date:
            used.add(identity["number"])
    number = 1
    while number in used:
        number += 1
    return number


def display_name(identity: dict) -> str:
    return (f"{identity['name']} · {identity['date']}"
            + (f" · {identity['number']:02d}" if identity["number"] > 1 else ""))
