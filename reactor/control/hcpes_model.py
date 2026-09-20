"""Pure HCPES characterization plans, expansion, and polarity campaigns.

Nothing in this module connects to or commands hardware.  It turns an
operator-reviewed, versioned plan into deterministic points and preview data;
runtime ownership and dispatch belong to the HCPES controller.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from itertools import islice
import json
import math
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import ReactorConfig


SCHEMA_VERSION = 1
CURRENT_PRESTART_ID = "current-hcpes-prestart"
CURRENT_PLAN_ID = "current-hcpes-plan"
AR_TARGET = "mfc:ar"
STAGE_TARGET = "supply:stage_bias"
REQUIRED_SUPPLY_TARGETS = (
    STAGE_TARGET,
    "supply:grid_bias",
    "supply:collimating",
    "supply:steering",
)


def _finite_nonnegative(value: float, path: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if value < 0:
        raise ValueError(f"{path} must be nonnegative")
    return value


def _linear_count(start: float, stop: float, step: float) -> int:
    """Inclusive linear count, rejecting an ambiguous partial final step."""
    a, b, stride = Decimal(str(start)), Decimal(str(stop)), Decimal(str(step))
    span = abs(b - a)
    quotient = span / stride
    integral = quotient.to_integral_value()
    if quotient != integral:
        raise ValueError("linear step must land exactly on stop")
    return int(integral) + 1


class SweepAxis(BaseModel):
    """One ordered plan block.  Plan order is outer-to-inner loop order."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1)
    mode: Literal["fixed", "list", "linear", "locked_zero"]
    value: float | None = None
    values: list[float] = Field(default_factory=list)
    start: float | None = None
    stop: float | None = None
    step: float | None = None

    @model_validator(mode="after")
    def _valid_definition(self) -> "SweepAxis":
        supplied_linear = any(v is not None for v in (self.start, self.stop, self.step))
        if self.mode == "fixed":
            if self.value is None:
                raise ValueError("fixed axis requires value")
            if self.values or supplied_linear:
                raise ValueError("fixed axis accepts only value")
            self.value = _finite_nonnegative(self.value, "value")
        elif self.mode == "list":
            if self.value is not None or supplied_linear:
                raise ValueError("list axis accepts only values")
            if not self.values:
                raise ValueError("list axis requires at least one value")
            self.values = [_finite_nonnegative(v, "values") for v in self.values]
            if len(self.values) != len(set(self.values)):
                raise ValueError("list axis values must be unique")
        elif self.mode == "linear":
            if self.value is not None or self.values:
                raise ValueError("linear axis accepts only start, stop, and step")
            if not supplied_linear or None in (self.start, self.stop, self.step):
                raise ValueError("linear axis requires start, stop, and step")
            self.start = _finite_nonnegative(self.start, "start")
            self.stop = _finite_nonnegative(self.stop, "stop")
            self.step = _finite_nonnegative(self.step, "step")
            if self.step <= 0:
                raise ValueError("step must be greater than zero")
            _linear_count(self.start, self.stop, self.step)
        else:
            if self.value is not None or self.values or supplied_linear:
                raise ValueError("locked_zero axis accepts no values")
        return self

    @property
    def locked(self) -> bool:
        return self.mode == "locked_zero"

    @property
    def count(self) -> int:
        if self.mode in {"fixed", "locked_zero"}:
            return 1
        if self.mode == "list":
            return len(self.values)
        return _linear_count(self.start, self.stop, self.step)  # type: ignore[arg-type]

    def value_at(self, index: int) -> float:
        if index < 0 or index >= self.count:
            raise IndexError(index)
        if self.mode == "locked_zero":
            return 0.0
        if self.mode == "fixed":
            return float(self.value)
        if self.mode == "list":
            return self.values[index]
        direction = 1 if self.stop >= self.start else -1  # type: ignore[operator]
        value = Decimal(str(self.start)) + direction * index * Decimal(str(self.step))
        return float(value)

    def iter_values(self) -> Iterator[float]:
        for index in range(self.count):
            yield self.value_at(index)


