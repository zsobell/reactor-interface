"""Versioned user-authored pre-start recipes and device capabilities.

This module is deliberately pure: building a catalog, parsing a recipe,
resolving parameters, and producing a preview never connects to or commands
hardware.  Runtime dispatch lives in :mod:`reactor.control.prestart`.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import ReactorConfig


SCHEMA_VERSION = 1
CURRENT_RECIPE_ID = "current-prestart"


class ParameterRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameter: str = Field(min_length=1)


class RecipeParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    label: str
    value_type: Literal["number", "integer", "boolean", "string", "choice"]
    default: Any
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: list[dict[str, Any]] = Field(default_factory=list)
    source: str | None = None


class PrestartStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    action: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    on_error: Literal["stop", "continue"] = "stop"


class PrestartRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    revision: int = Field(default=1, ge=1)
    builtin: bool = False
    parameters: list[RecipeParameter] = Field(default_factory=list)
    start_steps: list[PrestartStep] = Field(default_factory=list)
    abort_steps: list[PrestartStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "PrestartRecipe":
        parameter_ids = [p.id for p in self.parameters]
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ValueError("parameter ids must be unique")
        step_ids = [s.id for s in (*self.start_steps, *self.abort_steps)]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step ids must be unique across start and abort")
        return self


class PrestartLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    selected_id: str = CURRENT_RECIPE_ID
    recipes: list[PrestartRecipe]

    @model_validator(mode="after")
    def _valid_selection(self) -> "PrestartLibrary":
        ids = [r.id for r in self.recipes]
        if len(ids) != len(set(ids)):
            raise ValueError("recipe ids must be unique")
        if self.selected_id not in ids:
            raise ValueError(f"selected recipe {self.selected_id!r} does not exist")
        return self


class ResolvedStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    target: str
    action: str
    args: dict[str, Any]
    on_error: Literal["stop", "continue"]
    summary: str


class ResolvedPrestart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe_id: str
    revision: int
    name: str
    parameters: dict[str, Any]
    start_steps: tuple[ResolvedStep, ...]
    abort_steps: tuple[ResolvedStep, ...]


def _field(id: str, label: str, type: str, *, unit: str = "", default: Any = None,
           minimum: float | None = None, maximum: float | None = None,
           choices: list[dict[str, Any]] | None = None,
           target_kind: str = "", required: bool = True) -> dict[str, Any]:
    result = {"id": id, "label": label, "type": type, "required": required}
    if unit:
        result["unit"] = unit
    if default is not None:
        result["default"] = default
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    if choices is not None:
        result["choices"] = choices
    if target_kind:
        result["target_kind"] = target_kind
    return result


def _action(id: str, label: str, fields: list[dict[str, Any]] | None = None,
            *, summary: str) -> dict[str, Any]:
    return {"id": id, "label": label, "fields": fields or [], "summary": summary}


def capability_catalog(cfg: ReactorConfig) -> dict[str, Any]:
    """Serializable source of truth for recipe validation and the editor."""
    targets: list[dict[str, Any]] = [
        {
            "id": "system:delay", "kind": "logical", "label": "Delay",
            "actions": [_action("delay.wait", "Wait", [
                _field("seconds", "Duration", "number", unit="s", default=1.0,
                       minimum=0.0),
            ], summary="Wait {seconds} s")],
        },
        {
            "id": "controller:fill", "kind": "controller",
            "label": "Precursor fill regulator",
            "actions": [
                _action("fill.start", "Start regulation", [
                    _field("valve", "Fill valve", "target", target_kind="valve"),
                    _field("gauge", "Pressure gauge", "target", target_kind="sensor"),
                    _field("target_torr", "Target pressure", "number", unit="Torr",
                           default=0.02, minimum=0.0),
                    _field("pulse_on_s", "Pulse width", "number", unit="s",
                           default=0.1, minimum=0.0),
                    _field("pulse_off_s", "Pulse gap", "number", unit="s",
                           default=0.3, minimum=0.0),
                    _field("tolerance_frac", "Warning tolerance", "number",
                           default=0.2, minimum=0.0),
                ], summary="Regulate {gauge} to {target_torr} Torr via {valve}"),
                _action("fill.stop", "Stop regulation", summary="Stop fill regulation"),
            ],
        },
        {
            "id": "controller:plasma", "kind": "controller",
            "label": "Plasma strike and hold",
            "actions": [_action("plasma.strike_hold", "Strike and hold", [
                _field("switch", "Plasma ground valve", "target", target_kind="valve"),
                _field("ammeter", "Current measurement", "target", target_kind="sensor"),
                _field("min_current_a", "Minimum current", "number", unit="A",
                       default=5e-4, minimum=0.0),
                _field("pulse_s", "Reignite pulse", "number", unit="s",
                       default=0.1, minimum=0.0),
                _field("settle_s", "Reignite settle", "number", unit="s",
                       default=0.15, minimum=0.0),
                _field("hold_s", "Continuous hold", "number", unit="s",
                       default=5.0, minimum=0.0),
            ], summary="Strike plasma at |I| >= {min_current_a} A and hold {hold_s} s")],
        },
    ]

    for valve in cfg.valves:
        targets.append({
            "id": f"valve:{valve.id}", "kind": "valve",
            "label": valve.label or valve.id,
            "meta": {"device_id": valve.id, "soft_open": valve.soft_open},
            "actions": [
                _action("valve.open", "Open", summary="Open {target}"),
                _action("valve.close", "Close", summary="Close {target}"),
            ],
        })

    for mfc in cfg.mfcs:
        targets.append({
            "id": f"mfc:{mfc.id}", "kind": "mfc", "label": mfc.label or mfc.id,
            "meta": {"device_id": mfc.id, "isolation_valve": mfc.isolation_valve,
                     "read_key": f"mfc.{mfc.id}.flow", "unit": "sccm"},
            "actions": [
                _action("mfc.start_flow", "Start flow", [
                    _field("sccm", "Flow", "number", unit="sccm", default=1.0,
                           minimum=0.0),
                ], summary="Set {target} to {sccm} sccm"),
                _action("mfc.stop_flow", "Stop flow", summary="Stop {target}"),
                _wait_action("mfc.wait_flow", "Wait for measured flow", "sccm"),
            ],
        })

    for ps in cfg.power_supplies:
        if not ps.enabled:
            continue
        if ps.driver == "glassman_fl":
            actions = [_action("hv.off", "HV off", summary="Command {target} HV off")]
        else:
            actions = [
                _action("supply.output_on", "Output on", summary="Turn {target} output on"),
                _action("supply.output_off", "Output off", summary="Turn {target} output off"),
                _action("supply.set_voltage", "Set voltage", [
                    _field("volts", "Voltage", "number", unit="V", default=0.0,
                           minimum=0.0),
                ], summary="Set {target} to {volts} V"),
                _action("supply.set_current", "Set current", [
                    _field("amps", "Current limit", "number", unit="A", default=0.0,
                           minimum=0.0),
                ], summary="Set {target} current limit to {amps} A"),
                _wait_action("supply.wait_voltage", "Wait for measured voltage", "V",
                             read_key=f"psu.{ps.id}.voltage"),
                _wait_action("supply.wait_current", "Wait for measured current", "A",
                             read_key=f"psu.{ps.id}.current"),
            ]
            if ps.sample_bias:
                actions.insert(0, _action("supply.arm_bias", "Arm stage bias", [
                    _field("volts", "Voltage magnitude", "number", unit="V",
                           default=0.0, minimum=0.0),
                    _field("polarity", "Lead polarity", "choice", default=1,
                           choices=[{"value": 1, "label": "+ positive to stage"},
                                    {"value": -1, "label": "- negative to stage"}]),
                ], summary="Arm {target} at {volts} V with polarity {polarity}"))
        targets.append({
            "id": f"supply:{ps.id}", "kind": "supply", "label": ps.label or ps.id,
            "meta": {"device_id": ps.id, "driver": ps.driver,
                     "sample_bias": ps.sample_bias},
            "actions": actions,
        })

    targets.extend(_sensor_targets(cfg))
    return {"schema_version": SCHEMA_VERSION, "targets": targets}


def _wait_action(action_id: str = "sensor.wait_until", label: str = "Wait until",
                 unit: str = "", *, read_key: str = "") -> dict[str, Any]:
    fields = [
        _field("operator", "Condition", "choice", default="above", choices=[
            {"value": "above", "label": "At or above"},
            {"value": "below", "label": "At or below"},
            {"value": "within", "label": "Within range"},
        ]),
        _field("value", "Value", "number", unit=unit, default=0.0),
        _field("upper", "Upper value", "number", unit=unit, default=0.0,
               required=False),
        _field("hold_s", "Continuous hold", "number", unit="s", default=0.0,
               minimum=0.0),
        _field("timeout_s", "Timeout (0 = none)", "number", unit="s", default=0.0,
               minimum=0.0),
        _field("absolute", "Use absolute value", "boolean", default=False),
    ]
    result = _action(action_id, label, fields,
                     summary="Wait for {target} to be {operator} {value}")
    if read_key:
        result["read_key"] = read_key
    return result


def _sensor_targets(cfg: ReactorConfig) -> list[dict[str, Any]]:
    sensors: list[tuple[str, str, str, str]] = [
        ("sensor:pressure", "Chamber pressure", "pressure", cfg.pressure.unit),
    ]
    sensors.extend((f"sensor:gauge.{g.id}", g.label or g.id,
                    f"gauge.{g.id}", g.unit) for g in cfg.gauges)
    if cfg.stage_temp.enabled:
        sensors.append(("sensor:stage.temp", cfg.stage_temp.label, "stage.temp",
                        cfg.stage_temp.unit))
    sensors.extend((f"sensor:aux.{a.id}", a.label or a.id, f"aux.{a.id}", a.unit)
                   for a in cfg.aux_inputs)
    sensors.extend((f"sensor:inst.{i.id}", i.label or i.id, f"inst.{i.id}", i.unit)
                   for i in cfg.instruments if i.enabled)
    return [{
        "id": target, "kind": "sensor", "label": label,
        "meta": {"read_key": key, "unit": unit},
        "actions": [_wait_action(unit=unit)],
    } for target, label, key, unit in sensors]


def current_prestart_recipe(cfg: ReactorConfig) -> PrestartRecipe:
    """Built-in editable-template source matching the current hard-coded flow."""
    def ref(name: str) -> dict[str, str]:
        return {"parameter": name}

    params = [
        RecipeParameter(id="ar_sccm", label="Ar flow", value_type="number", default=4.0,
                        unit="sccm", minimum=0.0, source="ar_sccm"),
        RecipeParameter(id="valve_delay_s", label="Ar valve settle", value_type="number",
                        default=1.0, unit="s", minimum=0.0, source="valve_delay_s"),
        RecipeParameter(id="hold_s", label="Current hold time", value_type="number",
                        default=5.0, unit="s", minimum=0.0, source="hold_s"),
        RecipeParameter(id="dose_pressure_torr", label="Dose pressure",
                        value_type="number", default=0.02, unit="Torr", minimum=0.0,
                        source="dose_pressure_torr"),
        RecipeParameter(id="fill_pulse_on_s", label="Fill pulse width",
                        value_type="number", default=0.1, unit="s", minimum=0.0,
                        source="fill_pulse_on_s"),
        RecipeParameter(id="fill_pulse_off_s", label="Fill pulse gap",
                        value_type="number", default=0.3, unit="s", minimum=0.0,
                        source="fill_pulse_off_s"),
        RecipeParameter(id="tolerance_frac", label="Fill warning tolerance",
                        value_type="number", default=0.2, minimum=0.0,
                        source="tolerance_frac"),
        RecipeParameter(id="min_current_a", label="Minimum plasma current",
                        value_type="number", default=5e-4, unit="A", minimum=0.0,
                        source="min_current_a"),
        RecipeParameter(id="reignite_pulse_s", label="Reignite pulse",
                        value_type="number", default=0.1, unit="s", minimum=0.0,
                        source="reignite_pulse_s"),
        RecipeParameter(id="reignite_settle_s", label="Reignite settle",
                        value_type="number", default=0.15, unit="s", minimum=0.0,
                        source="reignite_settle_s"),
        RecipeParameter(id="sample_bias_v", label="Sample bias", value_type="number",
                        default=0.0, unit="V", minimum=0.0, source="sample_bias_v"),
        RecipeParameter(id="sample_bias_polarity", label="Sample bias polarity",
                        value_type="choice", default=1, source="sample_bias_polarity",
                        choices=[{"value": 1, "label": "+ positive to stage"},
                                 {"value": -1, "label": "- negative to stage"}]),
    ]
    steps: list[PrestartStep] = []
    for ps in cfg.power_supplies:
        if not ps.enabled or not ps.prestart_output:
            continue
        if ps.sample_bias:
            steps.append(PrestartStep(
                id=f"arm-{ps.id}", target=f"supply:{ps.id}", action="supply.arm_bias",
                args={"volts": ref("sample_bias_v"),
                      "polarity": ref("sample_bias_polarity")}, on_error="continue"))
        elif ps.driver == "keithley_2260b":
            steps.append(PrestartStep(
                id=f"on-{ps.id}", target=f"supply:{ps.id}",
                action="supply.output_on", on_error="continue"))
    steps.extend([
        PrestartStep(id="open-ar", target="valve:ar_pneumatic", action="valve.open"),
        PrestartStep(id="settle-ar", target="system:delay", action="delay.wait",
                     args={"seconds": ref("valve_delay_s")}),
        PrestartStep(id="flow-ar", target="mfc:ar", action="mfc.start_flow",
                     args={"sccm": ref("ar_sccm")}),
        PrestartStep(id="start-fill", target="controller:fill", action="fill.start", args={
            "valve": "valve:rpm_top", "gauge": "sensor:gauge.prec1_dose",
            "target_torr": ref("dose_pressure_torr"),
            "pulse_on_s": ref("fill_pulse_on_s"),
            "pulse_off_s": ref("fill_pulse_off_s"),
            "tolerance_frac": ref("tolerance_frac"),
        }),
        PrestartStep(id="strike-plasma", target="controller:plasma",
                     action="plasma.strike_hold", args={
            "switch": "valve:plasma_ground", "ammeter": "sensor:inst.ammeter",
            "min_current_a": ref("min_current_a"),
            "pulse_s": ref("reignite_pulse_s"),
            "settle_s": ref("reignite_settle_s"), "hold_s": ref("hold_s"),
        }),
    ])

    abort_steps = [
        PrestartStep(id="abort-ar-flow", target="mfc:ar", action="mfc.stop_flow",
                     on_error="continue"),
        PrestartStep(id="abort-ar-valve", target="valve:ar_pneumatic",
                     action="valve.close", on_error="continue"),
        PrestartStep(id="abort-fill", target="controller:fill", action="fill.stop",
                     on_error="continue"),
        PrestartStep(id="abort-fill-valve", target="valve:rpm_top",
                     action="valve.close", on_error="continue"),
    ]
    for ps in cfg.power_supplies:
        if ps.enabled and ps.driver == "glassman_fl":
            abort_steps.append(PrestartStep(
                id=f"abort-{ps.id}", target=f"supply:{ps.id}", action="hv.off",
                on_error="continue"))
    for ps in cfg.power_supplies:
        if ps.enabled and ps.prestart_output and ps.driver == "keithley_2260b":
            abort_steps.append(PrestartStep(
                id=f"abort-{ps.id}", target=f"supply:{ps.id}",
                action="supply.output_off", on_error="continue"))
    abort_steps.append(PrestartStep(
        id="abort-plasma-relay", target="valve:plasma_ground", action="valve.close",
        on_error="continue"))
    return PrestartRecipe(
        id=CURRENT_RECIPE_ID, name="Current pre-start", builtin=True,
        description="The pre-start sequence used before recipe editing was added.",
        parameters=params, start_steps=steps, abort_steps=abort_steps)


def resolve_recipe(recipe: PrestartRecipe, catalog: dict[str, Any],
                   supplied: dict[str, Any] | None = None) -> ResolvedPrestart:
    supplied = supplied or {}
    parameter_values = _resolve_parameters(recipe.parameters, supplied)
    targets = {target["id"]: target for target in catalog["targets"]}
    parameter_defs = {p.id: p for p in recipe.parameters}

    def resolve_steps(steps: list[PrestartStep], section: str) -> tuple[ResolvedStep, ...]:
        result = []
        for index, step in enumerate(steps):
            if not step.enabled:
                continue
            path = f"{section}[{index}]"
            target = targets.get(step.target)
            if target is None:
                raise ValueError(f"{path}.target: unknown target {step.target!r}")
            actions = {a["id"]: a for a in target["actions"]}
            action = actions.get(step.action)
            if action is None:
                raise ValueError(
                    f"{path}.action: {step.action!r} is not supported by {step.target!r}")
            fields = {f["id"]: f for f in action["fields"]}
            extra = sorted(set(step.args) - set(fields))
            if extra:
                raise ValueError(f"{path}.args: unknown field(s) {extra}")
            args: dict[str, Any] = {}
            for field_id, field in fields.items():
                raw = step.args.get(field_id, field.get("default"))
                if raw is None and field.get("required", True):
                    raise ValueError(f"{path}.args.{field_id}: value is required")
                if isinstance(raw, dict) and set(raw) == {"parameter"}:
                    ref = ParameterRef.model_validate(raw).parameter
                    if ref not in parameter_defs:
                        raise ValueError(f"{path}.args.{field_id}: unknown parameter {ref!r}")
                    raw = parameter_values[ref]
                args[field_id] = _coerce_field(raw, field, path=f"{path}.args.{field_id}")
                if field["type"] == "target" and args[field_id] is not None:
                    referenced = targets.get(args[field_id])
                    if referenced is None:
                        raise ValueError(
                            f"{path}.args.{field_id}: unknown target {args[field_id]!r}")
                    expected = field.get("target_kind")
                    if expected and referenced["kind"] != expected:
                        raise ValueError(
                            f"{path}.args.{field_id}: expected {expected}, got "
                            f"{referenced['kind']}")
            summary = _format_summary(action["summary"], target["label"], args)
            result.append(ResolvedStep(id=step.id, target=step.target, action=step.action,
                                       args=args, on_error=step.on_error, summary=summary))
        return tuple(result)

    return ResolvedPrestart(
        recipe_id=recipe.id, revision=recipe.revision, name=recipe.name,
        parameters=parameter_values,
        start_steps=resolve_steps(recipe.start_steps, "start_steps"),
        abort_steps=resolve_steps(recipe.abort_steps, "abort_steps"),
    )


def _resolve_parameters(definitions: list[RecipeParameter], supplied: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for p in definitions:
        raw = supplied[p.id] if p.id in supplied else (
            supplied[p.source] if p.source and p.source in supplied else p.default)
        field = {"type": p.value_type, "label": p.label, "unit": p.unit,
                 "minimum": p.minimum, "maximum": p.maximum, "choices": p.choices,
                 "required": True}
        values[p.id] = _coerce_field(raw, field, path=f"parameters.{p.id}")
    return values


def _coerce_field(value: Any, field: dict[str, Any], *, path: str) -> Any:
    if value is None and not field.get("required", True):
        return None
    kind = field["type"]
    try:
        if kind == "number":
            value = float(value)
        elif kind == "integer":
            value = int(value)
        elif kind == "boolean":
            if isinstance(value, str):
                value = value.strip().lower() in {"1", "true", "yes", "on"}
            else:
                value = bool(value)
        elif kind in {"string", "target"}:
            value = str(value)
        elif kind == "choice":
            allowed = [item["value"] for item in field.get("choices", [])]
            if value not in allowed:
                # HTML forms post scalar values as strings; retain typed choices.
                matched = next((item for item in allowed if str(item) == str(value)), None)
                if matched is None:
                    raise ValueError(f"must be one of {allowed}")
                value = matched
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid {kind} value {value!r}: {exc}") from exc
    minimum, maximum = field.get("minimum"), field.get("maximum")
    if minimum is not None and value < minimum:
        raise ValueError(f"{path}: must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{path}: must be at most {maximum}")
    return value


def target_device_id(target: str) -> str:
    return target.split(":", 1)[1]


def target_read_key(target: str, catalog: dict[str, Any], action: str = "") -> str:
    item = next((t for t in catalog["targets"] if t["id"] == target), None)
    if item is None:
        raise KeyError(target)
    if action:
        act = next((a for a in item["actions"] if a["id"] == action), None)
        if act and act.get("read_key"):
            return str(act["read_key"])
    return str(item.get("meta", {}).get("read_key") or "")


def _format_summary(template: str, target: str, args: dict[str, Any]) -> str:
    values = {"target": target, **args}
    return re.sub(r"\{([^{}]+)\}", lambda m: str(values.get(m.group(1), m.group(0))),
                  template)


def recipe_payload(recipe: PrestartRecipe) -> dict[str, Any]:
    return deepcopy(recipe.model_dump(mode="json"))
