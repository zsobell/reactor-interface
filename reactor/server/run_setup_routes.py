"""Read and review prior run settings without issuing device commands."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..run_setup import gas_conflicts, mfc_gas_identities, missing_fields
from .run_setups import RunSetupFiles


def create_run_setup_router(supervisor: Any, directory: Path) -> APIRouter:
    router = APIRouter()
    files = RunSetupFiles(directory)

    def reviewed(file: str) -> dict:
        setup = files.load(file)
        current = mfc_gas_identities(supervisor.mfcs)
        return {**setup, "current_gas_names": current,
                "gas_conflicts": gas_conflicts(setup, current),
                "missing_fields": missing_fields(setup)}

    @router.get("/api/run-setups")
    def list_setups() -> dict:
        return {"runs": files.entries()}

    @router.get("/api/run-setups/load")
    def load_setup(file: str) -> dict:
        return reviewed(file)

    @router.post("/api/run-setups/apply")
    def check_setup(payload: dict = Body(...)) -> dict:
        # This endpoint authorizes copying form values; it does not save a
        # program, start pre-start, or call any hardware setter.
        phase = getattr(supervisor.runs, "phase", "idle")
        phase = getattr(phase, "value", phase)
        if (supervisor.runs.in_progress or phase not in ("idle", "finished")
                or supervisor.prestart_running
                or supervisor.prestart.get("cleanup_available")
                or supervisor.hcpes_running):
            raise HTTPException(409, "wait until the current run or pre-start finishes")
        file = payload.get("file")
        if not isinstance(file, str):
            raise HTTPException(422, "select a previous run")
        setup = reviewed(file)
        if payload.get("digest") != setup["digest"]:
            raise HTTPException(409, "saved run changed; load it again before applying")
        if payload.get("current_gas_names") != setup["current_gas_names"]:
            raise HTTPException(409, "MFC gas selection changed; load the run again")
        acknowledged = payload.get("acknowledge_gases")
        if not isinstance(acknowledged, list) or any(not isinstance(mfc, str)
                                                     for mfc in acknowledged):
            raise HTTPException(422, "invalid gas acknowledgement")
        required = {item["mfc"] for item in setup["gas_conflicts"] if item["used"]}
        if not required.issubset(acknowledged):
            raise HTTPException(409, "review gas selection before applying flow settings")
        return setup

    return router
