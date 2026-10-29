"""Read-only discovery of previous ALD/CVD run settings."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from ..run_identity import META_SUFFIX, display_name, read_identity
from ..run_setup import SETUP_SUFFIX, validate_snapshot
from .data import DataFiles

_STAMP = re.compile(r"(?:^|_)(\d{6})_(\d{6})(?:_\d{2})?$")


def _legacy_name(root: Path, parent: Path, stem: str) -> str:
    match = _STAMP.search(stem)
    try:
        date = datetime.strptime(match[1], "%y%m%d").date().isoformat() if match else ""
    except ValueError:
        date = ""
    name = parent.name if parent != root else stem
    return f"{name} · {date}" if date else name


class RunSetupFiles:
    def __init__(self, directory: Path):
        self.files = DataFiles(directory)

    def _source(self, relative_name: str) -> Path:
        if not relative_name.endswith(SETUP_SUFFIX):
            raise HTTPException(404, "not a run setup file")
        path = self.files.in_run_data_dir(relative_name)
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "not a run setup file")
        return path

    def load(self, relative_name: str) -> dict:
        path = self._source(relative_name)
        try:
            content = path.read_bytes()
            if len(content) > 2_000_000:
                raise ValueError("run setup is too large")
            text = content.decode("utf-8-sig")
            value = validate_snapshot(json.loads(text))
        except (OSError, ValueError, UnicodeError) as exc:
            raise HTTPException(422, f"could not load run setup: {exc}") from exc
        return {**value, "file": relative_name,
                "digest": hashlib.sha256(content).hexdigest()}

    def entries(self) -> list[dict]:
        root = self.files.directory
        if not root.exists():
            return []
        out = []
        for path in root.rglob(f"*{SETUP_SUFFIX}"):
            if self.files.is_hcpes_path(path) or not path.is_file() or path.is_symlink():
                continue
            try:
                relative = path.relative_to(root).as_posix()
                loaded = self.load(relative)
                stem = path.name[:-len(SETUP_SUFFIX)]
                parent = path.parent
                identity = read_identity(parent / f"{stem}{META_SUFFIX}")
                stat = path.stat()
            except (HTTPException, OSError):
                continue
            out.append({
                "file": relative,
                "display_name": display_name(identity) if identity else _legacy_name(root, parent, stem),
                "mode": loaded["mode"],
                "source": loaded["source"],
                "mtime": stat.st_mtime,
            })
        out.sort(key=lambda entry: entry["mtime"], reverse=True)
        return out
