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

from .datalog import DataLogger


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
        self._last_ell_recv = 0.0
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
        return await self.run(getattr(self.logger, method), *args, **kwargs)

    def submit(self, method: str, *args, **kwargs) -> bool:
        if self._closed:
            return False
        if self._pending >= self.max_pending:
            if self._queue_error is None:
                self._queue_error = "recording backlog full; samples were not recorded"
                self._on_error("Recording failure (queue): " + self._queue_error)
            return False
        fn = method if callable(method) else getattr(self.logger, method)
        self._pending += 1
        try:
            future = self._schedule(fn, copy.deepcopy(args), copy.deepcopy(kwargs))
        except BaseException:
            self._pending -= 1
            raise
        future.add_done_callback(lambda f: self._loop.call_soon_threadsafe(self._completed, f))
        return True

    def _completed(self, future):
        self._pending -= 1
        future.exception()  # failures were reported by _invoke; consume the result

    def capture(self, point, idle_gap_s):
        """Queue stream rotation and its point together with all other writes."""
        return self.submit(self._capture, point, idle_gap_s)

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
        if self._queue_error:
            status["errors"]["queue"] = self._queue_error
        return status

    async def drain(self):
        await self.run(lambda: None)

    async def close(self):
        if self._close_task is None:
            future = self._schedule(self.logger.close, (), {})
            self._closed = True  # stop accepting samples before awaiting the drain
            self._close_task = asyncio.create_task(self._finish_close(future))
        await asyncio.shield(self._close_task)

    async def _finish_close(self, future):
        try:
            await asyncio.wrap_future(future)
        finally:
            await asyncio.to_thread(self._executor.shutdown, wait=True)
