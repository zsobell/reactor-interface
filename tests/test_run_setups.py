"""Saved-run discovery is read-only and constrained to run data."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from fastapi import HTTPException

from reactor.run_setup import SETUP_SUFFIX, make_snapshot, read_snapshot, write_snapshot
from reactor.server.run_setups import RunSetupFiles


async def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run = root / "Mo-042"
        run.mkdir()
        stem = "Mo-042_261027_120000"
        (run / f"{stem}{SETUP_SUFFIX}").write_text(json.dumps(make_snapshot(
            {"cycles": 42, "mfc1_gas_enable": True}, "ald",
            {"mfc1": "NH3"})), encoding="utf-8")
        write_snapshot(run / f"{stem}{SETUP_SUFFIX}", make_snapshot(
            {"cycles": 43, "mfc1_gas_enable": True}, "ald", {"mfc1": "NH3"}))
        assert read_snapshot(run / f"{stem}{SETUP_SUFFIX}")["params"]["cycles"] == 43
        assert not (run / f"{stem}{SETUP_SUFFIX}.tmp").exists()
        (run / f"{stem}_analysis.json").write_text(json.dumps({
            "schema_version": 1, "name": "Mo-042", "date": "2026-10-27", "number": 1,
        }), encoding="utf-8")
        hcpes = root / "HCPES-sample"
        hcpes.mkdir()
        (hcpes / f"x{SETUP_SUFFIX}").write_text(json.dumps(make_snapshot(
            {"cycles": 9}, "cvd", {})), encoding="utf-8")
        (root / "Older_260101_120000_run_params.txt").write_text(
            "Historical text reports are intentionally not loadable.", encoding="utf-8")
        files = RunSetupFiles(root)
        entries = files.entries()
        assert len(entries) == 1 and entries[0]["file"].endswith(SETUP_SUFFIX)
        loaded = files.load(entries[0]["file"])
        assert loaded["params"]["cycles"] == 43
        assert loaded["gas_names"]["mfc1"] == "NH3"
        for bad in ("../config/run_params.json", "HCPES-sample/x" + SETUP_SUFFIX,
                    "Older_260101_120000_run_params.txt"):
            try:
                files.load(bad)
            except HTTPException as exc:
                assert exc.status_code == 404
            else:
                raise AssertionError(f"accepted {bad}")
    print("PASS new run setup discovery and containment")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