class StabilityProfile(BaseModel):
    """One independently timed and thresholded stage-current stability gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stable_window_s: float = Field(gt=0)
    maximum_wait_s: float = Field(gt=0)
    max_drift_a_per_min: float = Field(gt=0)

    @model_validator(mode="after")
    def _window_fits_deadline(self) -> "StabilityProfile":
        if self.maximum_wait_s < self.stable_window_s:
            raise ValueError("stability maximum wait must cover the full stable window")
        return self


def _establishment_profile() -> StabilityProfile:
    return StabilityProfile(
        stable_window_s=20.0,
        maximum_wait_s=60.0,
        max_drift_a_per_min=0.0001,
    )


def _parameter_profile() -> StabilityProfile:
    return StabilityProfile(
        stable_window_s=3.0,
        maximum_wait_s=10.0,
        max_drift_a_per_min=0.0003,
    )


class HcpesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    establishment: StabilityProfile = Field(default_factory=_establishment_profile)
    parameter_change: StabilityProfile = Field(default_factory=_parameter_profile)
    parameter_settle_s: float = Field(default=3.0, ge=0)
    condition_settle_mode: Literal["time", "current"] = "time"
    plasma_min_current_a: float = Field(default=0.0001, ge=0)
    recovery_window_s: float = Field(default=30.0, gt=0)
    reignite_pulse_s: float = Field(default=1.0, gt=0)
    reignite_settle_s: float = Field(default=1.0, ge=0)
    qualified_samples: int = Field(default=5, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_settings(cls, value):
        """Read plans saved before stability was split and cadence removed."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        # Qualified collection is driven by fresh instrument readings.  Older
        # plans added another delay between those readings; accept and discard
        # that obsolete setting so saved operator plans continue to load.
        data.pop("sample_interval_s", None)
        legacy = {
            "stable_window_s": data.pop("stability_window_s", None),
            "maximum_wait_s": data.pop("stability_max_wait_s", None),
            "max_drift_a_per_min": data.pop("max_drift_a_per_min", None),
        }
        if any(item is not None for item in legacy.values()):
            if "establishment" in data:
                raise ValueError(
                    "cannot mix legacy flat stability settings with establishment")
            defaults = _establishment_profile().model_dump(mode="json")
            data["establishment"] = {
                key: defaults[key] if item is None else item
                for (key, item) in legacy.items()
            }
        return data


class HcpesPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    revision: int = Field(default=1, ge=1)
    builtin: bool = False
    prestart_recipe_id: str = Field(default=CURRENT_PRESTART_ID, min_length=1)
    stage_polarity: Literal[-1, 1] = 1
    settings: HcpesSettings = Field(default_factory=HcpesSettings)
    axes: list[SweepAxis]

    @model_validator(mode="after")
    def _unique_targets(self) -> "HcpesPlan":
        targets = [axis.target for axis in self.axes]
        if len(targets) != len(set(targets)):
            raise ValueError("axis targets must be unique")
        return self


class AxisCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str
    label: str
    role: Literal[
        "ar_flow", "background_flow", "stage_bias", "grid_bias",
        "collimating_current", "steering_current",
    ]
    quantity: Literal["flow", "voltage", "current"]
    unit: Literal["sccm", "V", "A"]
    read_key: str
    locked_zero_allowed: bool = False


def axis_capabilities(cfg: ReactorConfig) -> tuple[AxisCapability, ...]:
    """Configured HCPES targets, in stable default plan order."""
    mfcs = {m.id: m for m in cfg.mfcs}
    supplies = {
        ps.id: ps for ps in cfg.power_supplies
        if ps.enabled and ps.driver == "keithley_2260b"
    }
    result: list[AxisCapability] = []
    if ar := mfcs.get("ar"):
        result.append(AxisCapability(
            target=AR_TARGET, label=ar.label or "Ar", role="ar_flow",
            quantity="flow", unit="sccm", read_key="mfc.ar.flow",
        ))
    for supply_id, role, quantity in (
        ("stage_bias", "stage_bias", "voltage"),
        ("collimating", "collimating_current", "current"),
        ("steering", "steering_current", "current"),
        ("grid_bias", "grid_bias", "voltage"),
    ):
        if supply := supplies.get(supply_id):
            channel = "current" if quantity == "current" else "voltage"
            result.append(AxisCapability(
                target=f"supply:{supply_id}", label=supply.label or supply_id,
                role=role, quantity=quantity, unit="A" if quantity == "current" else "V",
                read_key=f"psu.{supply_id}.{channel}",
            ))
    for mfc in cfg.mfcs:
        if mfc.id == "ar":
            continue
        result.append(AxisCapability(
            target=f"mfc:{mfc.id}", label=mfc.label or mfc.id,
            role="background_flow", quantity="flow", unit="sccm",
            read_key=f"mfc.{mfc.id}.flow", locked_zero_allowed=True,
        ))
    return tuple(result)


