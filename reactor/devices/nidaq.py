"""NI-DAQmx backend.

Notes that matter for correctness:

* **DAQmx allows only one analog-input task per module.** So all AI channels on
  a given module are grouped into a single task and read in one call. Creating a
  task per channel works on the bench and then fails on real hardware with
  "resource reserved", so it is not done here.

* **Output tasks are created lazily, on the first write.** Creating an output
  task drives that output to its idle state, so it is deferred until something is
  actually commanded.

* **There is no analog-output path, deliberately.** The only AO hardware here is
  an NI 9265 (4 x 0-20 mA CURRENT output) and nothing is known to be wired to it
  (`reactor-5u2`). An earlier unreachable AO path was removed because it called
  `add_ao_voltage_chan`, which is simply wrong for a current module - a
  plausible-looking trap for whoever eventually identifies the 9265. When that
  day comes, write it fresh against `add_ao_current_chan`. See docs/HARDWARE.md.

* Reads are wrapped so a flaky channel yields `ok=False` instead of taking down
  the control loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from .base import Reading

AiKind = Literal["voltage", "thermocouple", "rtd"]


# --------------------------------------------------------------------------- #
#  Channel plan
# --------------------------------------------------------------------------- #


@dataclass
class AiSpec:
    key: str                       # reading key, e.g. "pressure" / "stage.temp"
    channel: str                   # "cDAQ1Mod1/ai0"
    kind: AiKind = "voltage"
    unit: str = "V"
    rng: tuple[float, float] = (-10.0, 10.0)
    terminal_config: str = "rse"
    tc_type: str = "K"
    rtd_type: str = "Pt100"
    rtd_wires: int = 4

    @property
    def device(self) -> str:
        return self.channel.split("/")[0]


@dataclass
class DoSpec:
    key: str
    line: str                      # "cDAQ2Mod2/port0/line0"
    invert: bool = False

    @property
    def device(self) -> str:
        return self.line.split("/")[0]


@dataclass
class DaqPlan:
    ai: list[AiSpec] = field(default_factory=list)
    do: list[DoSpec] = field(default_factory=list)


# --------------------------------------------------------------------------- #
#  Interface
# --------------------------------------------------------------------------- #


class NiDaqBackend:
    def __init__(self) -> None:
        self._plan = DaqPlan()
        self._ai_tasks: dict[str, tuple[object, list[AiSpec]]] = {}
        #: One single-line DO task PER VALVE, keyed by valve key. Each write then
        #: touches only its own line, so actuating one valve can never re-drive a
        #: sibling on the same module. (Digital output has no one-task-per-module
        #: restriction - that limit is analog-input only.)
        self._do_tasks: dict[str, object] = {}
        self._do_values: dict[str, bool] = {}
        #: one-off single-line DO tasks used only by valve identification, keyed
        #: by raw line name. Separate from the configured-valve _do_tasks.
        self._id_tasks: dict[str, object] = {}
        self._id_state: dict[str, bool] = {}
        self._lock = asyncio.Lock()
        self._nidaqmx = None
        #: why configure() failed, if it did - surfaced in the Connections table
        self.last_error = "not configured"

    @property
    def input_count(self) -> int:
        """Number of analog-input channels successfully brought up."""
        return sum(len(specs) for _, specs in self._ai_tasks.values())

    def _mod(self):
        if self._nidaqmx is None:
            import nidaqmx  # imported late so the app runs without the driver

            self._nidaqmx = nidaqmx
        return self._nidaqmx

    # -- setup -------------------------------------------------------------- #

    async def configure(self, plan: DaqPlan) -> None:
        """Create INPUT tasks only. Outputs are created on first write."""
        self._plan = plan
        try:
            await asyncio.to_thread(self._build_ai_tasks)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        self.last_error = ""

    def _build_ai_tasks(self) -> None:
        nidaqmx = self._mod()
        from nidaqmx.constants import (
            RTDType,
            TemperatureUnits,
            TerminalConfiguration,
            ThermocoupleType,
        )

        term_map = {
            "rse": TerminalConfiguration.RSE,
            "nrse": TerminalConfiguration.NRSE,
            "diff": TerminalConfiguration.DIFF,
            "pseudodiff": TerminalConfiguration.PSEUDO_DIFF,
        }

        # One task per (module, kind): DAQmx will not share a module across
        # simultaneous AI tasks, and mixing measurement types in one task is
        # not portable across module families.
        groups: dict[tuple[str, str], list[AiSpec]] = {}
        for spec in self._plan.ai:
            if spec.channel:
                groups.setdefault((spec.device, spec.kind), []).append(spec)

        for (device, kind), specs in groups.items():
            task = nidaqmx.Task(new_task_name=f"ai_{device}_{kind}")
            try:
                for s in specs:
                    if kind == "voltage":
                        task.ai_channels.add_ai_voltage_chan(
                            s.channel,
                            min_val=s.rng[0],
                            max_val=s.rng[1],
                            terminal_config=term_map.get(
                                s.terminal_config, TerminalConfiguration.RSE
                            ),
                        )
                    elif kind == "thermocouple":
                        task.ai_channels.add_ai_thrmcpl_chan(
                            s.channel,
                            units=TemperatureUnits.DEG_C,
                            thermocouple_type=getattr(
                                ThermocoupleType, s.tc_type.upper(), ThermocoupleType.K
                            ),
                        )
                    else:  # rtd
                        rtd = s.rtd_type.upper().replace("PT", "PT_")
                        task.ai_channels.add_ai_rtd_chan(
                            s.channel,
                            units=TemperatureUnits.DEG_C,
                            rtd_type=getattr(RTDType, rtd, RTDType.PT_3750),
                            resistance_config=_resistance_config(s.rtd_wires),
                            current_excit_val=1.0e-3,
                        )
                task.start()
                self._ai_tasks[f"{device}:{kind}"] = (task, specs)
            except Exception:
                task.close()
                raise

    # -- reads -------------------------------------------------------------- #

    async def read_ai(self) -> list[Reading]:
        return await asyncio.to_thread(self._read_ai_sync)

    def _read_ai_sync(self) -> list[Reading]:
        out: list[Reading] = []
        for task, specs in self._ai_tasks.values():
            try:
                raw = task.read()
                if not isinstance(raw, list):
                    raw = [raw]
                for spec, val in zip(specs, raw):
                    out.append(Reading(key=spec.key, value=float(val), unit=spec.unit))
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                for spec in specs:
                    out.append(
                        Reading(key=spec.key, value=None, unit=spec.unit,
                                ok=False, detail=detail)
                    )
        return out

    # -- writes ------------------------------------------------------------- #

    async def write_do(self, key: str, value: bool) -> None:
        spec = next((s for s in self._plan.do if s.key == key), None)
        if spec is None:
            raise KeyError(f"no digital output configured for '{key}'")
        async with self._lock:
            self._do_values[key] = bool(value)
            await asyncio.to_thread(self._flush_do, spec)

    def _flush_do(self, spec: DoSpec) -> None:
        task = self._ensure_do_task(spec)
        state = self._do_values.get(spec.key, False)
        task.write(not state if spec.invert else state, auto_start=True)

    def _ensure_do_task(self, spec: DoSpec):
        existing = self._do_tasks.get(spec.key)
        if existing:
            return existing
        nidaqmx = self._mod()
        task = nidaqmx.Task(new_task_name=f"do_{spec.key}")
        try:
            task.do_channels.add_do_chan(spec.line)
        except Exception:
            task.close()
            raise
        self._do_tasks[spec.key] = task
        return task

    # -- raw line pulsing (valve identification only) ----------------------- #

    async def id_write(self, line: str, state: bool) -> None:
        """Drive an arbitrary DO line, for identifying what it actuates."""
        async with self._lock:
            await asyncio.to_thread(self._id_write_sync, line, bool(state))

    def _id_write_sync(self, line: str, state: bool) -> None:
        task = self._id_tasks.get(line)
        if task is None:
            nidaqmx = self._mod()
            task = nidaqmx.Task(new_task_name=f"id_{line.replace('/', '_')}")
            task.do_channels.add_do_chan(line)
            self._id_tasks[line] = task
        task.write(state, auto_start=True)
        self._id_state[line] = state

    async def id_release(self, line: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._id_release_sync, line)

    def _id_release_sync(self, line: str) -> None:
        task = self._id_tasks.pop(line, None)
        self._id_state[line] = False
        if task is not None:
            try:
                task.write(False, auto_start=True)
            except Exception:
                pass
            try:
                task.close()
            except Exception:
                pass

    async def id_release_all(self) -> None:
        """Drive every identification line low and close its task. Emergency-safe."""
        async with self._lock:
            for line in list(self._id_tasks):
                self._id_release_sync(line)

    # -- teardown ----------------------------------------------------------- #

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        for task, _ in self._ai_tasks.values():   # (task, specs) tuples
            try:
                task.close()
            except Exception:
                pass
        self._ai_tasks.clear()
        for task in self._do_tasks.values():   # per-line DO tasks (task, not tuple)
            try:
                task.close()
            except Exception:
                pass
        self._do_tasks.clear()
        for line in list(self._id_tasks):
            self._id_release_sync(line)


def _resistance_config(wires: int):
    from nidaqmx.constants import ResistanceConfiguration

    return {
        2: ResistanceConfiguration.TWO_WIRE,
        3: ResistanceConfiguration.THREE_WIRE,
        4: ResistanceConfiguration.FOUR_WIRE,
    }.get(wires, ResistanceConfiguration.FOUR_WIRE)
