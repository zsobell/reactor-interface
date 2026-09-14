"""Typed views of UI payloads, preserving legacy coercion and raw reports.

These are structural models, not a new validation policy. Numeric conversions
use Python float/int, enable flags use truthiness, and Step/Recipe keep their
existing schema checks. Pre-start converts each stage only when reached because
moving a failing conversion changes which hardware commands precede cleanup.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

Payload = dict[str, Any]
GAS_SCHEDULE_MFCS = ("h2", "n2")


@dataclass(frozen=True)
class FillParameters:
    valve: str
    gauge: str
    target_torr: float
    pulse_on_s: float
    pulse_off_s: float
    tolerance_frac: float

    @classmethod
    def normalize(cls, p: Payload) -> FillParameters:
        return cls(p.get("fill_valve", "rpm_top"), p.get("gauge", "gauge.prec1_dose"),
                   float(p.get("dose_pressure_torr", 0.02)),
                   float(p.get("fill_pulse_on_s", 0.10)), float(p.get("fill_pulse_off_s", 0.30)),
                   float(p.get("tolerance_frac", 0.20)))


@dataclass(frozen=True)
class BeamParameters:
    switch: str
    ammeter: str
    min_current: float
    pulse_s: float
    settle_s: float

    @classmethod
    def normalize(cls, p: Payload) -> BeamParameters:
        return cls(p.get("plasma_switch", "plasma_ground"), p.get("ammeter", "inst.ammeter"),
                   float(p.get("min_current_a", 5.0e-4)),
                   float(p.get("reignite_pulse_s", 0.10)), float(p.get("reignite_settle_s", 0.15)))


@dataclass(frozen=True)
class GasParameters:
    mfc: str
    order: Literal["first", "second"]
    pct: float
    flow_sccm: float

    @classmethod
    def enabled(cls, p: Payload) -> tuple[GasParameters, ...]:
        result = []
        for mfc in GAS_SCHEDULE_MFCS:
            if not p.get(f"{mfc}_gas_enable"):
                continue
            order = p.get(f"{mfc}_gas_order", "first")
            if order not in ("first", "second"):
                raise ValueError(f"{mfc}: gas order must be 'first' or 'second'")
            result.append(cls(mfc, order, float(p.get(f"{mfc}_gas_pct", 100.0)),
                              float(p.get(f"{mfc}_gas_flow_sccm", 0.0))))
        return tuple(result)


@dataclass(frozen=True)
class RunParameters:
    mode: Literal["ald", "cvd"]
    name: str
    cycles: int
    dose_valve: str
    dose_s: float
    pump_a_s: float
    beam_s: float | None
    pump_b_s: float | None
    fill: FillParameters
    beam: BeamParameters
    gases: tuple[GasParameters, ...]
    gas_overlap_s: float
    ar_mfc: str
    ar_valve: str
    ar_close_delay_s: float
    _raw: Payload = field(repr=False, compare=False)

    @property
    def raw(self) -> Payload:
        """Independent original payload: keep unknown keys and original values."""
        return deepcopy(self._raw)

    @classmethod
    def normalize(cls, p: Payload | RunParameters, *, mode: Literal["ald", "cvd"]) -> RunParameters:
        if isinstance(p, cls):
            if p.mode != mode:
                raise ValueError("run parameters belong to a different recipe mode")
            return p
        raw = deepcopy(dict(p))
        # Disabled gases and CVD-only ignored ALD fields are never coerced.
        gases = GasParameters.enabled(raw)
        return cls(mode=mode, name=raw.get("name", "ALD + e-beam (precursor 1)" if mode == "ald"
                                          else "EE-CVD (precursor 1)"),
                   cycles=int(raw.get("cycles", 100)), dose_valve=raw.get("dose_valve", "prec1"),
                   dose_s=float(raw.get("dose_s", 0.05)), pump_a_s=float(raw.get("pump_a_s", 10.0)),
                   beam_s=float(raw.get("beam_s", 5.0)) if mode == "ald" else None,
                   pump_b_s=float(raw.get("pump_b_s", 10.0)) if mode == "ald" else None,
                   fill=FillParameters.normalize(raw), beam=BeamParameters.normalize(raw),
                   gases=gases, gas_overlap_s=float(raw.get("gas_overlap_s", 0.0)),
                   ar_mfc=raw.get("ar_mfc", "ar"), ar_valve=raw.get("ar_valve", "ar_pneumatic"),
                   ar_close_delay_s=float(raw.get("ar_close_delay_s", 10.0)), _raw=raw)


@dataclass(frozen=True)
class PrestartOpening:
    ar_valve: str
    ar_mfc: str
    ar_sccm: float
    valve_delay_s: float
    hold_s: float
    beam: BeamParameters


@dataclass(frozen=True)
class SupplyParameters:
    sample_bias_v: float
    polarity: int


@dataclass(frozen=True)
class PrestartParameters:
    _raw: Payload = field(repr=False)

    @classmethod
    def normalize(cls, p: Payload | PrestartParameters) -> PrestartParameters:
        return p if isinstance(p, cls) else cls(deepcopy(dict(p)))

    @property
    def raw(self) -> Payload:
        return deepcopy(self._raw)

    def opening(self) -> PrestartOpening:
        p = self._raw
        return PrestartOpening(p.get("ar_valve", "ar_pneumatic"), p.get("ar_mfc", "ar"),
                               float(p.get("ar_sccm", 4.0)), float(p.get("valve_delay_s", 1.0)),
                               float(p.get("hold_s", 5.0)), BeamParameters.normalize(p))

    def supplies(self) -> SupplyParameters:
        return SupplyParameters(float(self._raw.get("sample_bias_v", 0.0)),
                                int(self._raw.get("sample_bias_polarity", 1)))

    def fill(self) -> FillParameters:
        return FillParameters.normalize(self._raw)
