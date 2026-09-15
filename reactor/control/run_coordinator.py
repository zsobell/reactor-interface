"""One owner for run admission, session metadata and recording lifecycle.

The runner owns step execution. The host owns physical commands. This owner
keeps admission closed from file preparation through hardware cleanup and drain.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from .recipe import Recipe, RecipeRunner
from .clock import Clock
from ..recording import RecordingService


class RunPhase(str, Enum):
    IDLE = 'idle'
    PREPARING = 'preparing'
    EXECUTING = 'executing'
    FINISHING = 'finishing'
    FINISHED = 'finished'


@dataclass(frozen=True)
class RunSession:
    recipe_name: str = ''
    dose_valve: str | None = 'prec1'
    plasma_switch: str | None = 'plasma_ground'
    fill_valve: str | None = 'rpm_top'
    end_cleanup: bool = False


class RunHost(Protocol):
    @property
    def server_running(self) -> bool: ...
    @property
    def prestart_running(self) -> bool: ...
    def report_event(self, kind: str, message: str) -> None: ...
    def remember_run_name(self, name: str) -> None: ...
    async def cleanup_run(self, session: RunSession) -> None: ...


class RunCoordinator:
    def __init__(self, host: RunHost, runner: RecipeRunner, recording: RecordingService,
                 *, clock: Clock | None = None):
        self.clock = clock or Clock()
        self.host, self.runner, self.recording = host, runner, recording
        self.phase = RunPhase.IDLE
        self.session = RunSession()
        self._start_lock = asyncio.Lock()
        self._edit_lock = asyncio.Lock()
        self._cancelled = False
        self.params = {}
        self.changes = []
        self.started_elapsed = 0.0

    @property
    def in_progress(self) -> bool:
        return self._start_lock.locked() or self.runner.busy

    def _admit(self) -> None:
        if not self.host.server_running:
            raise RuntimeError('server is stopping or not started')
        if self.runner.busy:
            raise RuntimeError('a recipe is already running')
        if self.host.prestart_running:
            raise RuntimeError('pre-start is running - stop it before starting a run '
                               '(both drive the plasma-ground relay)')
        self._cancelled = False

    async def start(self, recipe: Recipe, params: dict | None = None) -> Recipe:
        async with self._start_lock:
            self._admit()  # rejection must leave metadata and events untouched
            previous = self.session
            previous_params, previous_changes = self.params, self.changes
            old_name = self.recording.run_name
            self.phase = RunPhase.PREPARING
            # File recipes keep display valve IDs but never inherit built-run cleanup.
            self.session = replace(previous, recipe_name=recipe.name, end_cleanup=False)
            try:
                name = ''
                if params is not None:
                    self.params, self.changes = dict(params), []
                    self.session = RunSession(recipe.name, params.get('dose_valve', 'prec1'),
                                              params.get('plasma_switch', 'plasma_ground'),
                                              params.get('fill_valve', 'rpm_top'), True)
                    name = await self.recording.set_run_name(str(params.get('run_name') or ''))
                await self._prepare(recipe, params)
                if name:
                    self.host.remember_run_name(name)
                    self.host.report_event('recipe', f'run name: {name}')
                return recipe
            except BaseException:
                self.session = previous
                self.params, self.changes = previous_params, previous_changes
                try:
                    await self.recording.stop_run_export()
                    if params is not None:
                        await self.recording.set_run_name(old_name)
                finally:
                    self.phase = RunPhase.FINISHED
                raise

    async def _prepare(self, recipe: Recipe, params: dict | None) -> None:
        # Retain timestamp origin: accepted start, before recording preparation.
        started_at = self.clock.wall()
        self.started_elapsed = self.clock.elapsed()
        try:
            path = await self.recording.start_run_export(recipe.name, started_at)
            self.host.report_event('recipe', f'run data recording to {path.name}')
            if params is not None:
                await self.recording.write_run_parameters(params, recipe)
        except Exception as exc:
            self.host.report_event('error', f'could not prepare run recording: {exc}')
        if self._cancelled or not self.host.server_running:
            raise RuntimeError('run start cancelled')
        await self.runner.start(recipe, started_at=started_at)
        self.phase = RunPhase.EXECUTING
        self.host.report_event('recipe', f"started '{recipe.name}' ({recipe.cycles} cycles)")

    async def abort(self) -> None:
        self._cancelled = True
        async with self._start_lock:
            await self.runner.abort()
        self.host.report_event('recipe', 'aborted by operator')

    async def finish(self) -> None:
        self.phase = RunPhase.FINISHING
        async with self._edit_lock:
            await self._finish()

    async def _finish(self) -> None:
        session = self.session
        try:
            await self.host.cleanup_run(session)
            # Physical cleanup must precede any wait behind accepted disk writes.
            await self.recording.stop_run_export()
        finally:
            self.session = replace(session, end_cleanup=False)
            self.phase = RunPhase.FINISHED

    async def update_params(self, params: dict) -> dict:
        """Serialize live edits through session ownership, never through abort's lock."""
        async with self._edit_lock:
            if self.phase != RunPhase.EXECUTING or not self.session.end_cleanup:
                raise RuntimeError("no editable built run in progress")
            result, report = await self._update_params(params)
        # Disk completion must not hold the lock that hardware cleanup needs.
        # The report was queued under the lock, before any subsequent close.
        if report is not None:
            try:
                await asyncio.shield(report)
            except Exception as exc:
                self.host.report_event("error", f"could not update run parameters file: {exc}")
        return result

    async def _update_params(self, params: dict) -> dict:
        from .recipe import build_ald_recipe, build_cvd_recipe

        runner = self.runner
        recipe_now = runner.recipe
        if not runner.busy or recipe_now is None:
            raise RuntimeError("no run in progress")

        from .parameters import migrate_params
        params = dict(params)
        expected_start = params.pop("_run_started_at", None)
        if expected_start is not None and expected_start != runner.progress.started_at:
            raise RuntimeError("live edit belongs to a different run")
        params = {**self.params, **migrate_params(params)}
        for key in ("mode", "name", "run_name", "dose_valve", "fill_valve", "plasma_switch",
                    "gauge", "ammeter", "ar_mfc", "ar_valve"):
            if key in self.params and params.get(key) != self.params[key]:
                raise ValueError(f"{key} cannot change during a run")
        if params.get("mode", recipe_now.mode) != recipe_now.mode:
            raise ValueError("mode cannot change during a run")
        for key, default in (("dose_valve", "prec1"), ("fill_valve", "rpm_top"),
                             ("plasma_switch", "plasma_ground"), ("gauge", "gauge.prec1_dose"),
                             ("ammeter", "inst.ammeter"), ("ar_mfc", "ar"), ("ar_valve", "ar_pneumatic")):
            if params.get(key, default) != self.params.get(key, default):
                raise ValueError(f"{key} cannot change during a run")
        changes: list[dict] = []
        for key in sorted(set(params) | set(self.params)):
            old, new = self.params.get(key), params.get(key)
            if key not in params or old == new:
                continue
            changes.append({
                "key": key, "old": old, "new": new,
                "elapsed_s": max(0.0, self.clock.elapsed() - self.started_elapsed),
                "cycle": runner.progress.cycle,
                "t": self.clock.wall(),
            })
        if not changes:
            return {"changed": []}, None

        build = build_cvd_recipe if recipe_now.mode == "cvd" else build_ald_recipe
        fresh = build(params)
        runner.apply_params(recipe_now, fresh)
        self.params = dict(params)
        self.changes.extend(changes)

        # A gas already flowing follows its new number immediately.
        for gs in fresh.gas_schedules:
            dev = self.host.mfcs.get(gs.mfc)
            if dev is None:
                continue
            if (getattr(dev, "commanded_sccm", 0) or 0) > 0:
                with contextlib.suppress(Exception):
                    await self.host.set_mfc_setpoint(gs.mfc, gs.flow_sccm)

        # The background fill regulator holds its own copy of the target and
        # the pulse timings, so it has to be told separately.
        if self.host.regulator.get("running"):
            fill = next((st for st in recipe_now.setup if st.op == "start_fill"), None)
            if fill is not None:
                with contextlib.suppress(Exception):
                    self.host.update_fill_regulation(
                        target_torr=fill.target_torr,
                        pulse_on_s=fill.pulse_on_s,
                        pulse_off_s=fill.pulse_off_s,
                        tolerance_frac=fill.tolerance_frac)

        for ch in changes:
            self.host.report_event("recipe",
                        f"parameter changed mid-run: {ch['key']} "
                        f"{ch['old']} -> {ch['new']} "
                        f"(cycle {ch['cycle']}, {ch['elapsed_s']:.0f}s in)")
        try:
            report = self.recording.queue_run_parameters(self.params, recipe_now, self.changes)
        except Exception as exc:
            self.host.report_event("error", f"could not update run parameters file: {exc}")
            report = None
        return {"changed": [c["key"] for c in changes]}, report
