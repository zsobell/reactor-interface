"""Run-setup HTTP review and active-run rejection use no devices."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from reactor.run_setup import SETUP_SUFFIX, make_snapshot
from reactor.server.run_setup_routes import create_run_setup_router
from tests._support import request


async def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run = root / "Test"
        run.mkdir()
        name = f"Test/Test_261027_120000{SETUP_SUFFIX}"
        (root / name).write_text(json.dumps(make_snapshot({
            "cycles": 10, "mfc1_gas_enable": True,
            "mfc1_gas_order": "first", "mfc1_gas_pct": 100,
            "mfc1_gas_flow_sccm": 2,
        }, "ald", {"mfc1": "NH3"})), encoding="utf-8")
        sup = SimpleNamespace(runs=SimpleNamespace(in_progress=False),
                              prestart_running=False, prestart={}, hcpes_running=False,
                              mfcs={"mfc1": SimpleNamespace(gas="H2", connected=True)})
        app = FastAPI()
        app.include_router(create_run_setup_router(sup, root))
        status, listed = await request(app, "/api/run-setups")
        assert status == 200 and len(listed["runs"]) == 1 and listed["runs"][0]["file"] == name
        url = "/api/run-setups/load?file=" + name
        status, loaded = await request(app, url)
        assert status == 200
        assert loaded["gas_conflicts"][0]["source_gas"] == "NH3"
        assert loaded["gas_conflicts"][0]["current_gas"] == "H2"
        payload = {"file": name, "digest": loaded["digest"],
                   "current_gas_names": loaded["current_gas_names"],
                   "acknowledge_gases": []}
        async def apply():
            return await request(app, "/api/run-setups/apply", method="POST",
                                 body=json.dumps(payload).encode(),
                                 headers=[(b"content-type", b"application/json")])
        assert (await apply())[0] == 409
        payload["acknowledge_gases"] = ["mfc1"]
        assert (await apply())[0] == 200
        sup.mfcs["mfc1"].gas = "N2"
        assert (await apply())[0] == 409
        sup.mfcs["mfc1"].gas = "H2"
        sup.runs.in_progress = True
        assert (await apply())[0] == 409
        sup.runs.in_progress = False
        sup.runs.phase = "finishing"
        assert (await apply())[0] == 409
        sup.runs.phase = "finished"
        sup.prestart = {"primed": True, "cleanup_available": True}
        assert (await apply())[0] == 409
        sup.prestart = {}
        payload["digest"] = "old"
        assert (await apply())[0] == 409
        assert (await request(app, "/api/run-setups/load?file=../config/run_params.json"))[0] == 404
    print("PASS run setup routes, gas review, active-run and digest guards")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
