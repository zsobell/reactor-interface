"""A "no physics" virtual reactor: fake devices, real everything else.

The fakes replace exactly the five classes Supervisor.start() constructs -
NiDaqBackend, MksMfc, ScpiInstrument, GlassmanFL, Keithley2260B - and nothing
else. Supervisor itself, RecipeRunner, and every control-logic method
(set_valve, set_mfc_setpoint, start_fill_regulation, start_prestart,
supplies_output_on/off, the recipe engine's whole state machine) run completely
unmodified, exactly as they would against the real reactor. Only the boundary
where bytes would otherwise cross onto a wire - DAQmx, Modbus, VISA, the
supplies' serial ports - is replaced with an in-memory stand-in a test can pose
and inspect.

Keeping the supply fakes in step matters for a second reason: the real drivers
open a COM port at connect, so a harness that let Supervisor build the real
ones would have tests talking to the actual 1.5 kV plasma supply and switching
on the four DC supplies that bias the stage and drive the coils.

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


class FakeSupply(Device):
    """Stands in for GlassmanFL (the HV plasma supply).

    Set `.voltage` / `.current` / `.arc_count` directly to control what the
    next read() reports, `.ok = False` to simulate the supply powered off or
    its USB unplugged, and the status flags to exercise fault handling::

        vr.supplies["hv"].voltage = 850.0      # volts
        vr.supplies["hv"].hv_on = True
        vr.supplies["hv"].faults = ["interlock"]

    The program sends this supply exactly ONE command - `hv_off()`, at the end
    of a run or on abort - so that is the only write modelled here. Calls land
    in `.hv_off_calls` and clear `.hv_on`; the voltage and current programs are
    deliberately left alone, mirroring GlassmanFL.hv_off, so a test can catch a
    regression that zeroes the operator's front-panel levels. There is still no
    way to set a level or turn HV *on*, because there is none in the program.
    """

    key_prefix = "hv"

    def log_channels(self) -> dict[str, str]:
        return {"voltage": "voltage", "current": "current",
                "arcs": "arc_count"}

    def __init__(self, cfg) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self.firmware = "02"
        self.ok = True
        self.voltage: float | None = 0.0
        self.current: float | None = 0.0
        self.arc_count: int = 0
        self.hv_on = False
        self.remote = False
        self.voltage_mode = True
        self.current_trip_enabled = False
        self.faults: list[str] = []
        self.hv_off_calls = 0

    async def hv_off(self) -> None:
        """Mirror of GlassmanFL.hv_off: no-op while disconnected, and it clears
        HV without touching the voltage/current programs."""
        if not self.connected:
            return
        self.hv_off_calls += 1
        self.hv_on = False
        self.remote = True          # any Set command moves the supply to remote

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def read(self) -> list[Reading]:
        vkey = f"hv.{self.id}.voltage"
        ikey = f"hv.{self.id}.current"
        akey = f"hv.{self.id}.arc_count"
        if not self.ok or self.voltage is None or self.current is None:
            return [self._bad(vkey, self.cfg.unit_v, "virtual: not ok"),
                    self._bad(ikey, self.cfg.unit_i, "virtual: not ok"),
                    self._bad(akey, "", "virtual: not ok")]
        return [
            Reading(key=vkey, value=self.voltage, unit=self.cfg.unit_v),
            Reading(key=ikey, value=self.current, unit=self.cfg.unit_i),
            Reading(key=akey, value=float(self.arc_count), unit=""),
        ]

    def status(self) -> dict:
        return {
            **super().status(),
            "driver": self.cfg.driver,
            "model": self.cfg.model or "VIRTUAL",
            "port": f"virtual:{self.cfg.id}",
            "baud": self.cfg.baud,
            "address": self.cfg.address,
            "firmware": self.firmware,
            "full_scale_v": self.cfg.full_scale_v,
            "full_scale_i": self.cfg.full_scale_i,
            "unit_v": self.cfg.unit_v,
            "unit_i": self.cfg.unit_i,
            "read_only": True,
            "voltage": self.voltage,
            "current": self.current,
            "arc_count": self.arc_count,
            "hv_on": self.hv_on,
            "remote": self.remote,
            "voltage_mode": self.voltage_mode,
            "current_trip_enabled": self.current_trip_enabled,
            "faulted": bool(self.faults),
            "faults": list(self.faults),
        }


class FakeKeithley(Device):
    """Stands in for Keithley2260B (the four DC supplies).

    Set `.voltage` / `.current` to control what the next read() reports and
    `.ok = False` to simulate the supply powered off. Unlike the Glassman fake
    this one IS commanded: the program switches its output on at pre-start and
    off at run end, and sets the voltage of whichever unit is the sample bias.

        vr.supplies["steering"].output_on          # -> True after pre-start
        vr.supplies["stage_bias"].voltage_calls    # -> [12.0]
        vr.supplies["stage_bias"].polarity         # -> -1

    `output_calls` records every :OUTP transition so a test can catch a supply
    being cycled when it should have been left alone - the collimating coil
    must NOT follow the beam.
    """

    key_prefix = "psu"

    def log_channels(self) -> dict[str, str]:
        return {"voltage": "voltage", "current": "current"}

    def __init__(self, cfg) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self.model = cfg.model or "VIRTUAL-2260B"
        self.serial_number = cfg.usb_serial or "0"
        self.port = f"virtual:{cfg.id}"
        self.identity = f"Keithley Instruments Inc.,Model {self.model},{self.serial_number},virtual"
        self.max_voltage = 250.0
        self.max_current = 4.725
        self.ok = True
        self.voltage: float | None = 0.0
        self.current: float | None = 0.0
        self.output_on: bool | None = False
        self.polarity = 1
        self.questionable = 0
        #: Every set_output(...) value, in order. A test asserts on the SHAPE of
        #: the sequence, not just the final state.
        self.output_calls: list[bool] = []
        #: Every set_voltage(...) magnitude, in order.
        self.voltage_calls: list[float] = []
        #: Every set_current(...) magnitude, in order. Nothing sets a current
        #: automatically - only the operator's Hardware-tab field does - so a
        #: non-empty list after a run is a bug.
        self.current_calls: list[float] = []
        self.voltage_setpoint: float | None = 0.0
        self.current_setpoint: float | None = 0.0

    def mode_label(self) -> str | None:
        """Same derivation as Keithley2260B: whichever limit the output has
        reached. Set .voltage/.current against .voltage_setpoint/
        .current_setpoint to pose a CV or CC supply in a test."""
        if not self.output_on:
            return None
        v, i = self.voltage, self.current
        v_set, i_set = self.voltage_setpoint, self.current_setpoint
        if v is None or i is None or v_set is None or i_set is None:
            return None

        def reached(meas, setpoint):
            if setpoint is None or setpoint <= 1e-9:
                return None
            return (setpoint - abs(meas)) / setpoint

        dv, di = reached(v, v_set), reached(i, i_set)
        at_v = dv is not None and dv <= 0.01
        at_i = di is not None and di <= 0.01
        if at_v and at_i:
            return "CV" if dv <= di else "CC"
        if at_v:
            return "CV"
        if at_i:
            return "CC"
        return None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        """Closes the link only - deliberately does NOT switch the output off,
        mirroring Keithley2260B.disconnect."""
        self.connected = False

    async def set_output(self, on: bool) -> None:
        self.output_calls.append(bool(on))
        self.output_on = bool(on)

    async def set_voltage(self, volts: float) -> None:
        # Magnitude only, like the real driver: the supply is single-quadrant.
        self.voltage_calls.append(abs(float(volts)))
        self.voltage_setpoint = abs(float(volts))

    async def set_current(self, amps: float) -> None:
        self.current_calls.append(abs(float(amps)))
        self.current_setpoint = abs(float(amps))

    async def read(self) -> list[Reading]:
        vkey = f"psu.{self.id}.voltage"
        ikey = f"psu.{self.id}.current"
        if not self.ok or self.voltage is None or self.current is None:
            return [self._bad(vkey, "V", "virtual: not ok"),
                    self._bad(ikey, "A", "virtual: not ok")]
        volts = self.voltage * (-1 if self.polarity < 0 else 1)
        return [Reading(key=vkey, value=volts, unit="V"),
                Reading(key=ikey, value=self.current, unit="A")]

    def status(self) -> dict:
        return {
            **super().status(),
            "driver": self.cfg.driver,
            "kind": "keithley_2260b",
            "model": self.model,
            "port": self.port,
            "usb_serial": self.serial_number,
            "identity": self.identity,
            "max_voltage": self.max_voltage,
            "max_current": self.max_current,
            "voltage": self.voltage,
            "current": self.current,
            "voltage_setpoint": self.voltage_setpoint,
            "current_setpoint": self.current_setpoint,
            "output_on": self.output_on,
            "mode": self.mode_label(),
            "polarity": self.polarity,
            "is_sample_bias": self.cfg.sample_bias,
            "prestart_output": self.cfg.prestart_output,
            "questionable": self.questionable,
        }


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
        self.supplies: dict[str, FakeSupply] = {}

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

        for ps in cfg.power_supplies:
            if not ps.enabled:
                continue
            dev = (FakeSupply(ps) if ps.driver == "glassman_fl"
                   else FakeKeithley(ps))
            await dev.connect()
            sup.supplies[ps.id] = dev
            self.supplies[ps.id] = dev

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
        (`_cycle`), one MFC poll (`_mfc_cycle`) and one fast-loop current read
        + publish (`_current_cycle`) - the same three methods the real
        background loops call every period, just invoked on your schedule
        instead of a timer's. This is what drives self.sup.history, the
        run-export CSV, and the manual data logger, so call it in a loop
        alongside whatever you're actually testing (a recipe run, pre-start,
        ...) rather than only at the end - a test that never ticks never
        produces a sample.

        On real hardware these three run at different rates (site.loop_hz,
        site.mfc_hz, site.current_hz) precisely because an MFC read is slow;
        here they advance together, so every channel is fresh on every tick.
        A test that needs the real staggering - e.g. checking that a channel
        which wasn't resampled is logged blank - must call the individual
        methods itself rather than tick()."""
        await self.sup._cycle()
        await self.sup._mfc_cycle()
        await self.sup._current_cycle()
