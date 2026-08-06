"""A "no physics" virtual reactor: fake devices, real everything else.

The fakes replace exactly the three classes Supervisor.start() constructs -
NiDaqBackend, MksMfc, ScpiInstrument - and nothing else. Supervisor itself,
RecipeRunner, and every control-logic method (set_valve, set_mfc_setpoint,
start_fill_regulation, start_prestart, the recipe engine's whole state
machine) run completely unmodified, exactly as they would against the real
reactor. Only the boundary where bytes would otherwise cross onto a wire -
DAQmx, Modbus, VISA - is replaced with an in-memory stand-in a test can pose
and inspect.

"No physics" is deliberate: the fakes do not model solenoid response time,
MFC settling curves, or plasma strike probability. A written value is
readable back immediately. That is the right level of fidelity for what
this is used for - verifying that the SOFTWARE reacts correctly to a given
sequence of readings (sequencing, timing, edge cases like an abort mid-dose
or a plasma dropout mid-reignite) - not for predicting how the physical
reactor behaves. Nothing here can answer "will the plasma actually
reignite in 150ms" or "is ai1 really precursor 1" - those are hardware
questions, answered only by running the real thing. See tests/README.md.

Usage:

    from reactor.testing.virtual_reactor import VirtualReactor

    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3      # "plasma is lit"
        vr.daq.raw["gauge.prec1_dose"] = 0.021         # volts, pre-scaling
        await vr.sup.set_valve("prec1", True, reason="test")
        assert vr.daq.do_state["prec1"] is True

        await vr.sup.recipes.start(build_cvd_recipe({...}))
        while vr.sup.recipes.busy:
            await vr.tick()                            # advance one sample
            await asyncio.sleep(0)                      # let other tasks run
"""

from __future__ import annotations

import contextlib
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import supervisor as supervisor_module
from ..config import load_config
from ..devices.base import Device, Reading
from ..supervisor import Supervisor

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "reactor.yaml"
)


class FakeDaq:
    """Stands in for NiDaqBackend. `read_ai()` reports back whatever `raw[key]`
    currently holds for each configured channel; `write_do` just records what
    was commanded. No timing, no noise, no cross-talk between lines.

    Value semantics per channel match exactly what the real driver hands to
    Supervisor, so a test sets the SAME thing Supervisor would see:
      - voltage channels (`pressure`, `gauge.*`, voltage-kind aux inputs):
        raw VOLTS, pre-scaling - Supervisor applies `scaling.apply()` itself,
        same as it would to a real reading.
      - thermocouple channels (`stage.temp`, thermocouple-kind aux inputs):
        already degrees C - that is what `add_ai_thrmcpl_chan` returns for
        real, and Supervisor does not re-scale it.
    Missing keys read back as 0.0 (interpret per the channel's own units
    above - 0.0 V for a voltage channel, 0.0 C for a thermocouple).
    """

    def __init__(self) -> None:
        self._plan = None
        self.raw: dict[str, float] = {}
        self.do_state: dict[str, bool] = {}
        self.do_writes: list[tuple[float, str, bool]] = []
        self.id_state: dict[str, bool] = {}
        self.last_error = ""

    @property
    def input_count(self) -> int:
        return len(self._plan.ai) if self._plan else 0

    async def configure(self, plan) -> None:
        self._plan = plan

    async def read_ai(self) -> list[Reading]:
        if self._plan is None:
            return []
        return [
            Reading(key=s.key, value=self.raw.get(s.key, 0.0), unit=s.unit, ok=True)
            for s in self._plan.ai
        ]

    async def write_do(self, key: str, value: bool) -> None:
        self.do_state[key] = value
        self.do_writes.append((time.time(), key, value))

    async def id_write(self, line: str, state: bool) -> None:
        self.id_state[line] = state

    async def id_release(self, line: str) -> None:
        self.id_state.pop(line, None)

    async def id_release_all(self) -> None:
        self.id_state.clear()

    async def close(self) -> None:
        pass


class FakeMfc(Device):
    """Stands in for MksMfc. `flow_sccm` tracks the commanded setpoint
    exactly and instantly (no physics) - set it directly after a write to
    simulate lag, undershoot, or a stuck valve for a specific test."""

    def __init__(self, cfg) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self.full_scale_sccm: float = {"Ar": 29.0, "H2": 10.0, "N2": 50.0}.get(
            cfg.gas, 50.0)
        self.gas = cfg.gas
        self.model = "VIRTUAL"
        self.serial = "0"
        self.valve_type = "N.C."
        self.device_mode = "virtual"
        self.health = {"over_temp": "No", "open_circuit": "No", "interface_error": "No"}
        self.commanded_sccm: float = 0.0
        self.flow_sccm: float = 0.0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def read(self) -> list[Reading]:
        p = f"mfc.{self.id}"
        out = [
            Reading(key=f"{p}.flow", value=self.flow_sccm, unit="sccm"),
            Reading(key=f"{p}.setpoint", value=self.commanded_sccm, unit="sccm"),
            Reading(key=f"{p}.temp", value=22.0, unit="C"),
            Reading(key=f"{p}.full_scale", value=self.full_scale_sccm, unit="sccm"),
        ]
        if self.full_scale_sccm:
            out.append(Reading(key=f"{p}.flow_pct",
                               value=100.0 * self.flow_sccm / self.full_scale_sccm,
                               unit="%"))
        return out

    async def set_setpoint_sccm(self, sccm: float) -> float:
        self.commanded_sccm = float(sccm)
        self.flow_sccm = float(sccm)
        return self.commanded_sccm

    def status(self) -> dict:
        return {
            **super().status(),
            "host": f"virtual:{self.cfg.id}",
            "gas": self.gas,
            "full_scale_sccm": self.full_scale_sccm,
            "model": self.model,
            "serial": self.serial,
            "valve_type": self.valve_type,
            "device_mode": self.device_mode,
            "health": self.health,
            "device_info": f"VIRTUAL  {self.gas}  FS {self.full_scale_sccm:g} sccm",
            "commanded_sccm": self.commanded_sccm,
        }


