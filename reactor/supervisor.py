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
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import ReactorConfig
from .control.recipe import Recipe, RecipeRunner
from .control.prestart import PrestartController
from . import datalog
from .datalog import DataLogger
from .recording import RecordingService
from .telemetry import Telemetry
from .devices.base import Device, Reading
from .devices.ellipsometer import EllipsometerClient, EllipsometerPoint
from .devices.glassman_fl import GlassmanFL
from .devices.keithley_2260b import Keithley2260B
from .devices.instrument import ScpiInstrument
from .devices.mks_mfc import MfcRegisters, MksMfc
from .devices.nidaq import AiSpec, DaqPlan, DoSpec, NiDaqBackend

log = logging.getLogger("reactor.supervisor")

HISTORY_SAMPLES = 18000       # at 5 Hz that is one hour of trend
RECONNECT_EVERY_S = 5.0       # retry a dropped instrument this often

#: Operator-edited display names (rename a valve/MFC/gauge when swapping
#: chemicals) live here, not in reactor.yaml, so a rename is a UI action rather
#: than a hand edit of the hardware map. Keyed as {"valve"|"mfc"|"gauge": {id: label}}.
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

#: Name of the last run actually STARTED (not merely typed into the box), so the
#: UI can pre-fill the next one incremented - "Mo-014" -> "Mo-015". Written when
#: a run starts, which is what makes the sequence reflect real runs: abandoning a
#: pre-filled name without starting leaves the counter where it was.
RUN_NAME_PATH = Path(__file__).resolve().parent.parent / "config" / "last_run.json"

#: Digital-output lines available for valve identification, grouped by module.
#: Verified present on this hardware. Note a 9375's port0 is INPUT; outputs are
#: port1.
DO_LINE_GROUPS: dict[str, list[str]] = {
    "cDAQ2Mod2 (NI 9472, 8)": [f"cDAQ2Mod2/port0/line{i}" for i in range(8)],
    "cDAQ2Mod3 (NI 9472, 8)": [f"cDAQ2Mod3/port0/line{i}" for i in range(8)],
    "cDAQ1Mod3 (NI 9375, 16)": [f"cDAQ1Mod3/port1/line{i}" for i in range(16)],
}


