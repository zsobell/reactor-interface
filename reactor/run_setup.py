"""Machine-readable settings captured alongside new ALD/CVD runs.

This module handles saved data only. Importing it never touches a device.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

SETUP_SUFFIX = "_run_setup.json"
SCHEMA_VERSION = 1

# Only operator-editable run settings are copied into a future run.
RUN_KEYS = frozenset({
    "cycles", "dose_pressure_torr", "dose_s", "dose_pulses",
    "dose_pulse_gap_s", "pump_a_s", "beam_s", "pump_b_s",
    "min_current_a", "sample_bias_v", "sample_bias_polarity",
    "plasma_current_ma", "grid_voltage_v", "steering_current_a",
    "collimating_current_a", "sample_bias_lead_s", "sample_bias_trail_s",
    "fill_pulse_on_s", "fill_pulse_off_s", "tolerance_frac",
    "reignite_pulse_s", "reignite_settle_s", "ar_close_delay_s",
    "ar_soft_open_pulses", "ar_soft_open_on_s", "ar_soft_open_gap_s",
    "pre_ar_sccm", "pre_valve_delay_s", "pre_hold_s", "gas_overlap_s",
    *(f"{mfc}_gas_{field}" for mfc in ("mfc1", "mfc2")
      for field in ("enable", "order", "pct", "flow_sccm")),
})
BOOL_KEYS = frozenset(f"{mfc}_gas_enable" for mfc in ("mfc1", "mfc2"))
TEXT_KEYS = frozenset(f"{mfc}_gas_order" for mfc in ("mfc1", "mfc2"))
INT_KEYS = frozenset({"cycles", "dose_pulses", "sample_bias_polarity", "ar_soft_open_pulses"})


def make_snapshot(params: dict[str, Any], mode: str,
                  gas_names: dict[str, str], *, prestart_recipe: dict | None = None) -> dict:
    if mode not in ("ald", "cvd"):
        raise ValueError("only built ALD/CVD runs have loadable setup")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "params": {key: value for key, value in params.items() if key in RUN_KEYS},
        "gas_names": {key: str(gas_names.get(key) or "") for key in ("ar", "mfc1", "mfc2")},
        "prestart_recipe": prestart_recipe,
        "source": "recorded settings",
    }


def validate_snapshot(value: Any) -> dict:
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != SCHEMA_VERSION):
        raise ValueError("unsupported run setup version")
    if value.get("mode") not in ("ald", "cvd") or not isinstance(value.get("params"), dict):
        raise ValueError("invalid run setup")
    if not isinstance(value.get("gas_names"), dict):
        raise ValueError("run setup has no gas identity record")
    if any(key not in RUN_KEYS for key in value["params"]):
        raise ValueError("run setup contains unknown parameter keys")
    for key, item in value["params"].items():
        if key in BOOL_KEYS:
            valid = isinstance(item, bool)
        elif key in TEXT_KEYS:
            valid = item in ("first", "second", "simultaneous")
        elif key in INT_KEYS:
            valid = type(item) is int
        else:
            valid = type(item) in (int, float) and math.isfinite(item)
        if not valid:
            raise ValueError(f"invalid saved parameter: {key}")
    if any(not isinstance(key, str) or not isinstance(item, str)
           for key, item in value["gas_names"].items()):
        raise ValueError("invalid gas identity record")
    if value.get("prestart_recipe") is not None and not isinstance(
            value["prestart_recipe"], dict):
        raise ValueError("invalid pre-start recipe record")
    return value


def read_snapshot(path: Path) -> dict:
    return validate_snapshot(json.loads(path.read_text(encoding="utf-8")))


def write_snapshot(path: Path, value: dict) -> None:
    """Replace one run's final settings without exposing a half-written file."""
    validate_snapshot(value)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def gas_conflicts(setup: dict, current_gas_names: dict[str, str]) -> list[dict]:
    """Compare all MFC identities; mark which differences affect saved flow."""
    params = setup["params"]
    used = {mfc for mfc in ("mfc1", "mfc2")
            if params.get(f"{mfc}_gas_enable")}
    if params.get("pre_ar_sccm", 0):
        used.add("ar")
    conflicts = []
    for mfc in ("ar", "mfc1", "mfc2"):
        source = str(setup["gas_names"].get(mfc) or "").strip()
        current = str(current_gas_names.get(mfc) or "").strip()
        if (source or current or mfc in used) and (
                not source or not current or source.casefold() != current.casefold()):
            conflicts.append({"mfc": mfc, "source_gas": source,
                              "current_gas": current, "used": mfc in used})
    return conflicts


def missing_fields(setup: dict) -> list[str]:
    expected = RUN_KEYS - ({"beam_s", "pump_b_s"} if setup["mode"] == "cvd" else set())
    return sorted(expected - setup["params"].keys())


def mfc_gas_identities(mfcs: dict) -> dict[str, str]:
    """Use each connected controller's gas selection, never an operator label."""
    return {mfc: str(getattr(device, "gas", "") or "")
            if getattr(device, "connected", False) else ""
            for mfc, device in mfcs.items()}
