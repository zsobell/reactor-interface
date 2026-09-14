"""One owner for run admission, session metadata and recording lifecycle.

The runner owns step execution. The host owns physical commands. This owner
keeps admission closed from file preparation through hardware cleanup and drain.
"""
from __future__ import annotations

import asyncio
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
        self._cancelled = False

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
            old_name = self.recording.run_name
            self.phase = RunPhase.PREPARING
            # File recipes keep display valve IDs but never inherit built-run cleanup.
            self.session = replace(previous, recipe_name=recipe.name, end_cleanup=False)
            try:
                name = ''
                if params is not None:
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
        session = self.session
        try:
            await self.host.cleanup_run(session)
            # Physical cleanup must precede any wait behind accepted disk writes.
            await self.recording.stop_run_export()
        finally:
            self.session = replace(session, end_cleanup=False)
            self.phase = RunPhase.FINISHED