class Supervisor:
    def __init__(self, cfg: ReactorConfig) -> None:
        self.cfg = cfg
        self.recipes = RecipeRunner(self)
        self._run_start_lock = asyncio.Lock()
        self._start_cancelled = False
        self.logger = DataLogger(cfg)
        self.recording = RecordingService(
            self.logger, lambda message: self._event("error", message),
            on_capture=lambda name: self._event("ellipsometer", f"acquisition start -> {name}"))

        self.snapshot: dict[str, Any] = {}
        self.readings: dict[str, Reading] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SAMPLES)
        #: A long scrollback, because 250 was nowhere near enough: one run emits
        #: roughly ten events a cycle, so a 150-cycle run pushed the whole
        #: pre-start out of the buffer before anyone could read it. This is only
        #: the in-memory copy the UI scrolls; every event is ALSO written to
        #: server.log by _event(), which rotates and is the permanent record.
        self.events: deque[dict[str, Any]] = deque(maxlen=20000)

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
        #: A counter rather than a wall-clock stamp because time.time() has
        #: ~16 ms resolution on Windows: two reads inside one clock tick would
        #: compare equal and a genuinely fresh reading would be dropped as
        #: stale.
        self._read_seq = 0
        self.snapshot_seq: dict[str, int] = {}
        #: _read_seq as of the last run-export row: the cutoff for "fresh since"
        self._last_row_seq = 0

        #: operator label overrides, {kind: {id: label}}; persisted to LABELS_PATH
        self.label_overrides: dict[str, dict[str, str]] = self._load_labels()
        self._cycle_count = 0
        self._running = False
        self.telemetry = Telemetry(self, DO_LINE_GROUPS)

        # Valve flip markers (for the current-trace overlay). Every set_valve is
        # recorded with its reason so the UI can mark scheduled vs reignite flips.
        self.marks: deque[dict[str, Any]] = deque(maxlen=3000)
        # Which valves the current run treats as the dose valve and plasma switch
        # (recorded into each trend sample). Defaults match the identified valves.
        self._run_dose_valve = "prec1"
        self._run_plasma_switch = "plasma_ground"
        # When an ALD run ends (completion or abort), leave the tool in a quiet
        # state: zero every MFC, stop the fill pulsing, close the fill valve.
        # Requested by the operator; only armed for ALD runs, not file recipes.
        self._run_fill_valve = "rpm_top"
        self._run_end_cleanup = False

        # Valve identification sweep
        self._sweep_task: asyncio.Task | None = None
        self._sweep_abort = asyncio.Event()
        self.sweep: dict[str, Any] = {"running": False}

        self._prestart = PrestartController(self)

        # Background fill-pressure regulation
        self._reg_task: asyncio.Task | None = None
        self._reg_stop = asyncio.Event()
        self.regulator: dict[str, Any] = {"running": False}

        # In-situ ellipsometer (FS-1) live stream: a read-only subscriber that
        # timestamps each streamed measurement with the reactor clock into a
        # per-acquisition sidecar, so a refit file downloaded afterwards can be
        # put back onto the reactor clock (reactor/analysis/ellipsometer_merge).
        # Created here, started in start(); None when disabled in config.
        self.ellipsometer: EllipsometerClient | None = None
        if cfg.ellipsometer.enabled and cfg.ellipsometer.host:
            self.ellipsometer = EllipsometerClient(
                cfg.ellipsometer.host,
                cfg.ellipsometer.port,
                on_point=self._on_ellipsometer_point,
                on_state=self._on_ellipsometer_state,
            )

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

    async def start(self) -> None:
        """Connect to everything and begin polling. Commands nothing.

        A device that fails to connect is recorded as failed and the rest of the
        system carries on - so a missing cDAQ or an unplugged DMM gives you a
        readable interface showing what is wrong, not a crash.
        """
        cfg = self.cfg
        self._plan = self._build_plan()

        self.daq = NiDaqBackend()
        try:
            await self.daq.configure(self._plan)
            self._event("startup",
                        f"DAQ configured: {len(self._plan.ai)} analog inputs, "
                        f"{len(self._plan.do)} digital outputs")
        except Exception as exc:
            self._event("error", f"DAQ configure failed: {type(exc).__name__}: {exc}")

        for m in cfg.mfcs:
            regs = MfcRegisters(
                setpoint_read=m.register_map.setpoint_read,
                setpoint_write=m.register_map.setpoint_write,
                word_order=m.register_map.word_order,
            )
            dev = MksMfc(m, regs)
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
            inst = ScpiInstrument(i)
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
            dev = (GlassmanFL(ps) if ps.driver == "glassman_fl"
                   else Keithley2260B(ps))
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

    async def stop(self) -> None:
        """Abort a pending/active run or primed pre-start, then disconnect.

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
        self._running = False
        await self.abort_recipe()

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
        if self.prestart.get("running") or self.prestart.get("done"):
            self._event("recipe",
                        "server stopping: aborting pre-start "
                        f"({'in progress' if self.prestart.get('running') else 'primed'})")
            with contextlib.suppress(Exception):
                await self.abort_prestart()

        with contextlib.suppress(Exception):
            await self.stop_fill_regulation()
        self._sweep_abort.set()

        for task in (self._loop_task, self._current_task, self._mfc_task,
                     self._reconnect_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self.ellipsometer is not None:
            with contextlib.suppress(Exception):
                await self.ellipsometer.stop()

        # Power supplies are included here purely to close their serial ports.
        # GlassmanFL.disconnect() deliberately commands nothing - it does NOT
        # send HV OFF - so stopping the server cannot switch off a plasma Zach
        # set by hand at the front panel. See reactor/devices/glassman_fl.py.
        for dev in (list(self.mfcs.values()) + list(self.instruments.values())
                    + list(self.supplies.values())):
            with contextlib.suppress(Exception):
                await dev.disconnect()
        if self.daq:
            with contextlib.suppress(Exception):
                await self.daq.close()

        await self.recording.close()

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
                    continue
                try:
                    await dev.connect()
                    self._event("startup",
                                f"instrument {dev_id} reconnected: {dev.identity}")
                except Exception as exc:
                    # Keep the reason visible in the tile, but don't flood the
                    # event log with one line per retry while it stays off.
                    dev.last_error = f"{type(exc).__name__}: {exc}"

            for dev_id, dev in list(self.supplies.items()):
                if dev.connected or not self._running:
                    continue
                try:
                    await dev.connect()
                    self._event("startup",
                                f"power supply {dev_id} reconnected: "
                                f"firmware {getattr(dev, 'firmware', '') or '?'}")
                except Exception as exc:
                    dev.last_error = f"{type(exc).__name__}: {exc}"

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
        self.recording.submit("write_sample", self.snapshot, self.recipes.progress,
                              sampled_at=time.time())

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

    async def _current_cycle(self) -> None:
        cfg = self.cfg
        # Instruments only - the MFCs have their own loop (see _mfc_loop).
        if self.instruments:
            self._absorb(await asyncio.gather(
                *(d.read() for d in self.instruments.values()),
                return_exceptions=True,
            ))

        snap = self.snapshot
        self._last_cycle = time.time()
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
            "dosing": bool(self.valve_state.get(self._run_dose_valve)),
            "beam_on": not self.valve_state.get(self._run_plasma_switch, True),
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
            self.recording.submit("write_run_sample", sample, prog, blank=stale)
        await self._publish()

    # ====================================================================== #
    #  Commands
    # ====================================================================== #

    async def set_valve(self, valve_id: str, state: bool, *, reason: str = "") -> None:
        if valve_id not in self.valve_state:
            raise KeyError(f"unknown valve '{valve_id}'")
        if self.daq is None:
            raise RuntimeError("DAQ not started")
        await self.daq.write_do(valve_id, state)
        self.valve_state[valve_id] = state
        self._save_valve_state()
        self.marks.append({"t": time.time(), "id": valve_id,
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
    def sweep_running(self) -> bool:
        return self._sweep_task is not None and not self._sweep_task.done()

    async def start_valve_sweep(
        self, lines: list[str], *, reps: int = 3,
        on_s: float = 1.0, off_s: float = 1.0, gap_s: float = 3.0,
        start_index: int = 0,
    ) -> None:
        """Pulse each line in turn so the operator can see which valve moves.

        Per line: (on, off) x reps, then a gap, then the next line. The operator
        watches the box and hits stop if anything is wrong.
        """
        if self.sweep_running:
            raise RuntimeError("a valve-identification sweep is already running")
        if self.daq is None:
            raise RuntimeError("DAQ not started")

        lines = [ln for ln in lines if ln]
        if not lines:
            raise ValueError("no lines to sweep")

        reps = max(1, min(10, int(reps)))
        on_s = max(0.1, min(5.0, float(on_s)))
        off_s = max(0.1, min(5.0, float(off_s)))
        gap_s = max(0.0, min(30.0, float(gap_s)))
        start_index = max(0, min(len(lines) - 1, int(start_index)))

        self._sweep_abort.clear()
        self.sweep = {
            "running": True, "lines": lines, "total": len(lines),
            "index": start_index, "current_line": None, "line_state": False,
            "rep": 0, "reps": reps, "phase": "starting",
            "on_s": on_s, "off_s": off_s, "gap_s": gap_s,
            "marks": [], "started_at": time.time(), "message": "",
        }
        self._sweep_task = asyncio.create_task(
            self._run_sweep(lines, reps, on_s, off_s, gap_s, start_index),
            name="valve-sweep",
        )
        self._event("valve-id",
                    f"sweep started: {len(lines)} lines, {reps}x "
                    f"{on_s:g}s on / {off_s:g}s off, {gap_s:g}s gap")

    async def _run_sweep(self, lines, reps, on_s, off_s, gap_s, start_index) -> None:
        try:
            for idx in range(start_index, len(lines)):
                if self._sweep_abort.is_set():
                    break
                line = lines[idx]
                self.sweep.update(index=idx, current_line=line, phase="pulse", rep=0)
                self._event("valve-id", f"pulsing {line}  ({idx + 1}/{len(lines)})")
                for rep in range(reps):
                    if self._sweep_abort.is_set():
                        break
                    self.sweep["rep"] = rep + 1
                    await self.daq.id_write(line, True)
                    self.sweep["line_state"] = True
                    if await self._sweep_sleep(on_s):
                        break
                    await self.daq.id_write(line, False)
                    self.sweep["line_state"] = False
                    if await self._sweep_sleep(off_s):
                        break
                await self.daq.id_release(line)
                if self._sweep_abort.is_set():
                    break
                self.sweep["phase"] = "gap"
                if await self._sweep_sleep(gap_s):
                    break
            self.sweep["phase"] = "done" if not self._sweep_abort.is_set() else "stopped"
            self.sweep["message"] = (
                "sweep complete" if not self._sweep_abort.is_set()
                else "sweep stopped"
            )
        except Exception as exc:
            self.sweep["phase"] = "error"
            self.sweep["message"] = f"{type(exc).__name__}: {exc}"
            self._event("error", f"valve sweep: {exc}")
        finally:
            with contextlib.suppress(Exception):
                await self.daq.id_release_all()
            self.sweep["running"] = False
            self.sweep["line_state"] = False

    async def _sweep_sleep(self, seconds: float) -> bool:
        """Sleep, returning True immediately if stop is hit."""
        if seconds <= 0:
            return self._sweep_abort.is_set()
        try:
            await asyncio.wait_for(self._sweep_abort.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop_valve_sweep(self) -> None:
        """Stop the sweep and drive every identification line low."""
        self._sweep_abort.set()
        if self.daq is not None:
            with contextlib.suppress(Exception):
                await self.daq.id_release_all()
        self.sweep["running"] = False
        self.sweep["phase"] = "stopped"
        self.sweep["line_state"] = False
        self._event("valve-id", "STOP - all identification lines low")

    def mark_sweep_line(self, valve_id: str = "", note: str = "") -> dict:
        """Bind the line being pulsed right now to a valve. Called when the
        operator sees that valve move."""
        line = self.sweep.get("current_line")
        if not self.sweep.get("running") or not line:
            raise RuntimeError("no line is being pulsed")
        mark = {"line": line, "valve": valve_id, "note": note, "t": time.time()}
        self.sweep.setdefault("marks", []).append(mark)
        who = valve_id or note or "?"
        self._event("valve-id", f"MARK: {line} -> {who}")
        return mark

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
                    f"{mfc_id}: isolation valve '{label}' is closed - "
                    "open it before setting flow above 0"
                )

        result = await dev.set_setpoint_sccm(sccm)   # type: ignore[attr-defined]
        self._event("mfc", f"{mfc_id} setpoint -> {result:.2f} sccm")
        return result

    # -- background fill-pressure regulation ------------------------------- #

    async def _drive_valve_quiet(self, valve_id: str, state: bool) -> None:
        """Drive a valve without emitting an event (used by the fast regulator
        pulsing, which would otherwise flood the event log)."""
        if self.daq is None:
            return
        await self.daq.write_do(valve_id, state)
        self.valve_state[valve_id] = state

    async def start_fill_regulation(
        self, *, valve: str, gauge: str, target_torr: float,
        pulse_on_s: float = 0.1, pulse_off_s: float = 0.3,
        tolerance_frac: float = 0.2,
    ) -> None:
        """Pulse `valve` to hold `gauge` (a snapshot key) at `target_torr`.

        Runs in the background until stop_fill_regulation. Emits a gentle "flag"
        event when the pressure drifts more than tolerance_frac off setpoint, and
        another when it comes back - it never stops the run.
        """
        await self.stop_fill_regulation()
        if valve not in self.valve_state:
            raise KeyError(f"unknown valve '{valve}'")
        self._reg_stop.clear()
        self.regulator = {
            "running": True, "valve": valve, "gauge": gauge,
            "target_torr": target_torr, "tolerance_frac": tolerance_frac,
            "pressure": None, "in_bounds": True, "duty": False,
        }
        self._reg_task = asyncio.create_task(
            self._run_regulation(valve, gauge, target_torr, pulse_on_s,
                                 pulse_off_s, tolerance_frac),
            name="fill-regulation",
        )
        self._event("fill",
                    f"regulating {gauge} to {target_torr:g} Torr via {valve} "
                    f"(flag beyond +/-{tolerance_frac*100:.0f}%)")

    async def stop_fill_regulation(self) -> None:
        if self._reg_task is not None and not self._reg_task.done():
            self._reg_stop.set()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(self._reg_task, timeout=5.0)
            if not self._reg_task.done():
                self._reg_task.cancel()
        self._reg_task = None
        if self.regulator.get("running"):
            self._event("fill", "fill regulation stopped")
        self.regulator = {"running": False}

    async def _run_regulation(self, valve, gauge, target, on_s, off_s, tol) -> None:
        async def nap(dur: float) -> bool:      # returns True if asked to stop
            try:
                await asyncio.wait_for(self._reg_stop.wait(), timeout=dur)
                return True
            except asyncio.TimeoutError:
                return False

        try:
            while not self._reg_stop.is_set():
                p = self.snapshot.get(gauge)
                self.regulator["pressure"] = p
                if isinstance(p, (int, float)) and target > 0:
                    off = abs(p - target) / target
                    inb = off <= tol
                    if inb != self.regulator.get("in_bounds", True):
                        self.regulator["in_bounds"] = inb
                        if not inb:
                            self._event("flag",
                                        f"{gauge} {p:.3g} Torr is {off*100:.0f}% off "
                                        f"setpoint {target:.3g} Torr")
                        else:
                            self._event("fill", f"{gauge} back within +/-{tol*100:.0f}%")
                    if p < target:
                        self.regulator["duty"] = True
                        await self._drive_valve_quiet(valve, True)
                        stop = await nap(on_s)
                        await self._drive_valve_quiet(valve, False)
                        if stop:
                            break
                        if await nap(off_s):
                            break
                        continue
                self.regulator["duty"] = False
                if await nap(off_s):
                    break
        finally:
            with contextlib.suppress(Exception):
                await self._drive_valve_quiet(valve, False)

    # ====================================================================== #
    #  Pre-start sequence
    # ====================================================================== #

    @property
    def prestart(self) -> dict:
        return self._prestart.state

    async def start_prestart(self, params: dict) -> None:
        await self._prestart.start(params)

    async def stop_prestart(self) -> None:
        await self._prestart.stop()

    async def abort_prestart(self) -> None:
        await self._prestart.abort()

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

    async def set_supply_output(self, supply_id: str, on: bool) -> dict[str, Any]:
        dev = self._supply(supply_id)
        setter = getattr(dev, "set_output", None)
        if setter is None:
            raise RuntimeError(
                f"{supply_id} has no output control in this program")
        await setter(bool(on))
        self._event("command",
                    f"{supply_id}: output {'ON' if on else 'OFF'} (operator)")
        return {"id": supply_id, "output_on": bool(on)}

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
        plain loop: its output comes on only when the run's Sample bias field is
        non-zero, and its voltage is set from that field first. `polarity` is
        lead-orientation bookkeeping (+1/-1) - the 2260B is single-quadrant and
        cannot source a negative voltage, so the sign is applied to the LOGGED
        value, never to what is commanded.

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
                    if magnitude <= 0.0:
                        # No bias wanted for this run. Leave it OFF - and say
                        # so, because "the bias supply is dark" should never be
                        # something the operator has to infer.
                        await set_output(False)
                        self._event("recipe",
                                    f"{ps_id}: sample bias is 0 V, output left off")
                        continue
                    await dev.set_voltage(magnitude)
                    await set_output(True)
                    sign = "-" if dev.polarity < 0 else "+"
                    # Read it back rather than trusting the write. This is the
                    # one supply that puts a potential on the sample, so
                    # "commanded" and "actually on" being conflated is not
                    # acceptable - and a silent no-op here is precisely what
                    # was reported on 2026-08-25.
                    confirmed = None
                    try:
                        await dev.read()
                        confirmed = getattr(dev, "output_on", None)
                    except Exception:
                        pass
                    if confirmed is False:
                        self._event("error",
                                    f"{ps_id}: commanded sample bias "
                                    f"{sign}{magnitude:g} V ON but the supply "
                                    f"still reports its output OFF - check the "
                                    f"front panel (protection tripped? output "
                                    f"key?)")
                    else:
                        self._event("recipe",
                                    f"{ps_id}: sample bias {sign}{magnitude:g} V, "
                                    f"output ON")
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
        async with self._run_start_lock:
            self._check_run_start()
            try:
                await self._start_recipe(recipe)
            except BaseException:
                await self.recording.call("stop_run_export")
                raise

    def _check_run_start(self) -> None:
        if not self._running:
            raise RuntimeError("server is stopping or not started")
        if self.recipes.busy:
            raise RuntimeError("a recipe is already running")
        if self.prestart.get("running"):
            raise RuntimeError("pre-start is running - stop it before starting a run "
                               "(both drive the plasma-ground relay)")
        self._start_cancelled = False

    async def _start_recipe(self, recipe: Recipe, params: dict | None = None) -> None:
        # Prepare recording before the recipe task can execute even one step.
        # The admission lock remains held across this off-thread file work.
        started_at = time.time()
        try:
            run_path = await self.recording.call("start_run_export", recipe.name, started_at)
            self._event("recipe", f"run data recording to {run_path.name}")
            if params is not None:
                await self.recording.call("write_run_params", params, recipe)
        except Exception as exc:
            self._event("error", f"could not prepare run recording: {exc}")
        if self._start_cancelled or not self._running:
            await self.recording.call("stop_run_export")
            raise RuntimeError("run start cancelled")
        await self.recipes.start(recipe, started_at=started_at)
        self._event("recipe", f"started '{recipe.name}' ({recipe.cycles} cycles)")

    async def start_ald_run(self, params: dict) -> Recipe:
        """Build and launch an e-beam ALD run from UI parameters."""
        from .control.recipe import build_ald_recipe

        return await self._start_built_run(build_ald_recipe(params), params)

    async def start_cvd_run(self, params: dict) -> Recipe:
        """Build and launch an electron-enhanced CVD run from UI parameters.

        Same plumbing as the ALD run; the difference lives entirely in the
        recipe (continuous beam, no pump B, cycle-anchored gas schedule).
        """
        from .control.recipe import build_cvd_recipe

        return await self._start_built_run(build_cvd_recipe(params), params)

    async def _start_built_run(self, recipe: Recipe, params: dict) -> Recipe:
        async with self._run_start_lock:
            self._check_run_start()
            fields = ("_run_dose_valve", "_run_plasma_switch", "_run_fill_valve",
                      "_run_end_cleanup")
            previous = {field: getattr(self, field) for field in fields}
            old_name = self.logger.run_name
            try:
                return await self._start_built_run_locked(recipe, params)
            except BaseException:
                # The start lock remains held while accepted disk work drains.
                # A cancelled attempt must not arm cleanup for a later file recipe.
                for field, value in previous.items():
                    setattr(self, field, value)
                await self.recording.call("stop_run_export")
                await self.recording.call("set_run_name", old_name)
                raise

    async def _start_built_run_locked(self, recipe: Recipe, params: dict) -> Recipe:
        self._run_dose_valve = params.get("dose_valve", "prec1")
        self._run_plasma_switch = params.get("plasma_switch", "plasma_ground")
        self._run_fill_valve = params.get("fill_valve", "rpm_top")
        self._run_end_cleanup = True     # zero MFCs + close fill valve at run end
        # Name this run before anything opens a file, so the trace, by-cycle,
        # parameter report and ellipsometer sidecar all carry the same prefix. Only
        # remembered once the run is actually starting, which is what makes the
        # auto-increment track real runs rather than abandoned attempts.
        run_name = await self.recording.call("set_run_name", str(params.get("run_name") or ""))
        await self._start_recipe(recipe, params)
        if run_name:
            self._remember_run_name(run_name)
            self._event("recipe", f"run name: {run_name}")
        return recipe

    # -- operator run naming (see RUN_NAME_PATH) ---------------------------- #

    @property
    def last_run_name(self) -> str:
        """Name of the last run that actually started, or "" if there is none."""
        try:
            data = json.loads(RUN_NAME_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return datalog.sanitize_run_name(str(data.get("name") or ""))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not read %s: %s", RUN_NAME_PATH, exc)
        return ""

    def suggest_run_name(self) -> str:
        """What to pre-fill the run-name box with: the last started run's name
        incremented. Purely a suggestion - the operator can type anything."""
        return datalog.next_run_name(self.last_run_name)

    def _remember_run_name(self, name: str) -> None:
        clean = datalog.sanitize_run_name(name)
        if not clean:
            return
        try:
            RUN_NAME_PATH.parent.mkdir(parents=True, exist_ok=True)
            RUN_NAME_PATH.write_text(
                json.dumps({"name": clean, "started_at": time.time()}, indent=2),
                encoding="utf-8")
        except Exception as exc:
            log.warning("could not persist run name: %s", exc)

    async def finish_run(self) -> None:
        """Called by the recipe runner however a run ends (done or aborted).

        Always stops the background fill regulation so the fill valve is not left
        pulsing, and always commands HV off (operator request, 2026-08-21 - a
        run that ends must not leave the plasma supply energised). For an ALD
        run it additionally returns the tool to a quiet state as the operator
        requested: every MFC setpoint to zero and the precursor fill valve
        closed. File recipes get only the fill stop and the HV off.
        """
        with contextlib.suppress(Exception):
            await self.stop_fill_regulation()
        # Above the ALD/CVD gate below on purpose: these apply to EVERY run
        # ending, file recipes included.
        await self.hv_off(reason="run end")
        await self.supplies_output_off(reason="run end")

        if not self._run_end_cleanup:
            await self.recording.call("stop_run_export")
            return
        self._run_end_cleanup = False

        if self._run_fill_valve in self.valve_state:
            with contextlib.suppress(Exception):
                await self.set_valve(self._run_fill_valve, False, reason="run end")
        for mfc_id in list(self.mfcs):
            with contextlib.suppress(Exception):
                await self.set_mfc_setpoint(mfc_id, 0.0)
        self._event("recipe", "run end: MFCs zeroed, fill stopped, "
                              "fill valve closed, HV off")
        await self.recording.call("stop_run_export")

    async def abort_recipe(self) -> None:
        self._start_cancelled = True
        async with self._run_start_lock:
            await self.recipes.abort()
        self._event("recipe", "aborted by operator")

    def _event(self, kind: str, message: str) -> None:
        self.events.append({"t": time.time(), "kind": kind, "message": message})
        log.info("[%s] %s", kind, message)

    # -- operator label overrides ------------------------------------------ #

    def _load_labels(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not read %s: %s", LABELS_PATH, exc)
        return {}

    def _label(self, kind: str, dev_id: str, default: str) -> str:
        return self.label_overrides.get(kind, {}).get(dev_id) or default

    # -- valve state persistence (see VALVE_STATE_PATH) --------------------- #

    def _load_valve_state(self) -> dict[str, bool]:
        try:
            data = json.loads(VALVE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: bool(v) for k, v in data.items()}
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("could not read %s: %s", VALVE_STATE_PATH, exc)
        return {}

    def _save_valve_state(self) -> None:
        try:
            VALVE_STATE_PATH.write_text(
                json.dumps(self.valve_state, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("could not save %s: %s", VALVE_STATE_PATH, exc)

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
            LABELS_PATH.write_text(
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
        self.recording.capture(point, self.cfg.ellipsometer.idle_gap_s)

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
