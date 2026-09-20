"""Ordered recording work on a dedicated thread, independent of control timing.

Only immutable copies of measurements cross this boundary. Hardware and recipe
state stay on the event loop. Lifecycle calls wait for preceding writes; sample
submission never waits for disk. A bounded sample backlog reports overflow as a
recording failure instead of blocking control or growing memory indefinitely.
"""
from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Callable

from .datalog import DataLogger
from .hcpes_recording import (
    HcpesCompletionStatus,
    HcpesCampaignPaths,
    HcpesObservation,
    HcpesPointSummary,
    HcpesSessionWriter,
    write_hcpes_campaign,
)

if TYPE_CHECKING:
    from .control.hcpes_model import PolarityCampaign, ResolvedHcpesPlan
    from .control.recipe import Recipe, RecipeProgress
    from .devices.ellipsometer import EllipsometerPoint


class RecordingService:
    def __init__(self, logger: DataLogger, on_error, *, max_pending: int = 2048, on_capture=None):
        self.logger = logger
        self._on_error = on_error
        self._on_capture = on_capture
        self._close_task = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recording")
        self._loop = None
        self._pending = 0
        self.max_pending = max_pending
        self._queue_error = None
        self._closed = False
        self._status = logger.status()
        self._run_name = logger.run_name
        self._last_ell_recv = 0.0
        # Accessed and mutated only on the single recording worker.  The
        # snapshot is copied for event-loop status reads.
        self._hcpes_writer: HcpesSessionWriter | None = None
        self._hcpes_status: dict[str, Any] = {"active": False}
        logger.on_error = self._error

    def _error(self, message):
        if self._loop is None:
            self._on_error(message)
            self._status = self.logger.status()
        else:
            self._loop.call_soon_threadsafe(self._on_error, message)

    def _invoke(self, fn, args, kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            # Header/open failures and unexpected writer errors must be visible.
            self.logger.report_error("worker", exc)
            raise
        finally:
            self._status = self.logger.status()

    def _schedule(self, fn, args, kwargs):
        if self._closed:
            raise RuntimeError("recording service is closed")
        self._loop = asyncio.get_running_loop()
        return self._executor.submit(self._invoke, fn, args, kwargs)

    async def run(self, fn, *args, **kwargs):
        future = self._schedule(fn, copy.deepcopy(args), copy.deepcopy(kwargs))
        # Cancelling the caller cannot cancel an already accepted close/write.
        wrapped = asyncio.wrap_future(future)
        # A cancelled HTTP caller may never collect a later worker exception.
        wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        return await asyncio.shield(wrapped)

    async def call(self, method: str, *args, **kwargs):
        """Compatibility dispatch for isolated tests; live code uses named methods."""
        if method == "set_run_name":
            return await self.set_run_name(*args, **kwargs)
        return await self.run(getattr(self.logger, method), *args, **kwargs)

    def _submit(self, fn: Callable, *args, **kwargs) -> bool:
        if self._closed:
            return False
        if self._pending >= self.max_pending:
            if self._queue_error is None:
                self._queue_error = "recording backlog full; samples were not recorded"
                self._on_error("Recording failure (queue): " + self._queue_error)
            return False
        self._pending += 1
        try:
            future = self._schedule(fn, copy.deepcopy(args), copy.deepcopy(kwargs))
        except BaseException:
            self._pending -= 1
            raise
        future.add_done_callback(lambda f: self._loop.call_soon_threadsafe(self._completed, f))
        return True

    def submit(self, method: str | Callable, *args, **kwargs) -> bool:
        """Compatibility dispatch for isolated tests; live code uses named methods."""
        if method == "set_run_name":
            fn = self._set_run_name
        else:
            fn = method if callable(method) else getattr(self.logger, method)
        return self._submit(fn, *args, **kwargs)

    # Named operations form the production boundary. Awaited lifecycle methods
    # are ordered after every previously accepted sample by the single worker.

    async def start_manual_log(self, label: str | None = None) -> Path:
        return await self.run(self.logger.start, label)

    async def stop_manual_log(self) -> None:
        await self.run(self.logger.stop)

    def submit_manual_sample(
        self,
        snapshot: dict[str, Any],
        progress: RecipeProgress | None = None,
        *,
        sampled_at: float | None = None,
    ) -> bool:
        return self._submit(
            self.logger.write_sample, snapshot, progress, sampled_at=sampled_at
        )

    @property
    def run_name(self) -> str:
        """Latest worker-owned run name, exposed without leaking the logger."""
        return self._run_name

    def _set_run_name(self, run_name: str) -> str:
        self._run_name = self.logger.set_run_name(run_name)
        return self._run_name

    async def set_run_name(self, run_name: str) -> str:
        return await self.run(self._set_run_name, run_name)

    async def start_run_export(self, recipe_name: str, started_at: float) -> Path:
        return await self.run(self.logger.start_run_export, recipe_name, started_at)

    async def write_run_parameters(
        self, params: dict[str, Any], recipe: Recipe, changes=None
    ) -> Path:
        return await asyncio.shield(self.queue_run_parameters(params, recipe, changes))

    def queue_run_parameters(self, params: dict[str, Any], recipe: Recipe, changes=None):
        """Accept a copied report before releasing control ownership.

        The caller may await its completion outside the edit lock. The ordered
        worker still writes it before a subsequent close, even if that caller
        disconnects or run cleanup begins while disk I/O is pending.
        """
        future = self._schedule(self.logger.write_run_params,
                                copy.deepcopy((params, recipe, changes)), {})
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        return wrapped

    def set_gas_names(self, names: dict[str, str]) -> bool:
        return self._submit(self.logger.set_gas_names, names)

    def submit_event(self, entry: dict[str, Any]) -> bool:
        return self._submit(self.logger.write_event, entry)

    def submit_run_sample(
        self,
        sample: dict[str, Any],
        progress: RecipeProgress | None = None,
        *,
        blank: set[str] | None = None,
    ) -> bool:
        return self._submit(
            self.logger.write_run_sample, sample, progress, blank=blank
        )

    async def stop_run_export(self) -> None:
        await self.run(self.logger.stop_run_export)

    # HCPES sessions use the same ordered worker as every other recorder.  One
    # observation call writes raw and (when accepted) qualified data together.

    def _start_hcpes_session(
        self,
        resolved: ResolvedHcpesPlan,
        session_id: str,
        started_at: float,
    ) -> Path:
        if self._hcpes_writer is not None:
            raise RuntimeError("an HCPES recording session is already active")
        writer = HcpesSessionWriter(
            self.logger.dir, resolved, session_id, started_at)
        self._hcpes_writer = writer
        self._hcpes_status = writer.snapshot()
        return writer.paths.directory

    async def start_hcpes_session(
        self,
        resolved: ResolvedHcpesPlan,
        session_id: str,
        started_at: float,
    ) -> Path:
        return await self.run(
            self._start_hcpes_session, resolved, session_id, started_at)

    def _write_hcpes_observation(self, observation: HcpesObservation) -> None:
        if self._hcpes_writer is None:
            raise RuntimeError("no HCPES recording session is active")
        self._hcpes_writer.write_observation(observation)
        self._hcpes_status = self._hcpes_writer.snapshot()

    def submit_hcpes_observation(self, observation: HcpesObservation) -> bool:
        return self._submit(self._write_hcpes_observation, observation)

    def _write_hcpes_point(self, summary: HcpesPointSummary) -> None:
        if self._hcpes_writer is None:
            raise RuntimeError("no HCPES recording session is active")
        self._hcpes_writer.write_point(summary)
        self._hcpes_status = self._hcpes_writer.snapshot()

    def submit_hcpes_point(self, summary: HcpesPointSummary) -> bool:
        return self._submit(self._write_hcpes_point, summary)

    def _finish_hcpes_session(
        self, status: HcpesCompletionStatus, ended_at: float, error: str,
    ) -> None:
        if self._hcpes_writer is None:
            return
        writer = self._hcpes_writer
        try:
            writer.finish(status, ended_at, error)
        finally:
            self._hcpes_status = writer.snapshot()
            self._hcpes_writer = None

    async def finish_hcpes_session(
        self,
        status: HcpesCompletionStatus,
        ended_at: float,
        error: str = "",
    ) -> None:
        await self.run(self._finish_hcpes_session, status, ended_at, error)

    async def create_hcpes_campaign(
        self,
        campaign: PolarityCampaign,
        session_directories: dict[str, Path],
        created_at: float,
    ) -> HcpesCampaignPaths:
        """Derive one linked signed-axis bundle on the recording worker."""
        resolved_directories = {
            session_id: self.logger.dir / directory
            for session_id, directory in session_directories.items()
        }
        return await self.run(
            write_hcpes_campaign,
            self.logger.dir,
            campaign,
            resolved_directories,
            created_at,
        )

    def _completed(self, future):
        self._pending -= 1
        future.exception()  # failures were reported by _invoke; consume the result

    def capture_ellipsometer_point(
        self, point: EllipsometerPoint, idle_gap_s: float
    ) -> bool:
        """Queue stream rotation and its point together with all other writes."""
        return self._submit(self._capture, point, idle_gap_s)

    def capture(self, point, idle_gap_s):
        """Compatibility alias for ``capture_ellipsometer_point``."""
        return self.capture_ellipsometer_point(point, idle_gap_s)

    def _capture(self, point, gap):
        if (not self.logger.ellipsometer_active or point.index <= 1
                or point.t_recv - self._last_ell_recv > gap):
            path = self.logger.start_ellipsometer_capture(point.t_recv)
            if self._on_capture is not None:
                self._loop.call_soon_threadsafe(self._on_capture, path.name)
        self.logger.write_ellipsometer_point(point)
        self._last_ell_recv = point.t_recv

    def status(self):
        status = copy.deepcopy(self._status)
        status["pending_samples"] = self._pending
        status["hcpes"] = copy.deepcopy(self._hcpes_status)
        if self._queue_error:
            status["errors"]["queue"] = self._queue_error
        return status

    async def drain(self):
        await self.run(lambda: None)

    async def close(self):
        if self._close_task is None:
            future = self._schedule(self._close_all, (), {})
            self._closed = True  # stop accepting samples before awaiting the drain
            self._close_task = asyncio.create_task(self._finish_close(future))
        await asyncio.shield(self._close_task)

    async def _finish_close(self, future):
        try:
            await asyncio.wrap_future(future)
        finally:
            await asyncio.to_thread(self._executor.shutdown, wait=True)

    def _close_all(self) -> None:
        failure = None
        if self._hcpes_writer is not None:
            try:
                self._finish_hcpes_session(
                    "interrupted", time.time(),
                    "recording service closed while active")
            except Exception as exc:
                failure = exc
        try:
            self.logger.close()
        finally:
            if failure is not None:
                raise failure
