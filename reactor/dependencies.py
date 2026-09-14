"""Construction dependencies: production adapters and per-instance state paths.

Constructing adapters does not connect or actuate them. Supervisor owns their
connection lifecycle; tests substitute factories, not global module variables.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

from .devices.nidaq import NiDaqBackend
from .devices.mks_mfc import MfcRegisters, MksMfc
from .devices.instrument import ScpiInstrument
from .devices.glassman_fl import GlassmanFL
from .devices.keithley_2260b import Keithley2260B
from .devices.ellipsometer import EllipsometerClient


@dataclass(frozen=True)
class StatePaths:
    labels: Path
    valves: Path
    run_name: Path

    @classmethod
    def in_directory(cls, directory: Path) -> "StatePaths":
        return cls(directory / "labels.json", directory / "valve_state.json",
                   directory / "last_run.json")


def make_mfc(cfg):
    return MksMfc(cfg, MfcRegisters(setpoint_read=cfg.register_map.setpoint_read,
                                  setpoint_write=cfg.register_map.setpoint_write,
                                  word_order=cfg.register_map.word_order))


def make_supply(cfg):
    return GlassmanFL(cfg) if cfg.driver == "glassman_fl" else Keithley2260B(cfg)


def make_ellipsometer(cfg, *, on_point, on_state):
    if not cfg.enabled or not cfg.host:
        return None
    return EllipsometerClient(cfg.host, cfg.port, on_point=on_point, on_state=on_state)


@dataclass(frozen=True)
class DeviceFactory:
    daq: Callable[..., Any] = NiDaqBackend
    mfc: Callable[..., Any] = make_mfc
    instrument: Callable[..., Any] = ScpiInstrument
    supply: Callable[..., Any] = make_supply
    ellipsometer: Callable[..., Any] = make_ellipsometer
