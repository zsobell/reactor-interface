"""Old gas-keyed run parameters still work after the channel rename.

The two process-gas MFCs were keyed by the gas on them (`h2_gas_pct`) until
2026-09-09. The gas is selected on the unit and changes - the `h2` line runs
NH3 now - so they are keyed by CHANNEL instead (`mfc1_gas_pct`). Two things
still hold the old names and must not lose the operator's numbers:

  * `config/run_params.json` as saved before the rename, and
  * a browser tab still holding the old page, which posts them on Start.

`app.migrate_params` renames them on every path that READS parameters; nothing
writes the old names back. This drives the real FastAPI app over raw ASGI, so
nothing here goes near the DAQ or the serial ports of the running reactor.

Run directly: python -m tests.test_param_migration
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from reactor.server import app as app_mod
from tests._support import Checker, asgi_call as call

OLD = {
    "cycles": 150, "dose_s": 0.5,
    "h2_gas_enable": True, "h2_gas_order": "simultaneous",
    "h2_gas_pct": "80", "h2_gas_flow_sccm": "2.4",
    "n2_gas_enable": True, "n2_gas_order": "simultaneous",
    "n2_gas_pct": "20", "n2_gas_flow_sccm": ".8",
}


async def main() -> int:
    c = Checker("test_param_migration")

    c.section("1. the rename itself")
    got = app_mod.migrate_params(OLD)
    c.check("every gas key moved to its channel",
            sorted(k for k in got if "gas" in k) ==
            ["mfc1_gas_enable", "mfc1_gas_flow_sccm", "mfc1_gas_order",
             "mfc1_gas_pct", "mfc2_gas_enable", "mfc2_gas_flow_sccm",
             "mfc2_gas_order", "mfc2_gas_pct"],
            str(sorted(k for k in got if "gas" in k)))
    c.check("the VALUES are untouched - that is the whole point",
            got["mfc1_gas_flow_sccm"] == "2.4" and got["mfc2_gas_pct"] == "20",
            f"{got['mfc1_gas_flow_sccm']} / {got['mfc2_gas_pct']}")
    c.check("nothing else is renamed",
            got["cycles"] == 150 and got["dose_s"] == 0.5)
    c.check("already-new params pass through unchanged",
            app_mod.migrate_params({"mfc1_gas_pct": 50}) == {"mfc1_gas_pct": 50})
    # A key that merely CONTAINS the old prefix is not a gas parameter.
    c.check("only a leading prefix counts",
            app_mod.migrate_params({"pump_h2_gas_pct": 1}) == {"pump_h2_gas_pct": 1})

    c.section("2. a saved file written before the rename")
    tmp = Path(tempfile.mkdtemp(prefix="param_migration_"))
    original = app_mod.RUN_PARAMS_PATH
    app_mod.RUN_PARAMS_PATH = tmp / "run_params.json"
    try:
        app_mod.RUN_PARAMS_PATH.write_text(json.dumps(OLD), encoding="utf-8")
        app = app_mod.create_app()
        st, body = await call(app, "GET", "/api/run_params")
        p = body.get("params", {})
        c.check("the UI is handed the new names", st == 200
                and "mfc1_gas_flow_sccm" in p and "h2_gas_flow_sccm" not in p,
                str(sorted(p))[:120])
        c.check("with the operator's numbers intact",
                p.get("mfc1_gas_flow_sccm") == "2.4", str(p.get("mfc1_gas_flow_sccm")))

        c.section("3. a stale browser posting the old names")
        # Saving through the API normalises them, so the file heals itself the
        # first time anything writes - the old names never come back.
        await call(app, "POST", "/api/run_params", OLD)
        on_disk = json.loads(app_mod.RUN_PARAMS_PATH.read_text(encoding="utf-8"))
        c.check("what lands on disk is already renamed",
                "mfc1_gas_pct" in on_disk and "h2_gas_pct" not in on_disk,
                str(sorted(on_disk))[:120])

        c.section("4. an estimate built from old parameters still schedules gas")
        st, body = await call(app, "POST", "/api/run/estimate",
                              dict(OLD, mode="ald"))
        c.check("200 with a real duration", st == 200
                and isinstance(body.get("total_s"), (int, float))
                and body["total_s"] > 0, f"{st} {body}")
    finally:
        app_mod.RUN_PARAMS_PATH = original

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
