"""The Supervisor: single owner of reactor state and the only path to hardware.

The original program kept system state in LabVIEW functional global variables,
which any subVI could modify from anywhere. Here there is exactly one Supervisor.
It owns the devices, the recipe runner and the logger. Application hardware commands are methods on this class. Only the explicitly
requested interlocks, sequences and cleanup in docs/CONTROL_MODEL.md are applied.
Pre-start, telemetry and recording are delegated to focused collaborators.

Scope: UHV chamber, gas dosing, pressure + stage-temperature measurement.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from itertools import islice
from pathlib import Path
from typing import Any

from .config import ReactorConfig
from .control.recipe import Recipe, RecipeRunner
from .control.prestart import PrestartController
from .control.prestart_store import PrestartRecipeStore
from .control.fill import FillController
from .control.sweep import SweepController
from .control.run_coordinator import RunCoordinator, RunSession
from .control.clock import Clock
from . import datalog
from .datalog import DataLogger
from .recording import RecordingService
from .telemetry import Telemetry
from .devices.base import Device, Reading
from .devices.ellipsometer import EllipsometerClient, EllipsometerPoint
from .devices.glassman_fl import GlassmanFL
from .devices.keithley_2260b import Keithley2260B
from .devices.instrument import ScpiInstrument
from .dependencies import DeviceFactory, StatePaths
from .devices.nidaq import AiSpec, DaqPlan, DoSpec, NiDaqBackend

log = logging.getLogger("reactor.supervisor")

HISTORY_SAMPLES = 18000       # at 5 Hz that is one hour of trend
RECONNECT_EVERY_S = 5.0       # retry a dropped instrument this often

#: Operator-edited display names (rename a valve/MFC/gauge when swapping
#: chemicals) live here, not in reactor.yaml, so a rename is a UI action rather
#: than a hand edit of the hardware map. Keyed as {"valve"|"mfc"|"gauge": {id: label}}.
def _tail(dq: deque, n: int) -> list:
    """The last `n` entries of a deque, without copying the whole thing.

    `list(dq)[-n:]` copies every element first, and these buffers hold 200 000.
    At the 5 Hz telemetry rate that is a million pointer copies a second to
    ship two hundred rows. Walking backwards from the right end is O(n).
    """
    return list(islice(reversed(dq), n))[::-1]


LABELS_PATH = Path(__file__).resolve().parent.parent / "config" / "labels.json"

#: Last-commanded valve state, persisted across restarts. There is no valve-
#: position feedback wired to this DAQ (see docs/IDENTIFYING_HARDWARE.md), so
#: this is a best-effort record of what the program last told each line to do,
#: not a hardware-confirmed reading. It is written on every operator/recipe
#: valve command and reloaded at startup so the UI does not lie by defaulting
#: every valve to "closed" when a restart happens with valves physically open.
#: Restoring it on startup only updates this program's internal model - it
#: never writes to hardware, so a restart still commands nothing (see start()).
VALVE_STATE_PATH = Path(__file__).resolve().parent.parent / "config" / "valve_state.json"


def _holds(dev) -> str:
    """What `dev` is occupying, for the shutdown receipt: "grid_bias (COM11)".

    The operator's question at shutdown is never "did the object disconnect" -
    it is "is COM11 free, can I start the server again". So the receipt names
    the port, and falls back to the device id when there is nothing better
    (an MFC holds a TCP socket, which is released by the same disconnect but is
    not the thing that blocks a restart).
    """
    cfg = getattr(dev, "cfg", None)
    where = (getattr(dev, "port", "")                       # resolved at connect
             or getattr(cfg, "port", "")                    # configured
             or getattr(cfg, "resource", "")                # VISA
             or getattr(cfg, "host", ""))                   # Modbus TCP
    where = str(where or "").strip()
    # MfcCfg.port is the Modbus TCP port (502), not a COM port - an integer
    # there means "no serial port", not "COM502".
    if where.isdigit():
        where = str(getattr(cfg, "host", "") or "")
    return f"{dev.id} ({where})" if where else str(dev.id)

#: Name of the last run actually STARTED (not merely typed into the box), so the
#: UI can pre-fill the next one incremented - "Mo-014" -> "Mo-015". Written when
#: a run starts, which is what makes the sequence reflect real runs: abandoning a
#: pre-filled name without starting leaves the counter where it was.
RUN_NAME_PATH = Path(__file__).resolve().parent.parent / "config" / "last_run.json"

#: Grace after a setpoint is commanded before its measurement is judged against
#: it. A device on its way to a new value is not a mismatch, and without this
#: every gas window would flash a warning as the MFC ramped. A DISPLAY debounce
#: only - nothing about what is commanded, or when, depends on it.
SETPOINT_SETTLE_S = 5.0

#: Digital-output lines available for valve identification, grouped by module.
#: Verified present on this hardware. Note a 9375's port0 is INPUT; outputs are
#: port1.
DO_LINE_GROUPS: dict[str, list[str]] = {
    "cDAQ2Mod2 (NI 9472, 8)": [f"cDAQ2Mod2/port0/line{i}" for i in range(8)],
    "cDAQ2Mod3 (NI 9472, 8)": [f"cDAQ2Mod3/port0/line{i}" for i in range(8)],
    "cDAQ1Mod3 (NI 9375, 16)": [f"cDAQ1Mod3/port1/line{i}" for i in range(16)],
}


class Supervisor:
    def __init__(self, cfg: ReactorConfig, *, devices: DeviceFactory | None = None,
                 paths: StatePaths | None = None, clock: Clock | None = None) -> None:
        self.clock = clock or Clock()
        self.cfg = cfg
        self.devices = devices or DeviceFactory()
        self.paths = paths or StatePaths(LABELS_PATH, VALVE_STATE_PATH, RUN_NAME_PATH)
        self.recipes = RecipeRunner(self, clock=self.clock)
        self.logger = DataLogger(cfg)
        self.recording = RecordingService(
            self.logger, lambda message: self._event("error", message, record=False),
            on_capture=lambda name: self._event("ellipsometer", f"acquisition start -> {name}"))
        self.runs = RunCoordinator(self, self.recipes, self.recording, clock=self.clock)
        self.prestart_recipes = PrestartRecipeStore(self.paths.prestart_recipes, cfg)

        self.snapshot: dict[str, Any] = {}
        self.readings: dict[str, Reading] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SAMPLES)
        #: A long scrollback, because 250 was nowhere near enough: one run emits
        #: roughly ten events a cycle, so a 150-cycle run pushed the whole
        #: pre-start out of the buffer before anyone could read it. This is only
        #: the in-memory copy the UI scrolls; every event is ALSO written to
        #: server.log by _event(), which rotates and is the permanent record.
        #: Everything that happened, oldest dropped first. 200k is far more than
        #: any run produces (a 150-cycle run logs a few hundred), and the run's
        #: own copy on disk is unbounded - see DataLogger.write_event.
        self.events: deque[dict[str, Any]] = deque(maxlen=200000)
        #: Just the bad news, kept separately so it does not have to be found by
        #: scrolling the event log (Zach, 2026-09-09). Same entries, same
        #: objects - a subset, not a second source of truth.
        self.errors: deque[dict[str, Any]] = deque(maxlen=200000)

        self.daq: NiDaqBackend | None = None
        self.mfcs: dict[str, Device] = {}
        self.instruments: dict[str, Device] = {}
        #: Supplies are polled on the slow loop. Glassman has application HV-off
        #: control only; Keithleys also have manual controls and the requested
        #: pre-start/run output lifecycle (see CONTROL_MODEL.md).
        self.supplies: dict[str, Device] = {}
        # Best-effort restore of last-commanded state (see VALVE_STATE_PATH) -
        # falls back to False for any valve it has no record of.
        _persisted_valves = self._load_valve_state()
        self.valve_state: dict[str, bool] = {
            v.id: _persisted_valves.get(v.id, False) for v in cfg.valves
        }

        # Soft-open pulse train for valves flagged `soft_open` in the config
        # (the Ar pneumatic). Operator settings, so they are owned by the UI and
        # arrive from config/run_params.json via set_soft_open_params - they are
        # NOT run parameters, because a manual open from the Hardware tab
        # carries none. These are the fallbacks until the UI has been saved once.
        # One bleed pulse, then full open. It was five until 2026-08-26, when
        # Zach found the pneumatic does not actuate fast enough for short
        # pulses to blunt the inrush - five of them just made five inrushes,
        # and the chamber gauge tripped off anyway.
        #: Per-device reconnect state, id -> {attempts, last, error}. The
        #: retry loop below rewrites a device's last_error on every attempt,
        #: and the text varies between attempts (a port-not-found becomes a
        #: timeout becomes an access-denied), so the Connections table had a
        #: cell whose width changed every few seconds and nothing that said
        #: whether the program was still trying. Reported 2026-08-26.
        self.reconnect: dict[str, dict[str, Any]] = {}

        self.soft_open: dict[str, float] = {
            "pulses": 1, "on_s": 0.05, "gap_s": 0.5,
        }

        # Setpoint-vs-measurement monitoring (operator, 2026-09-01, after the
        # N2 line on Mo-017 sat at a setpoint it never reached and nothing said
        # so). The rule is the one the precursor fill pressure already uses -
        # commanded value vs measured value, flagged past the same tolerance,
        # cleared the moment it comes back. It WARNS and never acts: no
        # setpoint is refused, clamped or changed by any of this.
        #: Fraction off setpoint that counts as a mismatch. The Run tab's "Fill
        #: flag tolerance (%)", shared so there is one number for all of it.
        self.flag_tolerance: float = 0.20
        #: id -> monotonic time its setpoint was last commanded, so a device on
        #: its way to a new value is not flagged for being on the way. Display
        #: debounce only; nothing about the hardware depends on it.
        self._setpoint_changed: dict[str, float] = {}

        self._plan = DaqPlan()
        self._loop_task: asyncio.Task | None = None
        self._current_task: asyncio.Task | None = None
        self._mfc_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._last_cycle = 0.0
        #: Monotonic counter of device reads, and the value it had when each
        #: snapshot key was last actually MEASURED. The snapshot itself always
        #: carries the latest known value (the live UI needs that), but the run
        #: export uses these to write a channel only on the rows where it was
        #: really read, leaving the cell empty otherwise - so the file records
        #: measurements, not carried-forward copies of them. See
        #: DataLogger.write_run_sample's `blank`.
        #:
        #: A counter rather than a wall-clock stamp: equal timestamps or clock
        #: adjustments must not make a newly acquired reading appear stale.
        self._read_seq = 0
        self.snapshot_seq: dict[str, int] = {}
        #: _read_seq as of the last run-export row: the cutoff for "fresh since"
        self._last_row_seq = 0

        #: operator label overrides, {kind: {id: label}}; persisted to LABELS_PATH
        self.label_overrides: dict[str, dict[str, str]] = self._load_labels()
        self._cycle_count = 0
        self._running = False
        self.telemetry = Telemetry(self, DO_LINE_GROUPS)

        # Shutdown receipt. `stop()` records what it released so the Shut down
        # button can SHOW it - Zach, 2026-09-10: "I need some confirmation
        # things are shut down and ready to be booted again". Also makes stop()
        # idempotent: the button awaits it inside the request handler, and the
        # lifespan then awaits it again on the way out.
        self._stop_steps: list[dict] = []
        self._stop_receipt: dict | None = None
        self._stop_task = None
        self._pending_teardowns = set()

        # Valve flip markers (for the current-trace overlay). Every set_valve is
        # recorded with its reason so the UI can mark scheduled vs reignite flips.
        self.marks: deque[dict[str, Any]] = deque(maxlen=3000)
        # Valve identification sweep
        self._sweep = SweepController(self)

        self._prestart = PrestartController(
            self, store=self.prestart_recipes, clock=self.clock)

        # Background fill-pressure regulation
        self._fill = FillController(self)

        # In-situ ellipsometer (FS-1) live stream: a read-only subscriber that
        # timestamps each streamed measurement with the reactor clock into a
        # per-acquisition sidecar, so a refit file downloaded afterwards can be
        # put back onto the reactor clock (reactor/analysis/ellipsometer_merge).
        # Created here, started in start(); None when disabled in config.
        self.ellipsometer = self.devices.ellipsometer(
            cfg.ellipsometer, on_point=self._on_ellipsometer_point,
            on_state=self._on_ellipsometer_state)

    # ====================================================================== #
    #  Startup / shutdown
    # ====================================================================== #

    def _build_plan(self) -> DaqPlan:
        cfg = self.cfg
        plan = DaqPlan()

        plan.ai.append(
            AiSpec(
                key="pressure",
                channel=cfg.pressure.channel,
                kind="voltage",
                unit="V",
                rng=tuple(cfg.pressure.input_range_v),
                terminal_config=cfg.pressure.terminal_config,
            )
        )

        for g in cfg.gauges:
            if g.channel:
                plan.ai.append(
                    AiSpec(
                        key=f"gauge.{g.id}",
                        channel=g.channel,
                        kind="voltage",
                        unit="V",
                        rng=tuple(g.input_range_v),
                        terminal_config=g.terminal_config,
                    )
                )

        if cfg.stage_temp.enabled and cfg.stage_temp.channel:
            plan.ai.append(
                AiSpec(
                    key="stage.temp",
                    channel=cfg.stage_temp.channel,
                    kind="thermocouple",
                    unit="C",
                    tc_type=cfg.stage_temp.tc_type,
                )
            )

        for aux in cfg.aux_inputs:
            plan.ai.append(
                AiSpec(
                    key=f"aux.{aux.id}",
                    channel=aux.channel,
                    kind=aux.kind,
                    unit="V" if aux.kind == "voltage" else "C",
                    rng=tuple(aux.input_range_v),
                    terminal_config=aux.terminal_config,
                    tc_type=aux.tc_type,
                )
            )

        for v in cfg.valves:
            # A valve with no line assigned has no hardware behind it, so it is
            # not put in the DAQ plan.
            if v.line:
                plan.do.append(DoSpec(key=v.id, line=v.line, invert=v.invert))

        return plan

    async def start(self, *, background_tasks: bool = True) -> None:
        """Connect to everything and begin polling. Commands nothing.

        A device that fails to connect is recorded as failed and the rest of the
        system carries on - so a missing cDAQ or an unplugged DMM gives you a
        readable interface showing what is wrong, not a crash.
        """
        cfg = self.cfg
        self._plan = self._build_plan()

        self.daq = self.devices.daq()
        try:
            await self.daq.configure(self._plan)
            self._event("startup",
                        f"DAQ configured: {len(self._plan.ai)} analog inputs, "
                        f"{len(self._plan.do)} digital outputs")
        except Exception as exc:
            self._event("error", f"DAQ configure failed: {type(exc).__name__}: {exc}")

        for m in cfg.mfcs:
            dev = self.devices.mfc(m)
            self.mfcs[m.id] = dev
            try:
                await dev.connect()
                self._event("startup", f"MFC {m.id} connected ({m.host}:{m.port})")
            except Exception as exc:
                dev.last_error = f"{type(exc).__name__}: {exc}"
                self._event("error", f"MFC {m.id}: {dev.last_error}")

        for i in cfg.instruments:
            if not i.enabled:
                continue
            inst = self.devices.instrument(i)
            self.instruments[i.id] = inst
            try:
                await inst.connect()
                self._event("startup", f"instrument {i.id}: {inst.identity}")
            except Exception as exc:
                inst.last_error = f"{type(exc).__name__}: {exc}"
                self._event("error", f"instrument {i.id}: {inst.last_error}")

        for ps in cfg.power_supplies:
            if not ps.enabled:
                continue
            # The config's Literal already restricts `driver`, so an unknown
            # value cannot reach here.
            dev = self.devices.supply(ps)
            self.supplies[ps.id] = dev
            try:
                await dev.connect()
            except Exception as exc:
                dev.last_error = f"{type(exc).__name__}: {exc}"
                self._event("error", f"power supply {ps.id}: {dev.last_error}")
                continue
            if ps.driver == "glassman_fl":
                self._event("startup",
                            f"power supply {ps.id}: {ps.model or ps.driver} on "
                            f"{ps.port} @{ps.baud} addr {ps.address}, "
                            f"firmware {dev.firmware or '?'} "
                            f"(monitor only; HV off at run end)")
            else:
                # The port is REPORTED, not configured - it is resolved from the
                # USB serial - so this line is the record of where it landed.
                rated = (f", max {dev.max_voltage:g} V / {dev.max_current:g} A"
                         if dev.max_voltage is not None
                         and dev.max_current is not None else "")
                role = "  [SAMPLE BIAS]" if ps.sample_bias else ""
                self._event("startup",
                            f"power supply {ps.id}: {dev.model or ps.model} "
                            f"serial {dev.serial_number} on {dev.port}"
                            f"{rated}{role}")

        self._running = True
        if background_tasks:
            self._loop_task = asyncio.create_task(self._control_loop(), name="control-loop")
            self._current_task = asyncio.create_task(
                self._current_loop(), name="current-loop")
            self._mfc_task = asyncio.create_task(self._mfc_loop(), name="mfc-loop")
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(), name="instrument-reconnect")

        if self.ellipsometer is not None:
            self.ellipsometer.start()
            self._event("startup",
                        f"ellipsometer subscriber -> {self.cfg.ellipsometer.host}:"
                        f"{self.cfg.ellipsometer.port} (read-only)")

    async def stop(self) -> dict:
        """Concurrent callers share one teardown and its truthful receipt."""
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(), name="reactor-stop")
        return await asyncio.shield(self._stop_task)

    async def _stop(self) -> dict:
        """Abort active work, stop polling and disconnect with a bounded receipt.

        Returns a RECEIPT: every teardown step with whether it succeeded, the
        devices whose ports were released, and how long it took. The Shut down
        button awaits this and shows it, because "port 8000 stopped answering"
        is not evidence that the DAQ and COM8-COM12 were let go - uvicorn
        releases the listening socket BEFORE the lifespan teardown runs, so the
        page used to report success on the one thing that was never in doubt.

        Idempotent. The shutdown endpoint calls it directly so it can report
        the receipt over HTTP while there is still an HTTP connection to report
        it on; the lifespan then calls it again and gets the same receipt back
        without touching a device twice.

        Previously documented here as "closing DAQmx output tasks resets those
        lines low" - CONTRADICTED by observation 2026-08: the Ar pneumatic
        isolation valve stayed physically open across a server restart, so at
        least that line (cDAQ2Mod2, NI 9472) does not reset on task close. This
        program does not reset every line. Run/pre-start cleanup commands its
        specified outputs; behavior of other lines on task close belongs to DAQ hardware,
        and per the above it should not be assumed to be "goes low". That is why
        valve state is now persisted (VALVE_STATE_PATH) and restored at startup
        instead of defaulting every valve to closed.
        """
        if self._stop_receipt is not None:
            return self._stop_receipt

        t0 = self.clock.elapsed()
        self._stop_steps = []
        self._running = False
        await self._teardown("abort run", self.abort_recipe(), timeout=12.0)

        # Pre-start gets the full ABORT, not just a stop. Operator decision,
        # 2026-08-25 (reactor-4h9), and the reasoning is his: "any server
        # shutdown should abort the run or prestart. Safety over data
        # collection."
        #
        # This covers a pre-start still RUNNING and one that has COMPLETED but
        # not yet handed over to a run. The completed case matters just as much:
        # a successful pre-start deliberately leaves the tool primed - Ar
        # flowing, fill valve pulsing, beam relay set, HV up, DC supplies on -
        # and a shutdown would otherwise walk away from all of it with nothing
        # left running to manage it.
        #
        # abort_prestart is the existing one-click undo (Ar off, fill off, relay
        # at rest, HV off, DC supply outputs off), so shutdown reuses it rather
        # than growing a second teardown that could drift out of step.
        #
        # Deliberately BEFORE the loops are cancelled and the devices are
        # disconnected below, or none of these commands could reach hardware.
        if self.prestart.get("cleanup_available"):
            self._event("recipe",
                        "server stopping: aborting pre-start "
                        f"({'in progress' if self.prestart.get('running') else 'primed'})")
            await self._teardown("abort pre-start", self.abort_prestart(),
                                 timeout=8.0)

        await self._teardown("stop fill regulation", self.stop_fill_regulation())
        if self.daq is not None:
            await self._teardown("stop valve identification", self._sweep.shutdown())

        # Cancel-and-wait, but never wait forever. `_cycle` reads the DAQ
        # through asyncio.to_thread, and a to_thread future CANNOT be
        # cancelled once its thread has started: `await task` after
        # `task.cancel()` blocks until DAQmx returns, which on a wedged module
        # is not bounded by anything this program controls. asyncio.wait is
        # used rather than awaiting the task, because awaiting a cancelled task
        # re-raises CancelledError and would abort the rest of this teardown.
        for name, task in (("control loop", self._loop_task),
                           ("current loop", self._current_task),
                           ("MFC loop", self._mfc_task),
                           ("reconnect loop", self._reconnect_task)):
            if task is None:
                continue
            log.info("shutdown: stopping %s", name)
            task.cancel()
            _done, pending = await asyncio.wait({task}, timeout=3.0)
            if pending:
                log.warning("shutdown: %s did not stop in 3s - leaving it "
                            "(its thread will die with the process)", name)
                self._stop_steps.append({
                    "what": f"stop {name}", "ok": False,
                    "note": "did not stop in 3s"})
            else:
                self._stop_steps.append({"what": f"stop {name}", "ok": True,
                                         "note": ""})

        if self.ellipsometer is not None:
            await self._teardown("stop ellipsometer subscriber",
                                 self.ellipsometer.stop())

        # Power supplies are included here purely to close their serial ports.
        # GlassmanFL.disconnect() deliberately commands nothing - it does NOT
        # send HV OFF - so stopping the server cannot switch off a plasma Zach
        # set by hand at the front panel. See reactor/devices/glassman_fl.py.
        #
        # Each disconnect gets its own deadline: one unresponsive serial port
        # must not keep the others - or the process - open. This is what left a
        # zombie holding the DAQ and COM8-COM12 after the Shut down button was
        # pressed (reported 2026-08-27); the button appeared to do nothing and
        # the next server came up unable to reach any device.
        for dev in (list(self.mfcs.values()) + list(self.instruments.values())
                    + list(self.supplies.values())):
            # Read what it is holding BEFORE disconnecting - a driver is free
            # to clear its own port attribute on the way down, and the receipt
            # exists to name the COM port that has been let go.
            await self._teardown(f"disconnect {dev.id}", dev.disconnect(),
                                 timeout=3.0, holds=_holds(dev))
        if self.daq:
            await self._teardown("close DAQ tasks", self.daq.close(),
                                 timeout=5.0, holds="DAQ tasks")

        await self._teardown("close recording worker", self.recording.close(), timeout=5.0)
        if self.recording.status().get("errors"):
            self._stop_steps.append({"what": "recording integrity", "ok": False,
                                     "note": str(self.recording.status()["errors"])})
        log.info("shutdown: teardown complete")

        released = [s["holds"] for s in self._stop_steps if s["ok"] and s.get("holds")]
        failed = [s for s in self._stop_steps if not s["ok"]]
        self._stop_receipt = {
            "steps": self._stop_steps,
            "released": released,
            "failed": [{"what": s["what"], "note": s["note"]} for s in failed],
            "ok": not failed,
            "elapsed_s": round(self.clock.elapsed() - t0, 2),
        }
        return self._stop_receipt

    async def _teardown(self, what: str, coro, timeout: float = 4.0,
                        holds: str = "") -> None:
        """One teardown step, with a deadline and a line in the log saying which.

        `holds` names the resource this step lets go of ("COM9", "DAQ tasks").
        It is what the Shut down button shows the operator, so only a step that
        actually succeeded contributes one.

        Teardown talks to real hardware over serial, USB and TCP, and any of
        those calls can block for as long as the driver feels like. Before
        2026-08-27 every step here was an unbounded await, so one wedged device
        stopped the whole shutdown - and the process stayed up holding the DAQ
        and the COM ports. Now each step is bounded and NAMED, so a hang is
        both survivable and diagnosable from server.log.
        """
        log.info("shutdown: %s", what)
        step = {"what": what, "ok": True, "note": "", "holds": holds}
        self._stop_steps.append(step)
        try:
            task = asyncio.ensure_future(coro)
            self._pending_teardowns.add(task)
            def collected(done):
                self._pending_teardowns.discard(done)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(collected)
            done, pending = await asyncio.wait({task}, timeout=timeout)
            if pending:
                task.cancel()
                raise asyncio.TimeoutError
            await task
        except asyncio.TimeoutError:
            log.warning("shutdown: %s did not finish in %.0fs - moving on",
                        what, timeout)
            step["ok"] = False
            step["note"] = f"did not finish in {timeout:.0f}s"
        except Exception as exc:
            log.warning("shutdown: %s failed: %s: %s",
                        what, type(exc).__name__, exc)
            step["ok"] = False
            step["note"] = f"{type(exc).__name__}: {exc}"

    # ====================================================================== #
    #  Control loop
    # ====================================================================== #

    async def _control_loop(self) -> None:
        period = 1.0 / self.cfg.site.loop_hz
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while self._running:
            next_at += period
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("control cycle failed")
                self._event("error", f"control cycle: {type(exc).__name__}: {exc}")
            delay = next_at - loop.time()
            if delay < -period:          # fell badly behind; resynchronise
                next_at = loop.time()
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    async def _reconnect_loop(self) -> None:
        """Retry any instrument that has dropped (e.g. the DMM was switched off).

        Connecting is read-only, so this reopens a session and re-sends the
        configured setup, nothing more. It never actuates anything. Bench
        instruments and power supplies are retried here; MFCs are left alone
        deliberately, since an MFC zeroes its setpoint when its Modbus master
        reconnects.

        The power supplies are safe to retry for the same reason: GlassmanFL's
        connect() only asks for the firmware revision, and its COM port can
        change out from under us anyway (a driver reinstall already moved it
        once), so a supply that comes back deserves to be picked up.
        """
        while self._running:
            await asyncio.sleep(RECONNECT_EVERY_S)
            for dev_id, dev in list(self.instruments.items()):
                if dev.connected or not self._running:
                    self.reconnect.pop(dev_id, None)
                    continue
                try:
                    await dev.connect()
                    self.reconnect.pop(dev_id, None)
                    self._event("startup",
                                f"instrument {dev_id} reconnected: {dev.identity}")
                except Exception as exc:
                    # Keep the reason visible in the tile, but don't flood the
                    # event log with one line per retry while it stays off.
                    dev.last_error = f"{type(exc).__name__}: {exc}"
                    self._note_retry(dev_id, dev.last_error)

            for dev_id, dev in list(self.supplies.items()):
                if dev.connected or not self._running:
                    self.reconnect.pop(dev_id, None)
                    continue
                try:
                    await dev.connect()
                    self.reconnect.pop(dev_id, None)
                    self._event("startup",
                                f"power supply {dev_id} reconnected: "
                                f"firmware {getattr(dev, 'firmware', '') or '?'}")
                except Exception as exc:
                    dev.last_error = f"{type(exc).__name__}: {exc}"
                    self._note_retry(dev_id, dev.last_error)

    def _note_retry(self, dev_id: str, error: str) -> None:
        """Record one failed reconnect attempt for `dev_id`.

        The count and the timestamp are the stable part - they say the program
        is still working on it and how long it has been - and they are what the
        Connections table leads with. The error text is carried along but is
        the volatile half.
        """
        st = self.reconnect.setdefault(
            dev_id, {"attempts": 0, "first": self.clock.wall(), "last": 0.0, "error": ""})
        st["attempts"] += 1
        st["last"] = self.clock.wall()
        st["error"] = error

    async def _cycle(self) -> None:
        """Slow loop: DAQ analog inputs and the power supplies, at site.loop_hz
        (the NI 9211 thermocouples cannot be read much faster). Updates the
        shared snapshot in place and writes the data log. MFCs and the
        sample-current instrument are independent Modbus/VISA devices with no such
        limit, so they are polled on the separate current and MFC loops below.

        The power supplies ride this loop rather than getting a timer of their
        own: site.loop_hz (2 Hz) is the slowest cadence in the program, and one
        Glassman query round-trips in ~13 ms - under 3% of the 500 ms budget -
        so it costs the thermocouples nothing. Their columns therefore appear on
        roughly every other run-export row, exactly as pressure and the
        thermocouples already do (see write_run_sample's `blank`). If they are
        ever wanted on every row, move this block into _current_cycle; do not
        add a fourth timer."""
        cfg = self.cfg
        readings: list[Reading] = []

        if self.daq is not None:
            readings.extend(await self.daq.read_ai())

        if self.supplies:
            for res in await asyncio.gather(
                    *(d.read() for d in self.supplies.values()),
                    return_exceptions=True):
                if isinstance(res, list):
                    readings.extend(res)
                elif isinstance(res, BaseException):
                    # read() is contracted not to raise, so this is a bug rather
                    # than a dead supply - but never let it kill the loop.
                    self._event("error",
                                f"power supply read: {type(res).__name__}: {res}")

        snap: dict[str, Any] = {}
        for r in readings:
            self.readings[r.key] = r
            snap[r.key] = r.value if r.ok else None

        # Engineering scaling for raw-voltage channels.
        volts = snap.get("pressure")
        if isinstance(volts, (int, float)):
            snap["pressure.volts"] = volts
            snap["pressure"] = cfg.pressure.scaling.apply(volts)

        for g in cfg.gauges:
            key = f"gauge.{g.id}"
            raw = snap.get(key)
            if isinstance(raw, (int, float)):
                snap[f"{key}.volts"] = raw
                snap[key] = g.scaling.apply(raw)

        for aux in cfg.aux_inputs:
            key = f"aux.{aux.id}"
            raw = snap.get(key)
            if isinstance(raw, (int, float)) and aux.kind == "voltage":
                snap[f"{key}.volts"] = raw
                snap[key] = aux.scaling.apply(raw)

        # Merge in place so the sample-current keys written by _current_loop are
        # not wiped each slow cycle.
        self.snapshot.update(snap)
        self._read_seq += 1
        for k in snap:
            self.snapshot_seq[k] = self._read_seq
        self.recording.submit_manual_sample(
            self.snapshot, self.recipes.progress, sampled_at=self.clock.wall()
        )

    async def _current_loop(self) -> None:
        """Fast loop: poll the bench instruments (the DMM6500 sample-current) at
        site.current_hz, publish telemetry, and write the run-export row. This is
        the plasma diagnostic, so it runs finer than the thermocouple-limited
        slow loop and drives the chart's current resolution and the electron-beam
        reignite cadence. The MFCs used to be read here too and held it to
        ~2.1 Hz; they now have their own loop (see _mfc_loop)."""
        period = 1.0 / self.cfg.site.current_hz
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while self._running:
            next_at += period
            try:
                await self._current_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("current cycle failed")
                self._event("error", f"current cycle: {type(exc).__name__}: {exc}")
            delay = next_at - loop.time()
            if delay < -period:
                next_at = loop.time()
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    def _absorb(self, results: list) -> None:
        """Fold device read results into the snapshot, stamping what was read.

        The stamp is what lets the run export tell a fresh measurement from a
        value that has merely been sitting in the snapshot since the last poll.
        """
        self._read_seq += 1
        for r in results:
            if isinstance(r, list):
                for rd in r:
                    self.readings[rd.key] = rd
                    self.snapshot[rd.key] = rd.value if rd.ok else None
                    self.snapshot_seq[rd.key] = self._read_seq
            elif isinstance(r, BaseException):
                self._event("error", f"device read: {type(r).__name__}: {r}")

    async def _mfc_loop(self) -> None:
        """Poll the MFCs on their own cadence (site.mfc_hz).

        Deliberately NOT part of _current_cycle: an MFC HTTP read takes
        0.45-0.9 s on this hardware, so gathering them there throttled the whole
        telemetry/logging tick to ~2.1 Hz no matter what current_hz said. Flow
        readings are for display and the log; no control path waits on them.
        """
        period = 1.0 / self.cfg.site.mfc_hz
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while self._running:
            next_at += period
            try:
                await self._mfc_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("mfc cycle failed")
                self._event("error", f"mfc cycle: {type(exc).__name__}: {exc}")
            delay = next_at - loop.time()
            if delay < -period:          # fell behind; resync rather than spin
                next_at = loop.time()
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    async def _mfc_cycle(self) -> None:
        """One poll of every MFC. Separate from _mfc_loop's timing so the
        virtual reactor can drive it a tick at a time (see VirtualReactor.tick)."""
        if self.mfcs:
            self._absorb(await asyncio.gather(
                *(d.read() for d in self.mfcs.values()),
                return_exceptions=True,
            ))
            # The gas is selected on the MFC itself and moves with it, so the
            # log headings and the recipe's own prose follow it from here rather
            # than from any config.
            names = self.gas_names()
            self.recording.set_gas_names(names)

    async def _current_cycle(self) -> None:
        cfg = self.cfg
        # Instruments only - the MFCs have their own loop (see _mfc_loop).
        if self.instruments:
            self._absorb(await asyncio.gather(
                *(d.read() for d in self.instruments.values()),
                return_exceptions=True,
            ))

        snap = self.snapshot
        self._last_cycle = self.clock.wall()
        self._cycle_count += 1

        # Column name -> the snapshot key it is measured from. Used both to
        # build the sample and to decide which columns are fresh this row.
        src = {
            "pressure": "pressure",
            "stage_temp": "stage.temp",
            **{f"gauge_{g.id}": f"gauge.{g.id}" for g in cfg.gauges},
            **{f"mfc_{m}": f"mfc.{m}.flow" for m in self.mfcs},
            **{f"inst_{i}": f"inst.{i}" for i in self.instruments},
            **{f"aux_{a.id}": f"aux.{a.id}" for a in cfg.aux_inputs},
            # Units are per-supply config (unit_v / unit_i, V and mA on the
            # Glassman) and are deliberately NOT baked into the column names:
            # the analysis page keys its saved plot layout on column name, so
            # these have to stay stable. See docs/GLASSMAN_FL.md.
            # Each supply declares its own namespace and channels, so the
            # Glassman keeps its established hv_hv_* columns while the Keithley
            # DC supplies get psu_<id>_*.
            **{f"{d.key_prefix}_{pid}_{col}": f"{d.key_prefix}.{pid}.{key}"
               for pid, d in self.supplies.items()
               for col, key in d.log_channels().items()},
        }
        sample = {
            "t": self._last_cycle,
            # Commanded valve state, not a measurement: always current, so it is
            # never blanked out of a row.
            "dosing": bool(self.valve_state.get(self.runs.session.dose_valve)),
            "beam_on": not self.valve_state.get(self.runs.session.plasma_switch, True),
            **{col: snap.get(key) for col, key in src.items()},
        }
        # Channels NOT measured since the previous row: written blank rather
        # than repeating a stale reading, since each loop runs at its own rate
        # (DAQ ~2 Hz, instruments ~5 Hz, MFCs ~6 Hz) and a row should carry only
        # what was really sampled at that instant. The commanded valve flags are
        # never in here - they are state, not a measurement.
        stale = {col for col, key in src.items()
                 if self.snapshot_seq.get(key, 0) <= self._last_row_seq}
        self._last_row_seq = self._read_seq
        # Fractional cycle number + paused flag, computed now (at log time) so a
        # sample taken mid-wall-step still gets an accurate position. Stored on
        # progress so the logger's by-cycle export and the telemetry share them.
        prog = self.recipes.progress
        prog.cycle_fraction = self.recipes.cycle_fraction()
        prog.paused = self.recipes.cycle_paused
        prog.pause_reason = self.recipes.pause_reason

        # history/telemetry keep the carried-forward values: the live charts and
        # tiles must show the last known reading, not blink out between polls.
        # Only the file gets the blanks.
        self.history.append(sample)
        if self.recipes.busy:
            self.recording.submit_run_sample(sample, prog, blank=stale)
        await self._publish()

    # ====================================================================== #
    #  Commands
    # ====================================================================== #

    def set_soft_open_params(self, params: dict) -> dict[str, float]:
        """Take the soft-open pulse settings from the UI's saved run params.

        Called on every /api/run_params save and once at startup, so a manual
        open from the Hardware tab uses the same numbers the Run tab shows.
        Unknown or unparseable keys leave the current value alone; `pulses` of 0
        disables the train and an open becomes one plain flip again.
        """
        for key, name in (("pulses", "ar_soft_open_pulses"),
                          ("on_s", "ar_soft_open_on_s"),
                          ("gap_s", "ar_soft_open_gap_s")):
            if name not in params:
                continue
            try:
                value = float(params[name])
            except (TypeError, ValueError):
                continue
            self.soft_open[key] = max(0.0, value)
        # The same saved parameters carry the flag tolerance, and setpoint
        # monitoring needs it outside a run too (pre-start, the Hardware tab).
        if "tolerance_pct" in params:
            try:
                self.flag_tolerance = max(0.0, float(params["tolerance_pct"]) / 100.0)
            except (TypeError, ValueError):
                pass
        return dict(self.soft_open)

    def _soft_open_plan(self, valve_id: str) -> tuple[int, float, float] | None:
        """(pulses, on_s, gap_s) if opening this valve should be pulsed, else
        None. None whenever the valve is not flagged or the operator has set
        the pulse count to zero."""
        cfg = next((v for v in self.cfg.valves if v.id == valve_id), None)
        if cfg is None or not getattr(cfg, "soft_open", False):
            return None
        pulses = int(self.soft_open.get("pulses", 0) or 0)
        if pulses <= 0:
            return None
        return (pulses, float(self.soft_open.get("on_s", 0.05)),
                float(self.soft_open.get("gap_s", 0.5)))

    async def _soft_open(self, valve_id: str, plan: tuple[int, float, float],
                         reason: str) -> None:
        """Bleed a valve open: N pulses of on_s, gap_s apart, then leave it OPEN.

        Requested 2026-08-26 for the Ar pneumatic - opening it in one flip dumps
        the Ar built up behind it into the reactor. One pulse is the default:
        the valve is too slow for a short command to move it far, so several
        pulses just made several inrushes. The pulses use the quiet write path
        deliberately:

        - no event per flip (10 log lines for one open is noise; one summary
          line goes out instead, and the final open logs normally);
        - no chart mark per flip, so the valve marker still reads as one open;
        - no valve_state.json write per flip, and no MFC-zeroing side effect.
          That last one matters: set_valve zeroes an MFC whose isolation valve
          closes, and the closes here are part of OPENING, not a close.
        """
        pulses, on_s, gap_s = plan
        shape = (f"{on_s:g} s pulse, then {gap_s:g} s" if pulses == 1
                 else f"{pulses} x {on_s:g} s pulses {gap_s:g} s apart")
        self._event("valve",
                    f"{valve_id}: soft open - {shape}, then full open"
                    + (f" ({reason})" if reason else ""))
        for _ in range(pulses):
            await self.drive_fill_valve(valve_id, True)
            await asyncio.sleep(on_s)
            await self.drive_fill_valve(valve_id, False)
            await asyncio.sleep(gap_s)

    async def set_valve(self, valve_id: str, state: bool, *, reason: str = "") -> None:
        if valve_id not in self.valve_state:
            raise KeyError(f"unknown valve '{valve_id}'")
        if self.daq is None:
            raise RuntimeError("DAQ not started")
        # A flagged valve is pulsed in rather than opened in one flip. This sits
        # in set_valve, not in the callers, so it covers EVERY open there is -
        # Hardware tab, pre-start, recipe step - as the operator asked.
        plan = self._soft_open_plan(valve_id) if state else None
        if plan is not None:
            await self._soft_open(valve_id, plan, reason)
        await self.daq.write_do(valve_id, state)
        self.valve_state[valve_id] = state
        self._save_valve_state()
        self.marks.append({"t": self.clock.wall(), "id": valve_id,
                           "state": bool(state), "reason": reason})
        self._event("valve", f"{valve_id} -> {'OPEN' if state else 'closed'}"
                             + (f" ({reason})" if reason else ""))

        # Requested by operator: closing an MFC's isolation valve zeroes that
        # MFC's setpoint (the mirror of the check in set_mfc_setpoint, which
        # refuses to raise a setpoint while the isolation valve is closed).
        if not state:
            for m in self.cfg.mfcs:
                if m.isolation_valve == valve_id and m.id in self.mfcs:
                    with contextlib.suppress(Exception):
                        await self.set_mfc_setpoint(m.id, 0.0)

    # -- valve identification sweep ---------------------------------------- #

    @property
    def sweep(self) -> dict:
        return self._sweep.state

    @property
    def sweep_running(self) -> bool:
        return self._sweep.running

    @property
    def identification_available(self) -> bool:
        return self.daq is not None

    async def identify_write(self, line: str, state: bool) -> None:
        await self.daq.id_write(line, state)

    async def identify_release(self, line: str) -> None:
        await self.daq.id_release(line)

    async def identify_release_all(self) -> None:
        await self.daq.id_release_all()

    async def start_valve_sweep(
        self, lines: list[str], *, reps: int = 3,
        on_s: float = 1.0, off_s: float = 1.0, gap_s: float = 3.0,
        start_index: int = 0,
    ) -> None:
        await self._sweep.start(lines, reps=reps, on_s=on_s, off_s=off_s,
                                gap_s=gap_s, start_index=start_index)

    async def stop_valve_sweep(self) -> None:
        await self._sweep.stop()

    def mark_sweep_line(self, valve_id: str = "", note: str = "") -> dict:
        return self._sweep.mark(valve_id, note)

    async def set_mfc_setpoint(self, mfc_id: str, sccm: float) -> float:
        dev = self.mfcs.get(mfc_id)
        if dev is None:
            raise KeyError(f"unknown MFC '{mfc_id}'")

        # Requested by operator: an MFC with a configured isolation valve
        # cannot be set above 0 sccm while that valve is closed.
        if sccm > 0:
            cfg = next((m for m in self.cfg.mfcs if m.id == mfc_id), None)
            iso = cfg.isolation_valve if cfg else None
            if iso and not self.valve_state.get(iso, False):
                label = self._label("valve", iso, iso)
                raise ValueError(
                    f"{self.gas_label(mfc_id)}: isolation valve '{label}' "
                    "is closed - "
                    "open it before setting flow above 0"
                )

        result = await dev.set_setpoint_sccm(sccm)   # type: ignore[attr-defined]
        self._setpoint_changed[f"mfc.{mfc_id}"] = self.clock.elapsed()
        self._event("mfc",
                    f"{self.gas_label(mfc_id)} setpoint -> {result:.2f} sccm")
        return result

    # -- background fill-pressure regulation ------------------------------- #

    async def drive_fill_valve(self, valve_id: str, state: bool) -> None:
        """Drive a valve without emitting an event (used by the fast regulator
        pulsing, which would otherwise flood the event log)."""
        if self.daq is None:
            return
        await self.daq.write_do(valve_id, state)
        self.valve_state[valve_id] = state

    @property
    def regulator(self) -> dict:
        return self._fill.state

    def has_valve(self, valve_id: str) -> bool:
        return valve_id in self.valve_state
    def update_fill_regulation(self, *, target_torr: float | None = None,
                               pulse_on_s: float | None = None,
                               pulse_off_s: float | None = None,
                               tolerance_frac: float | None = None) -> dict:
        """Retune the running fill regulator in place, without restarting it.

        Restarting would close the fill valve and drop the chamber off its
        setpoint mid-run, which is exactly what a mid-run tweak must not do.
        The loop re-reads these on its next pass (see _run_regulation).
        """
        for key, value in (("target_torr", target_torr),
                           ("pulse_on_s", pulse_on_s),
                           ("pulse_off_s", pulse_off_s),
                           ("tolerance_frac", tolerance_frac)):
            if value is not None:
                self.regulator[key] = float(value)
        return dict(self.regulator)

    async def start_fill_regulation(
        self, *, valve: str, gauge: str, target_torr: float,
        pulse_on_s: float = 0.1, pulse_off_s: float = 0.3,
        tolerance_frac: float = 0.2,
    ) -> None:
        await self._fill.start(valve=valve, gauge=gauge, target_torr=target_torr,
                               pulse_on_s=pulse_on_s, pulse_off_s=pulse_off_s,
                               tolerance_frac=tolerance_frac)

    async def stop_fill_regulation(self) -> None:
        await self._fill.stop()

    # ====================================================================== #
    #  Pre-start sequence
    # ====================================================================== #

    @property
    def prestart(self) -> dict:
        return self._prestart.state

    async def start_prestart(self, params: dict) -> None:
        params = dict(params or {})
        recipe_id = params.pop("recipe_id", None)
        revision = params.pop("recipe_revision", None)
        await self._prestart.start(
            params, recipe_id=recipe_id,
            expected_revision=int(revision) if revision is not None else None)

    async def stop_prestart(self) -> None:
        await self._prestart.stop()

    async def abort_prestart(self) -> None:
        await self._prestart.abort()

    def consume_prestart_primed(self) -> None:
        self._prestart.consume_primed()

    async def hv_off(self, *, reason: str = "") -> None:
        """Command every HV supply's output OFF.

        The only write this program makes to the plasma supply, and it only ever
        turns it *off* - requested by the operator on 2026-08-21 so a run that
        ends (completed, aborted, or crashed) cannot leave HV energised. There
        is still no way to set a voltage or turn HV on from here.

        The supply's dialled-in voltage and current programs are preserved - see
        GlassmanFL.hv_off for how, and for the fact that any Set command moves
        the supply into REMOTE until LOC/REM is pressed. A failure is reported,
        never raised: this runs inside run teardown.
        """
        for ps_id, dev in self.supplies.items():
            off = getattr(dev, "hv_off", None)
            if off is None:
                continue
            try:
                await off()
            except Exception as exc:
                self._event("error",
                            f"HV off failed for {ps_id}: {type(exc).__name__}: {exc}")
            else:
                tail = f" ({reason})" if reason else ""
                self._event("recipe", f"HV commanded off: {ps_id}{tail}")

    # -- operator control of a single supply -------------------------------- #
    #
    # Requested 2026-08-25: voltage/current fields and an output toggle per
    # supply on the Hardware tab. These are manual, one supply at a time, and
    # entirely separate from the automatic pre-start/run-end switching below.
    # Nothing calls them except the HTTP API.
    #
    # They raise on a bad request rather than swallowing it, unlike the
    # teardown helpers: an operator who presses a button is waiting for an
    # answer, and a silent no-op there is worse than an error toast.

    def _supply(self, supply_id: str):
        dev = self.supplies.get(supply_id)
        if dev is None:
            raise KeyError(f"no such power supply: {supply_id!r}")
        if not dev.connected:
            raise RuntimeError(f"{supply_id} is not connected")
        return dev

    async def set_supply_voltage(self, supply_id: str, volts: float) -> dict[str, Any]:
        dev = self._supply(supply_id)
        setter = getattr(dev, "set_voltage", None)
        if setter is None:
            raise RuntimeError(
                f"{supply_id} has no voltage control in this program")
        await setter(volts)
        self._setpoint_changed[f"psu.{supply_id}"] = self.clock.elapsed()
        self._event("command", f"{supply_id}: voltage set to {float(volts):g} V")
        return {"id": supply_id, "voltage": float(volts)}

    async def set_supply_current(self, supply_id: str, amps: float) -> dict[str, Any]:
        dev = self._supply(supply_id)
        setter = getattr(dev, "set_current", None)
        if setter is None:
            raise RuntimeError(
                f"{supply_id} has no current control in this program")
        await setter(amps)
        self._event("command", f"{supply_id}: current limit set to {float(amps):g} A")
        return {"id": supply_id, "current": float(amps)}

    def _settling(self, key: str) -> bool:
        """True while a device is still on its way to a newly commanded value."""
        last = self._setpoint_changed.get(key)
        return last is not None and (self.clock.elapsed() - last) < SETPOINT_SETTLE_S

    def setpoint_flags(self) -> list[dict[str, Any]]:
        """Every commanded value that its own measurement does not agree with.

        Exactly the precursor fill pressure's rule, applied to everything else
        this program commands: |measured - commanded| / commanded past
        `flag_tolerance`. Warn-only, and computed fresh each telemetry tick so
        it clears itself the instant the device catches up - the operator asked
        for warnings that "stay up only when they are out of bounds".

        A commanded zero is not monitored: there is no relative baseline, and a
        gas that is off is not a fault. Nothing here is a limit - a setpoint
        this flags is still written to the hardware exactly as typed.
        """
        out: list[dict[str, Any]] = []
        tol = self.flag_tolerance
        if tol <= 0:
            return out

        for mid, dev in self.mfcs.items():
            sp = getattr(dev, "commanded_sccm", None)
            flow = self.snapshot.get(f"mfc.{mid}.flow")
            if not isinstance(sp, (int, float)) or sp <= 0:
                continue
            if not isinstance(flow, (int, float)):
                continue
            if self._settling(f"mfc.{mid}"):
                continue
            off = abs(flow - sp) / sp
            if off > tol:
                out.append({
                    "id": mid, "kind": "mfc",
                    "label": self.gas_label(mid),
                    "commanded": float(sp), "measured": float(flow),
                    "unit": "sccm", "off_frac": off,
                })

        for pid, dev in self.supplies.items():
            st = dev.status()
            if not st.get("output_on"):
                continue
            # A supply in CONSTANT CURRENT is doing its job at a voltage below
            # its setpoint - that is what CC means, and the coils run there all
            # run (Zach, 2026-09-09: "of course they're not, they're on CC mode,
            # not CV"). Only a CV supply owes its voltage setpoint anything.
            if st.get("mode") == "CC":
                continue
            sp = st.get("voltage_setpoint")
            meas = st.get("voltage")
            if not isinstance(sp, (int, float)) or sp <= 0:
                continue
            if not isinstance(meas, (int, float)):
                continue
            if self._settling(f"psu.{pid}"):
                continue
            off = abs(meas - sp) / sp
            if off > tol:
                out.append({
                    "id": pid, "kind": "supply",
                    "label": self._label("supply", pid, st.get("label") or pid),
                    "commanded": float(sp), "measured": float(meas),
                    "unit": "V", "off_frac": off,
                })

        out.sort(key=lambda d: -d["off_frac"])
        return out

    async def set_supply_output(self, supply_id: str, on: bool) -> dict[str, Any]:
        dev = self._supply(supply_id)
        setter = getattr(dev, "set_output", None)
        if setter is None:
            raise RuntimeError(
                f"{supply_id} has no output control in this program")
        await setter(bool(on))
        self._event("command",
                    f"{supply_id}: output {'ON' if on else 'OFF'} (operator)")
        self._setpoint_changed[f"psu.{supply_id}"] = self.clock.elapsed()
        return {"id": supply_id, "output_on": bool(on)}

    def _sample_bias_supply(self):
        """The one supply flagged `sample_bias` in the config, or (None, None).

        config.py already refuses more than one, so the first is the one.
        """
        for ps_id, dev in self.supplies.items():
            if getattr(getattr(dev, "cfg", None), "sample_bias", False):
                return ps_id, dev
        return None, None

    async def set_sample_bias_output(
        self, on: bool, *, volts: float | None = None,
        polarity: int | None = None, reason: str = "",
    ) -> bool:
        """Switch the sample-bias supply output, optionally setting its level.

        Called by the recipe runner to bracket the beam (operator request
        2026-08-26): a live stage bias corrupts the stage thermocouple, so the
        stage is only energised from bias_lead_s before the beam comes on until
        bias_trail_s after it goes off, and the TC reads clean through the rest
        of the cycle. See Step.bias_v and RecipeRunner._schedule_bias.

        `volts` is written only when given - the runner passes it on the FIRST
        ON of a run and never again, so a level adjusted by hand on the Hardware
        tab mid-run is not overwritten every cycle. The magnitude is what
        reaches the instrument; `polarity` is lead-orientation bookkeeping that
        signs the LOGGED value, because the 2260B is single-quadrant and cannot
        source a negative voltage.

        Returns False if there is no sample-bias supply to command. Raises if
        there is one and the command fails - the caller decides what a failed
        bias means for the run.
        """
        ps_id, dev = self._sample_bias_supply()
        set_output = getattr(dev, "set_output", None)
        if dev is None or set_output is None:
            return False
        if polarity is not None:
            dev.polarity = -1 if polarity < 0 else 1
        if volts is not None:
            await dev.set_voltage(abs(float(volts)))
        await set_output(bool(on))
        sign = "-" if getattr(dev, "polarity", 1) < 0 else "+"
        level = getattr(dev, "voltage_setpoint", None)
        level_txt = (f" at {sign}{abs(level):g} V"
                     if on and isinstance(level, (int, float)) else "")
        tail = f" ({reason})" if reason else ""
        self._event("recipe", f"{ps_id}: sample bias output "
                              f"{'ON' if on else 'OFF'}{level_txt}{tail}")
        return True

    async def arm_sample_bias(self, supply_id: str, *, volts: float,
                              polarity: int = 1, reason: str = "") -> dict[str, Any]:
        """Arm one configured sample-bias supply without energising its output."""
        dev = self._supply(supply_id)
        cfg = getattr(dev, "cfg", None)
        if not getattr(cfg, "sample_bias", False):
            raise RuntimeError(f"{supply_id} is not configured as the sample bias")
        dev.polarity = -1 if polarity < 0 else 1
        await dev.set_output(False)
        magnitude = abs(float(volts))
        if magnitude > 0:
            await dev.set_voltage(magnitude)
        sign = "-" if dev.polarity < 0 else "+"
        tail = f" ({reason})" if reason else ""
        self._event("recipe", f"{supply_id}: sample bias armed at "
                              f"{sign}{magnitude:g} V, output OFF{tail}")
        return {"id": supply_id, "voltage": magnitude,
                "polarity": dev.polarity, "output_on": False}

    async def supplies_output_on(self, *, sample_bias_v: float = 0.0,
                                 polarity: int = 1, reason: str = "") -> None:
        """Switch ON every supply configured with `prestart_output`.

        Called from pre-start (operator request, 2026-08-21): Zach wants the
        steering, grid and collimating supplies live before a run starts so he
        can see how the tool is behaving. They then stay on for the whole run -
        they are deliberately NOT tied to plasma events, because the collimating
        coil is what keeps the plasma stable when the beam dump is grounded, so
        cycling it with the beam would be actively harmful.

        The SAMPLE BIAS supply is the exception, and the reason this is not a
        plain loop. Since 2026-08-26 pre-start only ARMS it: the level and the
        lead orientation are programmed here, but the output is left OFF and the
        beam steps switch it (set_sample_bias_output). It used to come on here
        and stay on for the whole run, which held a potential on the stage
        continuously and made the stage thermocouple unreadable.

        Current limits are never touched: those are set by hand on each front
        panel and are Zach's.

        Failures are reported as events, never raised - this runs inside the
        pre-start sequence and one dead supply must not abort the rest.
        """
        magnitude = abs(float(sample_bias_v or 0.0))
        for ps_id, dev in self.supplies.items():
            cfg = getattr(dev, "cfg", None)
            if cfg is None or not getattr(cfg, "prestart_output", False):
                continue
            set_output = getattr(dev, "set_output", None)
            if set_output is None:
                continue
            try:
                if getattr(cfg, "sample_bias", False):
                    dev.polarity = -1 if polarity < 0 else 1
                    # Output OFF either way now - the beam brackets it. Which of
                    # the two cases this is still gets said, because "the bias
                    # supply is dark" should never be something to infer.
                    await set_output(False)
                    if magnitude <= 0.0:
                        self._event("recipe",
                                    f"{ps_id}: sample bias is 0 V, output stays off")
                        continue
                    await dev.set_voltage(magnitude)
                    sign = "-" if dev.polarity < 0 else "+"
                    self._event("recipe",
                                f"{ps_id}: sample bias armed at "
                                f"{sign}{magnitude:g} V - output follows the beam")
                else:
                    await set_output(True)
                    self._event("recipe", f"{ps_id}: output ON")
            except Exception as exc:
                self._event("error",
                            f"{ps_id}: output on failed: "
                            f"{type(exc).__name__}: {exc}")

    async def supplies_output_off(self, *, reason: str = "") -> None:
        """Switch OFF every supply configured with `prestart_output`.

        Called from `finish_run` - which the recipe runner invokes however a run
        ends, including a crash - and from the pre-start abort. Deliberately NOT
        called from `stop_prestart`: that only ends the *sequence* and hands the
        tool over primed for Start run, the same way it leaves Ar flowing and
        the fill pulsing.

        Never raises: this is teardown.
        """
        tail = f" ({reason})" if reason else ""
        for ps_id, dev in self.supplies.items():
            cfg = getattr(dev, "cfg", None)
            if cfg is None or not getattr(cfg, "prestart_output", False):
                continue
            set_output = getattr(dev, "set_output", None)
            if set_output is None:
                continue
            try:
                await set_output(False)
            except Exception as exc:
                self._event("error",
                            f"{ps_id}: output off failed: "
                            f"{type(exc).__name__}: {exc}")
            else:
                self._event("recipe", f"{ps_id}: output OFF{tail}")

    # ====================================================================== #
    #  Run admission and lifecycle
    # ====================================================================== #

    async def start_recipe(self, recipe: Recipe) -> None:
        await self.runs.start(recipe)

    async def start_ald_run(self, params: dict) -> Recipe:
        """Build and launch an e-beam ALD run from UI parameters."""
        from .control.recipe import build_ald_recipe
        from .control.parameters import RunParameters

        typed = RunParameters.normalize(params, mode="ald")
        return await self._start_built_run(build_ald_recipe(typed), typed.raw)

    async def start_cvd_run(self, params: dict) -> Recipe:
        """Build and launch an electron-enhanced CVD run from UI parameters.

        Same plumbing as the ALD run; the difference lives entirely in the
        recipe (continuous beam, no pump B, cycle-anchored gas schedule).
        """
        from .control.recipe import build_cvd_recipe
        from .control.parameters import RunParameters

        typed = RunParameters.normalize(params, mode="cvd")
        return await self._start_built_run(build_cvd_recipe(typed), typed.raw)

    async def _start_built_run(self, recipe: Recipe, params: dict) -> Recipe:
        return await self.runs.start(recipe, params)

    async def update_run_params(self, params: dict) -> dict[str, Any]:
        return await self.runs.update_params(params)

    # -- operator run naming (see RUN_NAME_PATH) ---------------------------- #

    @property
    def last_run_name(self) -> str:
        """Name of the last run that actually started, or "" if there is none."""
        try:
            data = json.loads(self.paths.run_name.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return datalog.sanitize_run_name(str(data.get("name") or ""))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not read %s: %s", self.paths.run_name, exc)
        return ""

    def suggest_run_name(self) -> str:
        """What to pre-fill the run-name box with: the last started run's name
        incremented. Purely a suggestion - the operator can type anything."""
        return datalog.next_run_name(self.last_run_name)

    def remember_run_name(self, name: str) -> None:
        clean = datalog.sanitize_run_name(name)
        if not clean:
            return
        try:
            self.paths.run_name.parent.mkdir(parents=True, exist_ok=True)
            self.paths.run_name.write_text(
                json.dumps({"name": clean, "started_at": self.clock.wall()}, indent=2),
                encoding="utf-8")
        except Exception as exc:
            log.warning("could not persist run name: %s", exc)

    @property
    def server_running(self) -> bool:
        return self._running

    @property
    def prestart_running(self) -> bool:
        return bool(self.prestart.get("running"))

    @property
    def prestart_cleanup_required(self) -> bool:
        return bool(self.prestart.get("cleanup_available")
                    and not self.prestart.get("primed"))

    async def finish_run(self) -> None:
        await self.runs.finish()

    async def cleanup_run(self, session: RunSession) -> None:
        """Existing hardware cleanup, with policy supplied by the run owner."""
        with contextlib.suppress(Exception):
            await self.stop_fill_regulation()
        await self.hv_off(reason="run end")
        await self.supplies_output_off(reason="run end")
        if not session.end_cleanup:
            return
        if self.valve_state.get(session.plasma_switch) is not False:
            with contextlib.suppress(Exception):
                await self.set_valve(session.plasma_switch, False, reason="run end - relay at rest")
        if session.fill_valve in self.valve_state:
            with contextlib.suppress(Exception):
                await self.set_valve(session.fill_valve, False, reason="run end")
        for mfc_id in list(self.mfcs):
            with contextlib.suppress(Exception):
                await self.set_mfc_setpoint(mfc_id, 0.0)
        self._event("recipe", "run end: MFCs zeroed, fill stopped, "
                              "fill valve closed, HV off")

    async def abort_recipe(self) -> None:
        await self.runs.abort()

    @property
    def run_in_progress(self) -> bool:
        return self.runs.in_progress

    def report_event(self, kind: str, message: str) -> None:
        """Publish controller events through the application event owner."""
        self._event(kind, message)

    #: Event kinds that belong in the error log as well as the event log.
    #: One list, defined next to the writer that also uses it.
    ERROR_KINDS = datalog.ERROR_KINDS

    def _event(self, kind: str, message: str, *, record=True) -> None:
        entry = {"t": self.clock.wall(), "kind": kind, "message": message}
        self.events.append(entry)
        if kind in self.ERROR_KINDS:
            self.errors.append(entry)
            log.warning("[%s] %s", kind, message)
        else:
            log.info("[%s] %s", kind, message)
        # A run keeps its own unbounded copy next to its data files.
        if record:
            self.recording.submit_event(entry)

    # -- operator label overrides ------------------------------------------ #

    def _load_labels(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.paths.labels.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not read %s: %s", self.paths.labels, exc)
        return {}

    def _label(self, kind: str, dev_id: str, default: str) -> str:
        return self.label_overrides.get(kind, {}).get(dev_id) or default

    def _gas_name(self, mfc_id: str) -> str:
        """The gas on an MFC line, or "" if nothing has said what it is.

        The unit's own gas selection is the authority: change it from H2 to NH3
        and it reports "2: NH3" (gas-table index and name), full scale moves
        with it, and everything that names the line has to follow. An operator
        rename wins over it - that is someone stating the name deliberately.

        Empty when neither has spoken, deliberately: nothing static in this
        program may claim a gas, so a run file that cannot know says so by
        falling back to the CHANNEL (see gas_names / DataLogger._heading).
        """
        over = self.label_overrides.get("mfc", {}).get(mfc_id)
        if over:
            return over.split(" - ")[0].strip() or over
        gas = str(getattr(self.mfcs.get(mfc_id), "gas", "") or "")
        return gas.split(":")[-1].strip()      # "2: NH3" -> "NH3"

    def gas_names(self) -> dict[str, str]:
        """MFC id -> the gas it is really flowing, KNOWN ones only. Feeds every
        log heading and the recipe's step prose, both of which fall back to the
        channel id rather than invent a gas."""
        return {mid: name for mid in self.mfcs
                if (name := self._gas_name(mid))}

    def gas_label(self, mfc_id: str) -> str:
        """What to CALL this line on screen - never empty. The gas if one is
        known, else the channel as named in reactor.yaml ("MFC 1")."""
        if name := self._gas_name(mfc_id):
            return name
        label = next((m.label for m in self.cfg.mfcs if m.id == mfc_id), "") or ""
        return label.split(" - ")[0].strip() or mfc_id.upper()

    def mfc_label(self, mfc_id: str, device_label: str = "") -> str:
        """Full display label for an MFC line, with the gas kept honest.

        The reactor.yaml labels read "<channel> - <what it is for>" ("MFC 1 -
        reactive background"). The head is replaced by the gas the unit actually
        reports, so the tile reads "NH3 - reactive background" and reverts to
        "MFC 1 - ..." if the device stops saying. An operator rename overrides
        the whole thing.
        """
        over = self.label_overrides.get("mfc", {}).get(mfc_id)
        if over:
            return over
        cfg_label = next((m.label for m in self.cfg.mfcs if m.id == mfc_id), "")
        cfg_label = cfg_label or device_label or mfc_id
        _, sep, tail = cfg_label.partition(" - ")
        return f"{self.gas_label(mfc_id)}{sep}{tail}" if sep else self.gas_label(mfc_id)

    # -- valve state persistence (see VALVE_STATE_PATH) --------------------- #

    def _load_valve_state(self) -> dict[str, bool]:
        try:
            data = json.loads(self.paths.valves.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: bool(v) for k, v in data.items()}
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not read %s: %s", self.paths.valves, exc)
        return {}

    def _save_valve_state(self) -> None:
        try:
            self.paths.valves.write_text(
                json.dumps(self.valve_state, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("could not save %s: %s", self.paths.valves, exc)

    def set_label(self, kind: str, dev_id: str, label: str) -> str:
        """Rename a valve / MFC / gauge from the UI, persisted to labels.json.

        An empty label clears the override, reverting to the reactor.yaml name.
        Renaming is display-only: it never changes an id, a channel, or wiring.
        """
        if kind not in ("valve", "mfc", "gauge"):
            raise ValueError(f"cannot rename '{kind}'")
        valid = {
            "valve": {v.id for v in self.cfg.valves},
            "mfc": set(self.mfcs),
            "gauge": {g.id for g in self.cfg.gauges},
        }[kind]
        if dev_id not in valid:
            raise KeyError(f"unknown {kind} '{dev_id}'")

        label = label.strip()
        bucket = self.label_overrides.setdefault(kind, {})
        if label:
            bucket[dev_id] = label
        else:
            bucket.pop(dev_id, None)
        try:
            self.paths.labels.write_text(
                json.dumps(self.label_overrides, indent=2), encoding="utf-8")
        except Exception as exc:
            raise RuntimeError(f"could not save label: {exc}") from exc
        self._event("config", f"renamed {kind} {dev_id} -> {label or '(default)'}")
        return label

    # -- ellipsometer stream ------------------------------------------------ #

    def _on_ellipsometer_point(self, point: EllipsometerPoint) -> None:
        """Route one streamed FS-1 measurement into a per-acquisition sidecar,
        stamped with the reactor clock. A new sidecar opens when the point index
        resets to 1 or after an idle gap - the two ways one acquisition ends and
        the next begins (the stream carries no explicit start/stop marker)."""
        self.recording.capture_ellipsometer_point(
            point, self.cfg.ellipsometer.idle_gap_s
        )

    def _on_ellipsometer_state(self, connected: bool, detail: str) -> None:
        self._event("ellipsometer",
                    "stream connected" if connected
                    else f"stream disconnected ({detail})")

    def state(self) -> dict[str, Any]:
        return self.telemetry.state()

    def trend(self, limit: int = 1800) -> list[dict[str, Any]]:
        return self.telemetry.trend(limit)

    def subscribe(self) -> asyncio.Queue:
        return self.telemetry.subscribe()

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.telemetry.unsubscribe(q)

    async def _publish(self) -> None:
        await self.telemetry._publish()
