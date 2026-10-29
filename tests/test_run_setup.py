"""Pure saved-run setup checks; no reactor or filesystem writes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from reactor.run_setup import (gas_conflicts, make_snapshot, mfc_gas_identities,
                               validate_snapshot)


async def main() -> int:
    params = {
        "cycles": 12, "dose_s": 0.25, "pump_a_s": 3,
        "beam_s": 4, "pump_b_s": 2,
        "mfc1_gas_enable": True, "mfc1_gas_order": "first",
        "mfc1_gas_pct": 70, "mfc1_gas_flow_sccm": 2.4,
        "mfc2_gas_enable": True, "mfc2_gas_order": "second",
        "mfc2_gas_pct": 30, "mfc2_gas_flow_sccm": 0.8,
        "run_name": "old name", "unexpected": "do not restore",
    }
    setup = validate_snapshot(make_snapshot(params, "ald", {"mfc1": "NH3", "mfc2": "N2"}))
    assert setup["mode"] == "ald"
    assert setup["gas_names"]["mfc1"] == "NH3"
    assert setup["params"]["mfc1_gas_flow_sccm"] == 2.4
    assert "run_name" not in setup["params"]
    assert "unexpected" not in setup["params"]
    assert gas_conflicts(setup, {"mfc1": "NH3", "mfc2": "N2"}) == []
    assert [item["mfc"] for item in gas_conflicts(
        setup, {"mfc1": "H2", "mfc2": "N2"})] == ["mfc1"]
    idle_setup = make_snapshot({"mfc1_gas_enable": False}, "ald", {"mfc1": "NH3"})
    idle_conflict = gas_conflicts(idle_setup, {"mfc1": "H2"})
    assert idle_conflict[0]["mfc"] == "mfc1" and not idle_conflict[0]["used"]
    assert mfc_gas_identities({"mfc1":SimpleNamespace(gas="2: NH3", connected=True),
                               "mfc2":SimpleNamespace(gas="N2", connected=False)}) == {
                                   "mfc1":"2: NH3", "mfc2":""}
    for bad in (float("nan"), "twelve", True):
        broken = {**setup, "params": {**setup["params"], "cycles": bad}}
        try:
            validate_snapshot(broken)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid saved cycle count: {bad!r}")

    print("PASS run setup snapshot and gas identity checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