class FakeInstrument(Device):
    """Stands in for ScpiInstrument (the DMM6500). Set `.value` directly to
    control what the next read() reports - e.g. `inst.value = 0.0` to
    simulate "plasma out", or `.ok = False` to simulate a dropped
    connection."""

    def __init__(self, cfg) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self.identity = "VIRTUAL INSTRUMENT"
        self.setup_sent: list[str] = []
        self.value: float | None = 0.0
        self.ok = True

    async def connect(self) -> None:
        self.connected = True
        self.setup_sent = list(self.cfg.setup)

    async def disconnect(self) -> None:
        self.connected = False

    async def read(self) -> list[Reading]:
        key = f"inst.{self.id}"
        if not self.ok or self.value is None:
            return [self._bad(key, self.cfg.unit, "virtual: not ok")]
        return [Reading(key=key, value=self.value, unit=self.cfg.unit)]


class VirtualReactor:
    """A real Supervisor wired to fake devices instead of real hardware.

    Two safety guarantees, both load-bearing - a test that forgets everything
    else still cannot damage the real project state:

    - `cfg.site.data_dir` is redirected to a throwaway temp directory, so
      DataLogger and the automatic run-export never touch the project's real
      `data/`.
    - `VALVE_STATE_PATH` / `LABELS_PATH` (hardcoded in supervisor.py as
      module-level constants, not per-instance - there is no constructor
      argument for them) are monkeypatched to that same temp directory for
      the life of this object and restored on exit, so a test can never
      overwrite the real `config/valve_state.json` or `config/labels.json`.

    Background loops (`_control_loop`, `_current_loop`, `_reconnect_loop`)
    are deliberately NOT started - a real timer racing test assertions would
    make tests flaky for no benefit. Call `await vr.tick()` to advance
    telemetry by exactly one sample, on your own schedule.

    Always use as an async context manager so teardown (including restoring
    the monkeypatched paths) is guaranteed:

        async with VirtualReactor() as vr:
            ...
    """

    def __init__(self, config_path: Path | str | None = None) -> None:
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._orig_valve_state_path = None
        self._orig_labels_path = None
        self.sup: Supervisor | None = None
        self.daq: FakeDaq | None = None
        self.mfcs: dict[str, FakeMfc] = {}
        self.instruments: dict[str, FakeInstrument] = {}

    async def __aenter__(self) -> "VirtualReactor":
        self._tmpdir = tempfile.TemporaryDirectory(prefix="virtual_reactor_")
        tmp = Path(self._tmpdir.name)

        # Must happen before Supervisor(cfg) - __init__ itself reads these to
        # restore last-commanded valve state.
        self._orig_valve_state_path = supervisor_module.VALVE_STATE_PATH
        self._orig_labels_path = supervisor_module.LABELS_PATH
        supervisor_module.VALVE_STATE_PATH = tmp / "valve_state.json"
        supervisor_module.LABELS_PATH = tmp / "labels.json"

        cfg = load_config(self.config_path)
        cfg.site.data_dir = str(tmp / "data")

        sup = Supervisor(cfg)
        self.daq = FakeDaq()
        sup._plan = sup._build_plan()
        await self.daq.configure(sup._plan)
        sup.daq = self.daq

        for m in cfg.mfcs:
            dev = FakeMfc(m)
            await dev.connect()
            sup.mfcs[m.id] = dev
            self.mfcs[m.id] = dev

        for i in cfg.instruments:
            if not i.enabled:
                continue
            dev = FakeInstrument(i)
            await dev.connect()
            sup.instruments[i.id] = dev
            self.instruments[i.id] = dev

        sup._running = True     # so abort()/etc behave; background loops NOT started
        self.sup = sup
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self.sup is not None:
            with contextlib.suppress(Exception):
                await self.sup.recipes.abort()
            with contextlib.suppress(Exception):
                await self.sup.stop_fill_regulation()
            with contextlib.suppress(Exception):
                await self.sup.stop_prestart()
            self.sup.logger.close()
        if self._orig_valve_state_path is not None:
            supervisor_module.VALVE_STATE_PATH = self._orig_valve_state_path
            supervisor_module.LABELS_PATH = self._orig_labels_path
        if self._tmpdir is not None:
            self._tmpdir.cleanup()

    async def tick(self) -> None:
        """Advance telemetry by exactly one sample: one slow-loop DAQ read
        (`_cycle`) and one fast-loop current/MFC read + publish
        (`_current_cycle`) - the same two methods the real background loops
        call every period, just invoked on your schedule instead of a timer's.
        This is what drives self.sup.history, the run-export CSV, and the
        manual data logger, so call it in a loop alongside whatever you're
        actually testing (a recipe run, pre-start, ...) rather than only at
        the end - a test that never ticks never produces a sample."""
        await self.sup._cycle()
        await self.sup._current_cycle()
