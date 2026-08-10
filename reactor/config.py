"""Typed, validated view of config/reactor.yaml.

Everything hardware-specific enters the program here. If a value is wrong the
error names the YAML key, which is what makes this safe to edit by hand.

Scope note: this describes a UHV chamber with gas dosing. There is no heater
control - the sample-stage thermocouple is read and logged only.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------- #
#  Scaling
# --------------------------------------------------------------------------- #

#: Named gauge curves, so you pick a gauge instead of deriving logarithms.
#: Each entry is (type, gain, offset) mapping volts -> Torr.
#:
#: The wide-range entries use the "PKR-compatible" curve that Pfeiffer and
#: Inficon combination gauges share:  P[mbar] = 10 ** ((V - 6.143) / 0.778),
#: converted here to Torr.
#:
#: These are CANDIDATES, not defaults to trust. On a log gauge a 0.1 V error is
#: a ~30% pressure error and a wrong curve can be off by decades. Identify yours
#: with:  python -m tools.discover_hardware --read-pressure --expect <reading>
GAUGE_PRESETS: dict[str, tuple[str, float, float]] = {
    "pkr251":          ("log10", 1.285347, -8.020826),
    "inficon_bpg402":  ("log10", 1.285347, -8.020826),
    "pfeiffer_pkr361": ("log10", 1.285347, -8.020826),
    # Ion-gauge controllers commonly emit 1 decade per volt. The offset is the
    # exponent at 0 V and differs by family, so the name says which:
    #   ion_gauge_e10 -> P = 10 ** (V - 10) Torr    <- confirmed on this chamber
    #   ion_gauge_e11 -> P = 10 ** (V - 11) Torr
    "ion_gauge_e10": ("log10", 1.0, -10.0),
    "ion_gauge_e11": ("log10", 1.0, -11.0),
    # Capacitance manometers (Baratron and similar): linear, 0-10 V = 0-full
    # scale. Pick the one matching the range printed on the head - reading a
    # 10 Torr head with the 1000 Torr preset is wrong by 100x.
    "baratron_0p1torr":  ("linear", 0.01, 0.0),
    "baratron_1torr":    ("linear", 0.1, 0.0),
    "baratron_2torr":    ("linear", 0.2, 0.0),
    "baratron_10torr":   ("linear", 1.0, 0.0),
    "baratron_100torr":  ("linear", 10.0, 0.0),
    "baratron_1000torr": ("linear", 100.0, 0.0),
}


class Scaling(BaseModel):
    """Volts -> engineering units.

    Either name a `preset` from GAUGE_PRESETS, or give type/gain/offset directly.
    A preset overrides the explicit values.
    """

    type: Literal["linear", "log10"] = "linear"
    gain: float = 1.0
    offset: float = 0.0
    preset: str | None = None

    @model_validator(mode="after")
    def _expand_preset(self) -> "Scaling":
        if self.preset is None:
            return self
        key = self.preset.strip().lower()
        if key not in GAUGE_PRESETS:
            raise ValueError(
                f"unknown gauge preset '{self.preset}'. "
                f"Known: {', '.join(sorted(GAUGE_PRESETS))}"
            )
        kind, gain, offset = GAUGE_PRESETS[key]
        object.__setattr__(self, "type", kind)
        object.__setattr__(self, "gain", gain)
        object.__setattr__(self, "offset", offset)
        return self

    def apply(self, volts: float) -> float:
        raw = volts * self.gain + self.offset
        return 10.0**raw if self.type == "log10" else raw

    def invert(self, value: float) -> float:
        """Engineering units -> volts. Used to sanity-check a curve against a
        known gauge reading."""
        raw = math.log10(max(value, 1e-30)) if self.type == "log10" else value
        return (raw - self.offset) / self.gain if self.gain else 0.0


# --------------------------------------------------------------------------- #
#  Safety
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
#  Inputs
# --------------------------------------------------------------------------- #


class Site(BaseModel):
    name: str = "UHV Reactor"
    loop_hz: float = Field(default=2.0, gt=0, le=50)
    #: The sample-current (DMM6500) is polled on its own faster loop so the
    #: plasma diagnostic and the electron-beam reignite logic run at a finer time
    #: step than the slow thermocouples allow. Telemetry publishes at this rate.
    current_hz: float = Field(default=5.0, gt=0, le=50)
    data_dir: Path = Path("./data")


class Daq(BaseModel):
    chassis: list[str] = Field(default_factory=list)


class PressureCfg(BaseModel):
    """The chamber gauge. Read and logged."""

    label: str = "Chamber Pressure"
    channel: str
    terminal_config: Literal["rse", "nrse", "diff", "pseudodiff"] = "rse"
    input_range_v: tuple[float, float] = (0.0, 10.0)
    scaling: Scaling = Field(default_factory=Scaling)
    unit: str = "Torr"


class GaugeCfg(BaseModel):
    """An additional pressure gauge - a Baratron on a predose volume, a foreline
    gauge, and so on. Read and logged.
    """

    id: str
    label: str = ""
    channel: str
    terminal_config: Literal["rse", "nrse", "diff", "pseudodiff"] = "rse"
    input_range_v: tuple[float, float] = (-10.0, 10.0)
    scaling: Scaling = Field(default_factory=Scaling)
    unit: str = "Torr"


class StageTempCfg(BaseModel):
    """Sample-stage thermocouple. Read and logged; nothing is controlled."""

    enabled: bool = True
    label: str = "Stage Temperature"
    channel: str = ""
    tc_type: Literal["J", "K", "N", "R", "S", "T", "B", "E"] = "K"
    unit: str = "C"


class AuxInputCfg(BaseModel):
    """A generic extra analog input, for channels whose purpose is still being
    established (e.g. the old log's A/B/C/D columns)."""

    id: str
    label: str = ""
    channel: str
    kind: Literal["voltage", "thermocouple"] = "voltage"
    tc_type: Literal["J", "K", "N", "R", "S", "T", "B", "E"] = "K"
    input_range_v: tuple[float, float] = (-10.0, 10.0)
    terminal_config: Literal["rse", "nrse", "diff", "pseudodiff"] = "rse"
    scaling: Scaling = Field(default_factory=Scaling)
    unit: str = "V"


# --------------------------------------------------------------------------- #
#  Outputs
# --------------------------------------------------------------------------- #


class ValveBankCfg(BaseModel):
    """A physical control box. Used only for grouping in the UI."""

    id: str
    label: str = ""
    note: str = ""


class ValveCfg(BaseModel):
    id: str
    label: str = ""
    #: "dose" and "utility" are labels only, used for grouping/description.
    kind: Literal["dose", "utility"] = "utility"
    line: str
    invert: bool = False
    #: which control box this output lives on (see valve_banks)
    bank: str = ""
    #: Informational label that this line's destination was confirmed by
    #: observation. Does not gate actuation.
    identified: bool = False


class MfcRegisterMapCfg(BaseModel):
    """Modbus registers used for writing an MFC setpoint.

    Readings come from the device's HTTP interface, not from Modbus - see
    reactor/devices/mks_mfc.py for why a register sweep could not find flow.
    """

    word_order: Literal["big", "little"] = "big"
    setpoint_read: int = 0xA000
    setpoint_write: int = 0xA000


class MfcCfg(BaseModel):
    id: str
    label: str = ""
    #: Fallback only. The real gas is read from the device.
    gas: str = ""
    host: str
    port: int = 502
    unit_id: int = 1
    register_map: MfcRegisterMapCfg = Field(default_factory=MfcRegisterMapCfg)
    #: Optional pneumatic isolation valve gating this MFC's gas line, by valve
    #: id. When set, requested by the operator: the setpoint cannot be raised
    #: above 0 sccm while this valve is closed, and closing the valve zeroes the
    #: setpoint. Leave unset for an MFC with no known isolation valve.
    isolation_valve: str | None = None


class InstrumentCfg(BaseModel):
    """A bench instrument reached over VISA (USB/LAN/GPIB) or raw serial.

    Polled with `query` and never otherwise commanded, except the `setup`
    commands sent once at connect. Those DO change the instrument's state -
    typically to select a measurement function - so they are listed explicitly
    here rather than hidden in a driver.
    """

    id: str
    label: str = ""
    enabled: bool = False
    driver: Literal["scpi", "keithley_dmm6500"] = "scpi"
    resource: str = ""
    baud: int = 9600
    setup: list[str] = Field(default_factory=list)
    query: str = "READ?"
    unit: str = ""
    #: Reject readings whose magnitude exceeds this (catches overload sentinels
    #: such as Keithley's 9.9e37).
    overload_above: float = 1e30


class EllipsometerCfg(BaseModel):
    """Film Sense FS-1 in-situ ellipsometer.

    Read-only, over the instrument's live-broadcast TCP stream (see
    reactor/devices/ellipsometer.py): the reactor subscribes to the
    per-measurement stream on port 4001 and timestamps each point with its own
    clock; it never writes to or commands the instrument (the trigger sockets
    on 4000/4010 are deliberately untouched). Data flows only while a dynamic
    acquisition is running on the FS-1 itself.

    This exists so a *refit* dynamic file - downloaded from the FS-1 after a
    run - can be put back onto the reactor clock and merged with the run log
    (reactor/analysis/ellipsometer_merge.py). The stream's live thickness is
    the instrument's uncalibrated fit and is NOT treated as truth.
    """

    enabled: bool = False
    label: str = "FS-1 Ellipsometer"
    host: str = ""
    port: int = 4001
    #: Seconds of stream silence that ends an acquisition (points arrive ~1 Hz;
    #: on stop the socket just goes quiet, with no end marker). The next point
    #: after this gap - or a point-index reset to 1 - opens a fresh per-run
    #: sidecar file.
    idle_gap_s: float = 5.0


class LoggingCfg(BaseModel):
    filename_suffix: str = "General"
    #: Column name -> snapshot key. Explicit so the file layout is visible and
    #: editable without touching code.
    #:
    #: The old LabVIEW logger wrote Time/Pressure/QCM Mass/A/B/C/D. There is no
    #: QCM, and A-D have not been identified, so they are omitted rather than
    #: written as meaningless zeros.
    columns: dict[str, str] = Field(
        default_factory=lambda: {
            "Time": "_elapsed",
            "Pressure": "pressure",
            "Stage T": "stage.temp",
            "Current": "inst.ammeter",
        }
    )
    extended_log: bool = True


# --------------------------------------------------------------------------- #
#  Root
# --------------------------------------------------------------------------- #


class ReactorConfig(BaseModel):
    version: int = 1
    site: Site = Field(default_factory=Site)
    daq: Daq = Field(default_factory=Daq)
    pressure: PressureCfg
    gauges: list[GaugeCfg] = Field(default_factory=list)
    stage_temp: StageTempCfg = Field(default_factory=StageTempCfg)
    aux_inputs: list[AuxInputCfg] = Field(default_factory=list)
    valve_banks: list[ValveBankCfg] = Field(default_factory=list)
    valves: list[ValveCfg] = Field(default_factory=list)
    mfcs: list[MfcCfg] = Field(default_factory=list)
    instruments: list[InstrumentCfg] = Field(default_factory=list)
    ellipsometer: EllipsometerCfg = Field(default_factory=EllipsometerCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)

    @model_validator(mode="after")
    def _check_ids_unique(self) -> "ReactorConfig":
        for label, ids in (
            ("valves", [v.id for v in self.valves]),
            ("valve_banks", [b.id for b in self.valve_banks]),
            ("gauges", [g.id for g in self.gauges]),
            ("mfcs", [m.id for m in self.mfcs]),
            ("instruments", [i.id for i in self.instruments]),
            ("aux_inputs", [a.id for a in self.aux_inputs]),
        ):
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                raise ValueError(f"{label}: duplicate id(s) {sorted(dupes)}")

        # A valve pointing at a bank that does not exist is a typo worth catching.
        banks = {b.id for b in self.valve_banks}
        for v in self.valves:
            if v.bank and v.bank not in banks:
                raise ValueError(
                    f"valve '{v.id}': bank '{v.bank}' is not in valve_banks "
                    f"({sorted(banks) or 'none defined'})"
                )

        # Two valves on the same physical line would fight each other.
        lines = [v.line for v in self.valves if v.line]
        dup_lines = {ln for ln in lines if lines.count(ln) > 1}
        if dup_lines:
            raise ValueError(f"valves: same DAQ line used twice: {sorted(dup_lines)}")
        return self


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "reactor.yaml"


def load_config(path: Path | str | None = None) -> ReactorConfig:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return ReactorConfig.model_validate(data)