def capability_catalog(cfg: ReactorConfig) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "axes": [capability.model_dump(mode="json")
                 for capability in axis_capabilities(cfg)],
    }


def current_hcpes_plan(cfg: ReactorConfig) -> HcpesPlan:
    """Code-owned safe template used to seed the visual plan library.

    Required controls start as explicit fixed zeroes and every background MFC
    starts locked at zero.  The protected template therefore supplies the
    complete configured shape without inventing operating setpoints; an
    operator duplicates and edits it before launch.
    """
    axes = [
        SweepAxis(
            target=capability.target,
            mode="locked_zero" if capability.locked_zero_allowed else "fixed",
            **({} if capability.locked_zero_allowed else {"value": 0.0}),
        )
        for capability in axis_capabilities(cfg)
    ]
    return HcpesPlan(
        id=CURRENT_PLAN_ID,
        name="Current HCPES template",
        description=(
            "Protected configured-device template. Duplicate it, define the "
            "parameter space, review the preview, and save before starting."
        ),
        builtin=True,
        axes=axes,
    )


@dataclass(frozen=True)
class SweepPoint:
    index: int
    values: tuple[tuple[str, float], ...]
    stage_polarity: int

    @property
    def setpoints(self) -> dict[str, float]:
        return dict(self.values)

    @property
    def signed_stage_bias_v(self) -> float:
        return self.setpoints[STAGE_TARGET] * self.stage_polarity


class NominalEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    points: int
    parameter_changes: int
    qualified_samples: int
    best_case_s: float
    startup_timeout_case_s: float


