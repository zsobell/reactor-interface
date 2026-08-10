"""The Supervisor: single owner of reactor state and the only path to hardware.

The original program kept system state in LabVIEW functional global variables,
which any subVI could modify from anywhere. Here there is exactly one Supervisor.
It owns the devices, the recipe runner and the logger. Every command is a method
on this class; each one does exactly what it is told and nothing else - there are
no software interlocks, limits, or automatic actions.

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
from .datalog import DataLogger
from .devices.base import Device, Reading
from .devices.ellipsometer import EllipsometerClient, EllipsometerPoint
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
        self.logger = DataLogger(cfg)

        self.snapshot: dict[str, Any] = {}
        self.readings: dict[str, Reading] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SAMPLES)
        self.events: deque[dict[str, Any]] = deque(maxlen=250)

        self.daq: NiDaqBackend | None = None
        self.mfcs: dict[str, Device] = {}
        self.instruments: dict[str, Device] = {}
        # Best-effort restore of last-commanded state (see VALVE_STATE_PATH) -
        # falls back to False for any valve it has no record of.
        _persisted_valves = self._load_valve_state()
        self.valve_state: dict[str, bool] = {
            v.id: _persisted_valves.get(v.id, False) for v in cfg.valves
        }

        self._plan = DaqPlan()
        self._loop_task: asyncio.Task | None = None
        self._current_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._last_cycle = 0.0

        #: operator label overrides, {kind: {id: label}}; persisted to LABELS_PATH
        self.label_overrides: dict[str, dict[str, str]] = self._load_labels()
        self._cycle_count = 0
        self._running = False
        self._subscribers: set[asyncio.Queue] = set()

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

        # Operator pre-start sequence (see start_prestart)
        self._prestart_task: asyncio.Task | None = None
        self._prestart_stop = asyncio.Event()
        self.prestart: dict[str, Any] = {"running": False}

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
        self._ell_last_recv = 0.0
        self._ell_last_index = 0

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

        self._running = True
        self._loop_task = asyncio.create_task(self._control_loop(), name="control-loop")
        self._current_task = asyncio.create_task(
            self._current_loop(), name="current-loop")
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="instrument-reconnect")

        if self.ellipsometer is not None:
            self.ellipsometer.start()
            self._event("startup",
                        f"ellipsometer subscriber -> {self.cfg.ellipsometer.host}:"
                        f"{self.cfg.ellipsometer.port} (read-only)")

    async def stop(self) -> None:
        """Stop polling and disconnect. Does not actuate anything.

        Previously documented here as "closing DAQmx output tasks resets those
        lines low" - CONTRADICTED by observation 2026-08: the Ar pneumatic
        isolation valve stayed physically open across a server restart, so at
        least that line (cDAQ2Mod2, NI 9472) does not reset on task close. This
        program never commands a reset either way - stopping does not write to
        any line - so whatever happens is purely the DAQ hardware's behaviour,
        and per the above it should not be assumed to be "goes low". That is why
        valve state is now persisted (VALVE_STATE_PATH) and restored at startup
        instead of defaulting every valve to closed.
        """
        self._running = False

        with contextlib.suppress(Exception):
            await self.recipes.abort()
        with contextlib.suppress(Exception):
            await self.stop_fill_regulation()
        self._sweep_abort.set()

        for task in (self._loop_task, self._current_task, self._reconnect_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self.ellipsometer is not None:
            with contextlib.suppress(Exception):
                await self.ellipsometer.stop()

        for dev in list(self.mfcs.values()) + list(self.instruments.values()):
            with contextlib.suppress(Exception):
                await dev.disconnect()
        if self.daq:
            with contextlib.suppress(Exception):
                await self.daq.close()

        self.logger.close()

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
        configured setup, nothing more. It never actuates anything. Only bench
        instruments are retried here; MFCs are left alone deliberately, since an
        MFC zeroes its setpoint when its Modbus master reconnects.
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

    async def _cycle(self) -> None:
        """Slow loop: DAQ analog inputs only, at site.loop_hz (the NI 9211
        thermocouples cannot be read much faster). Updates the shared snapshot
        in place and writes the data log. MFCs and the sample-current instrument
        are independent HTTP/VISA devices with no such limit, so they are polled
        on the faster _current_loop below instead."""
        cfg = self.cfg
        readings: list[Reading] = []

        if self.daq is not None:
            readings.extend(await self.daq.read_ai())

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
        self.logger.write_sample(self.snapshot, self.recipes.progress)

    async def _current_loop(self) -> None:
        """Fast loop: poll the bench instruments (the DMM6500 sample-current)
        and the MFCs (HTTP reads, no DAQ involved) at site.current_hz and
        publish telemetry. This is the plasma diagnostic, so it runs finer than
        the thermocouple-limited slow loop and drives the chart's current
        resolution, the MFC flow chart, and the electron-beam reignite cadence."""
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

    async def _current_cycle(self) -> None:
        cfg = self.cfg
        devices = list(self.instruments.values()) + list(self.mfcs.values())
        if devices:
            results = await asyncio.gather(
                *(d.read() for d in devices),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, list):
                    for rd in r:
                        self.readings[rd.key] = rd
                        self.snapshot[rd.key] = rd.value if rd.ok else None
                elif isinstance(r, BaseException):
                    self._event("error", f"device read: {type(r).__name__}: {r}")

        snap = self.snapshot
        self._last_cycle = time.time()
        self._cycle_count += 1

        sample = {
            "t": self._last_cycle,
            "pressure": snap.get("pressure"),
            "stage_temp": snap.get("stage.temp"),
            "dosing": bool(self.valve_state.get(self._run_dose_valve)),
            "beam_on": not self.valve_state.get(self._run_plasma_switch, True),
            **{f"gauge_{g.id}": snap.get(f"gauge.{g.id}") for g in cfg.gauges},
            **{f"mfc_{m}": snap.get(f"mfc.{m}.flow") for m in self.mfcs},
            **{f"inst_{i}": snap.get(f"inst.{i}") for i in self.instruments},
            **{f"aux_{a.id}": snap.get(f"aux.{a.id}") for a in cfg.aux_inputs},
        }
        # Fractional cycle number + paused flag, computed now (at log time) so a
        # sample taken mid-wall-step still gets an accurate position. Stored on
        # progress so the logger's by-cycle export and the telemetry share them.
        prog = self.recipes.progress
        prog.cycle_fraction = self.recipes.cycle_fraction()
        prog.paused = self.recipes.cycle_paused

        self.history.append(sample)
        self.logger.write_run_sample(sample, prog)
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

    async def start_prestart(self, params: dict) -> None:
        """Bring the tool up to a struck, primed, beam-off state.

        Exactly the sequence the operator specified (2026-08-05):
        open the Ar isolation valve, wait, flow Ar, start the precursor fill
        pulse, run the reignite protocol until sample current appears, hold
        that current, then energise plasma ground so the beam ends OFF.

        The strike retries indefinitely - by explicit instruction there is no
        timeout and no attempt limit. Stopping is the operator's call, via
        stop_prestart. However this ends - finished, stopped, or crashed - the
        beam is grounded on the way out; Ar and the fill regulation are left
        running, which is the same state a completed sequence leaves behind.
        """
        if self.prestart.get("running"):
            raise RuntimeError("pre-start is already running")
        if self.recipes.busy:
            raise RuntimeError("a run is in progress - abort it first")

        self._prestart_stop.clear()
        self.prestart = {
            "running": True, "phase": "starting", "lit": False,
            "current": None, "held_s": 0.0, "hold_target_s": 0.0, "strikes": 0,
        }
        self._prestart_task = asyncio.create_task(
            self._run_prestart(params), name="prestart")
        self._event("recipe", "pre-start sequence started")

    async def stop_prestart(self) -> None:
        if self._prestart_task is not None and not self._prestart_task.done():
            self._prestart_stop.set()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(self._prestart_task, timeout=10.0)
            if not self._prestart_task.done():
                self._prestart_task.cancel()
        self._prestart_task = None
        if self.prestart.get("running"):
            self._event("recipe", "pre-start stopped by operator")
        # _run_prestart's own finally already set running=False/done/phase/
        # strikes in place by the time the wait above returns - merge, don't
        # replace, or an operator-initiated stop always reports back as bare
        # "idle" and throws away the phase/strike-count info the UI shows.
        self.prestart["running"] = False

    async def _run_prestart(self, p: dict) -> None:
        g = lambda k, d: p.get(k, d)  # noqa: E731
        ar_valve = g("ar_valve", "ar_pneumatic")
        ar_mfc = g("ar_mfc", "ar")
        ar_sccm = float(g("ar_sccm", 4.0))
        valve_delay_s = float(g("valve_delay_s", 1.0))
        hold_s = float(g("hold_s", 5.0))
        switch = g("plasma_switch", "plasma_ground")
        ammeter = g("ammeter", "inst.ammeter")
        min_current = float(g("min_current_a", 5.0e-4))
        pulse_s = float(g("reignite_pulse_s", 0.10))
        settle_s = float(g("reignite_settle_s", 0.15))

        async def nap(dur: float) -> bool:      # True if asked to stop
            try:
                await asyncio.wait_for(self._prestart_stop.wait(), timeout=dur)
                return True
            except asyncio.TimeoutError:
                return False

        def phase(name: str) -> None:
            self.prestart["phase"] = name

        struck = False
        try:
            self.prestart["hold_target_s"] = hold_s

            phase("opening Ar isolation valve")
            await self.set_valve(ar_valve, True, reason="pre-start")
            if await nap(valve_delay_s):
                return

            phase(f"Ar to {ar_sccm:g} sccm")
            await self.set_mfc_setpoint(ar_mfc, ar_sccm)

            phase("starting precursor fill pulse")
            await self.start_fill_regulation(
                valve=g("fill_valve", "rpm_top"),
                gauge=g("gauge", "gauge.prec1_dose"),
                target_torr=float(g("dose_pressure_torr", 0.02)),
                pulse_on_s=float(g("fill_pulse_on_s", 0.10)),
                pulse_off_s=float(g("fill_pulse_off_s", 0.30)),
                tolerance_frac=float(g("tolerance_frac", 0.20)),
            )

            # Strike, then hold. A drop-out during the hold sends it straight
            # back to striking, and the held time restarts - the point of the
            # hold is a continuous stretch of current, not a total.
            phase("striking plasma")
            await self.set_valve(switch, False, reason="pre-start - beam on")
            if await nap(settle_s):
                return

            held = 0.0
            while not self._prestart_stop.is_set():
                t0 = time.time()
                if await nap(0.2):
                    return
                dt = time.time() - t0
                cur = self.snapshot.get(ammeter)
                lit = isinstance(cur, (int, float)) and abs(cur) >= min_current
                self.prestart["current"] = (
                    float(cur) if isinstance(cur, (int, float)) else None)
                self.prestart["lit"] = lit

                if not lit:
                    if held > 0.0:
                        self._event("flag", "pre-start: plasma dropped out, restriking")
                    held = 0.0
                    self.prestart["held_s"] = 0.0
                    phase("striking plasma")
                    self.prestart["strikes"] = self.prestart.get("strikes", 0) + 1
                    # Same restrike protocol the run uses.
                    await self.set_valve(switch, True, reason="pre-start reignite pulse")
                    if await nap(pulse_s):
                        return
                    await self.set_valve(switch, False, reason="pre-start reignite - beam on")
                    if await nap(settle_s):
                        return
                    continue

                held += dt
                self.prestart["held_s"] = held
                phase(f"holding current ({held:.1f}/{hold_s:g} s)")
                if held >= hold_s:
                    struck = True
                    break

            if struck:
                phase("done - beam grounded, Ar and fill running")
                self._event("recipe",
                            "pre-start complete: plasma struck and held "
                            f"{hold_s:g} s, beam grounded")
        except Exception as exc:
            self.prestart["phase"] = f"error: {exc}"
            self._event("error", f"pre-start failed: {exc}")
        finally:
            # The sequence's declared end state is beam OFF, and that applies
            # however it ends - including an operator stop mid-strike.
            with contextlib.suppress(Exception):
                await self.set_valve(switch, True, reason="pre-start end - beam off")
            self.prestart["running"] = False
            self.prestart["done"] = struck

    async def start_recipe(self, recipe: Recipe) -> None:
        await self.recipes.start(recipe)
        self._event("recipe", f"started '{recipe.name}' ({recipe.cycles} cycles)")
        # Automatic server-side trace of this run - every recipe, not just
        # ALD/CVD, and independent of the operator's own Data Logging toggle.
        # Keyed on the run's own started_at so the filename correlates with
        # what the browser would otherwise have downloaded; that download
        # stays too (this is a redundant copy, not a replacement - it's the
        # one that survives a closed browser).
        try:
            run_path = self.logger.start_run_export(
                recipe.name, self.recipes.progress.started_at or time.time())
            self._event("recipe", f"run data recording to {run_path.name}")
        except Exception as exc:
            self._event("error", f"could not start run data export: {exc}")

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
        if self.prestart.get("running"):
            raise RuntimeError(
                "pre-start is running - stop it before starting a run "
                "(both drive the plasma-ground relay)"
            )
        self._run_dose_valve = params.get("dose_valve", "prec1")
        self._run_plasma_switch = params.get("plasma_switch", "plasma_ground")
        self._run_fill_valve = params.get("fill_valve", "rpm_top")
        self._run_end_cleanup = True     # zero MFCs + close fill valve at run end
        await self.start_recipe(recipe)
        try:
            snap_path = self.logger.write_ald_snapshot(params, recipe)
            self._event("recipe", f"run parameters recorded: {snap_path.name}")
        except Exception as exc:
            self._event("error", f"could not record run parameters: {exc}")
        return recipe

    async def finish_run(self) -> None:
        """Called by the recipe runner however a run ends (done or aborted).

        Always stops the background fill regulation so the fill valve is not left
        pulsing. For an ALD run it additionally returns the tool to a quiet state
        as the operator requested: every MFC setpoint to zero and the precursor
        fill valve closed. File recipes get only the fill-regulation stop.
        """
        with contextlib.suppress(Exception):
            await self.stop_fill_regulation()
        with contextlib.suppress(Exception):
            self.logger.stop_run_export()

        if not self._run_end_cleanup:
            return
        self._run_end_cleanup = False

        if self._run_fill_valve in self.valve_state:
            with contextlib.suppress(Exception):
                await self.set_valve(self._run_fill_valve, False, reason="run end")
        for mfc_id in list(self.mfcs):
            with contextlib.suppress(Exception):
                await self.set_mfc_setpoint(mfc_id, 0.0)
        self._event("recipe", "run end: MFCs zeroed, fill stopped, fill valve closed")

    async def abort_recipe(self) -> None:
        await self.recipes.abort()
        self._event("recipe", "aborted by operator")

    # ====================================================================== #
    #  Telemetry out
    # ====================================================================== #

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
        gap = self.cfg.ellipsometer.idle_gap_s
        if (not self.logger.ellipsometer_active
                or point.index <= 1
                or (point.t_recv - self._ell_last_recv) > gap):
            path = self.logger.start_ellipsometer_capture(point.t_recv)
            self._event("ellipsometer", f"acquisition start -> {path.name}")
        self.logger.write_ellipsometer_point(point)
        self._ell_last_recv = point.t_recv
        self._ell_last_index = point.index

    def _on_ellipsometer_state(self, connected: bool, detail: str) -> None:
        self._event("ellipsometer",
                    "stream connected" if connected
                    else f"stream disconnected ({detail})")

    def _ellipsometer_state(self) -> dict[str, Any]:
        cfg = self.cfg.ellipsometer
        st: dict[str, Any] = {
            "enabled": cfg.enabled, "label": cfg.label,
            "host": cfg.host, "port": cfg.port,
        }
        if self.ellipsometer is not None:
            st.update(self.ellipsometer.status())
        return st

    def state(self) -> dict[str, Any]:
        return {
            "t": time.time(),
            "site": self.cfg.site.name,
            "cycle_count": self._cycle_count,
            "loop_hz": self.cfg.site.loop_hz,
            "snapshot": self.snapshot,
            "readings": {k: r.as_dict() for k, r in self.readings.items()},
            "daq": {
                "configured": bool(self.daq and self.daq.input_count),
                "inputs": self.daq.input_count if self.daq else 0,
                "error": self.daq.last_error if self.daq else "not started",
            },
            "stage_temp": {
                "enabled": self.cfg.stage_temp.enabled,
                "label": self.cfg.stage_temp.label,
                "value": self.snapshot.get("stage.temp"),
                "unit": self.cfg.stage_temp.unit,
            },
            "aux": [
                {
                    "id": a.id,
                    "label": a.label or a.id,
                    "value": self.snapshot.get(f"aux.{a.id}"),
                    "volts": self.snapshot.get(f"aux.{a.id}.volts"),
                    "unit": a.unit,
                }
                for a in self.cfg.aux_inputs
            ],
            "gauges": [
                {
                    "id": g.id,
                    "label": self._label("gauge", g.id, g.label or g.id),
                    "value": self.snapshot.get(f"gauge.{g.id}"),
                    "volts": self.snapshot.get(f"gauge.{g.id}.volts"),
                    "unit": g.unit,
                    "channel": g.channel,
                }
                for g in self.cfg.gauges
            ],
            "valve_banks": [
                {"id": b.id, "label": b.label or b.id, "note": b.note}
                for b in self.cfg.valve_banks
            ],
            "valves": [
                {
                    "id": v.id,
                    "label": self._label("valve", v.id, v.label or v.id),
                    "kind": v.kind,
                    "bank": v.bank,
                    "line": v.line,
                    "identified": v.identified,
                    "open": self.valve_state.get(v.id, False),
                }
                for v in self.cfg.valves
            ],
            "mfcs": [
                {**st, "label": self._label("mfc", mid, st.get("label") or mid),
                 "isolation_valve": next(
                     (m.isolation_valve for m in self.cfg.mfcs if m.id == mid), None)}
                for mid, st in ((mid, d.status()) for mid, d in self.mfcs.items())
            ],
            "instruments": [i.status() for i in self.instruments.values()],
            "regulator": self.regulator,
            "prestart": self.prestart,
            "marks": [m for m in self.marks if time.time() - m["t"] <= 900][-500:],
            "run_valves": {"dose": self._run_dose_valve,
                           "plasma": self._run_plasma_switch},
            "valve_id": {
                **self.sweep,
                "groups": DO_LINE_GROUPS,
                "unidentified_valves": [
                    {"id": v.id, "label": v.label or v.id, "bank": v.bank}
                    for v in self.cfg.valves if not v.identified
                ],
            },
            "recipe": self.recipes.progress.as_dict(),
            "logging": self.logger.status(),
            "ellipsometer": self._ellipsometer_state(),
            "events": list(self.events)[-40:],
        }

    def trend(self, limit: int = 1800) -> list[dict[str, Any]]:
        return list(self.history)[-limit:]

    # -- websocket fan-out -------------------------------------------------- #

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _publish(self) -> None:
        if not self._subscribers:
            return
        payload = self.state()
        for q in list(self._subscribers):
            if q.full():                       # slow client: drop the old frame
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)