@dataclass(frozen=True)
class ResolvedHcpesPlan:
    plan: HcpesPlan
    capabilities: tuple[AxisCapability, ...]

    @property
    def active_axes(self) -> tuple[SweepAxis, ...]:
        return tuple(axis for axis in self.plan.axes if not axis.locked)

    @property
    def point_count(self) -> int:
        return math.prod(axis.count for axis in self.active_axes)

    def iter_points(self) -> Iterator[SweepPoint]:
        active = self.active_axes
        selected: dict[str, float] = {}
        index = 0

        def walk(depth: int) -> Iterator[SweepPoint]:
            nonlocal index
            if depth == len(active):
                index += 1
                values = tuple(
                    (axis.target, 0.0 if axis.locked else selected[axis.target])
                    for axis in self.plan.axes
                )
                yield SweepPoint(index, values, self.plan.stage_polarity)
                return
            axis = active[depth]
            for value in axis.iter_values():
                selected[axis.target] = value
                yield from walk(depth + 1)

        yield from walk(0)

    @property
    def parameter_change_count(self) -> int:
        """Writes needed when only changed active setpoints are written."""
        prefix = 1
        changes = 0
        for axis in self.active_axes:
            prefix *= axis.count
            changes += 1 if axis.count == 1 else prefix
        return changes

    def estimate(self) -> NominalEstimate:
        samples = self.point_count * self.plan.settings.qualified_samples
        # Qualified readings are count-based and arrive with fresh instrument
        # telemetry.  They have no plan-owned duration to add to this estimate.
        establishment = self.plan.settings.establishment
        parameter = self.plan.settings.parameter_change
        if self.plan.settings.condition_settle_mode == "time":
            # Initial setpoints are programmed before plasma exists and do not
            # consume per-change settling time.  Startup has one current gate;
            # only later changed parameters use the fixed delay.
            later_changes = max(0, self.parameter_change_count - len(self.active_axes))
            fixed = later_changes * self.plan.settings.parameter_settle_s
            best_case = establishment.stable_window_s + fixed
            timeout_case = establishment.maximum_wait_s + fixed
        else:
            # Current mode replaces per-change delays with one current gate per
            # later condition after all changed setpoints have been applied.
            later_points = max(0, self.point_count - 1)
            best_case = (establishment.stable_window_s
                         + later_points * parameter.stable_window_s)
            timeout_case = (establishment.maximum_wait_s
                             + later_points * parameter.maximum_wait_s)
        return NominalEstimate(
            points=self.point_count,
            parameter_changes=self.parameter_change_count,
            qualified_samples=samples,
            best_case_s=best_case,
            startup_timeout_case_s=timeout_case,
        )

    def compatibility_payload(self) -> dict:
        """Plan identity excluding run names, revisions, and physical polarity."""
        return {
            "schema_version": self.plan.schema_version,
            "prestart_recipe_id": self.plan.prestart_recipe_id,
            "settings": self.plan.settings.model_dump(mode="json"),
            "axes": [axis.model_dump(mode="json", exclude_none=True)
                     for axis in self.plan.axes],
            "capabilities": [cap.model_dump(mode="json") for cap in self.capabilities],
        }

    @property
    def compatibility_signature(self) -> str:
        canonical = json.dumps(
            self.compatibility_payload(), sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return sha256(canonical).hexdigest()


def resolve_plan(plan: HcpesPlan, cfg: ReactorConfig) -> ResolvedHcpesPlan:
    """Validate one complete plan against the currently configured reactor."""
    snapshot = HcpesPlan.model_validate(plan.model_dump(mode="json"))
    if snapshot.prestart_recipe_id != CURRENT_PRESTART_ID:
        raise ValueError(
            f"unsupported HCPES pre-start profile: {snapshot.prestart_recipe_id!r}")
    capabilities = axis_capabilities(cfg)
    by_target = {cap.target: cap for cap in capabilities}
    configured = set(by_target)
    supplied = {axis.target for axis in snapshot.axes}
    unknown = supplied - configured
    missing = configured - supplied
    if unknown:
        raise ValueError(f"unknown HCPES axis target(s): {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing HCPES axis target(s): {sorted(missing)}")
    required = {AR_TARGET, *REQUIRED_SUPPLY_TARGETS}
    absent_hardware = required - configured
    if absent_hardware:
        raise ValueError(f"required HCPES hardware is not configured: {sorted(absent_hardware)}")
    for axis in snapshot.axes:
        capability = by_target[axis.target]
        if axis.locked and not capability.locked_zero_allowed:
            raise ValueError(f"{axis.target} cannot be locked at zero")
    return ResolvedHcpesPlan(snapshot, capabilities)


def clone_opposite_polarity(
    plan: HcpesPlan, *, new_id: str, new_name: str,
) -> HcpesPlan:
    payload = plan.model_dump(mode="json")
    payload.update(
        id=new_id, name=new_name, revision=1,
        stage_polarity=-plan.stage_polarity, builtin=False,
    )
    return HcpesPlan.model_validate(payload)


class CampaignSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    polarity: Literal[-1, 1]
    plan_signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class PolarityCampaign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=100)
    sessions: list[CampaignSession] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _compatible_sessions(self) -> "PolarityCampaign":
        ids = [session.session_id for session in self.sessions]
        polarities = [session.polarity for session in self.sessions]
        signatures = {session.plan_signature for session in self.sessions}
        if len(ids) != len(set(ids)):
            raise ValueError("campaign session ids must be unique")
        if len(polarities) != len(set(polarities)):
            raise ValueError("campaign cannot contain the same polarity twice")
        if len(signatures) != 1:
            raise ValueError("campaign sessions do not have matching HCPES plans")
        return self

    @property
    def complete(self) -> bool:
        return {session.polarity for session in self.sessions} == {-1, 1}


def preview_values(axis: SweepAxis, limit: int = 8) -> list[float]:
    """Bounded UI helper; notably does not materialize a large linear axis."""
    return list(islice(axis.iter_values(), max(0, limit)))
