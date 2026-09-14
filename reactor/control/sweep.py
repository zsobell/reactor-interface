"""Valve identification owns its task and progress; host owns DAQ commands."""
import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Protocol


class SweepHost(Protocol):
    @property
    def identification_available(self) -> bool: ...
    async def identify_write(self, line: str, state: bool) -> None: ...
    async def identify_release(self, line: str) -> None: ...
    async def identify_release_all(self) -> None: ...
    def report_event(self, kind: str, message: str) -> None: ...


@dataclass(frozen=True)
class _TaskOutcome:
    finished: bool
    cancelled: bool = False
    error: BaseException | None = None


class SweepController:
    def __init__(self, host: SweepHost, *, stop_grace_s: float = 1.0):
        self.host = host
        self.task: asyncio.Task[None] | None = None
        self.abort_event = asyncio.Event()
        self.state = {"running": False}
        self.stop_grace_s = stop_grace_s
        self._stop_lock = asyncio.Lock()
        self._pending: set[asyncio.Task] = set()
        self._termination_unconfirmed = False
        self._termination_detail = ""

    def _finished(self, task: asyncio.Task) -> None:
        self._outcome(task)
        self._pending.discard(task)
        if self.task is task:
            self.task = None
        if (self._termination_unconfirmed and self.abort_event.is_set()
                and not self.running):
            self.state.update(
                running=False,
                phase="error",
                message=self._termination_detail,
            )

    @staticmethod
    def _outcome(task: asyncio.Task) -> _TaskOutcome:
        """Retrieve a terminal task result so cancellation cannot warn later."""
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return _TaskOutcome(finished=True, cancelled=True)
        return _TaskOutcome(finished=True, error=error)

    async def _settle(self, task: asyncio.Task) -> _TaskOutcome:
        """Allow cooperative exit, then cancel without an unbounded await.

        ``asyncio.wait_for(task, timeout)`` is deliberately avoided: after its
        timeout it cancels the task and can then wait forever for a stalled
        ``finally`` block. ``asyncio.wait`` always returns at the deadline.
        """
        if task.done():
            return self._outcome(task)
        done, _ = await asyncio.wait({task}, timeout=self.stop_grace_s)
        if not done:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=self.stop_grace_s)
        if done:
            return self._outcome(task)
        # A cooperative coroutine finishes on this second cancellation. If an
        # adapter suppresses cancellation indefinitely, retain ownership until
        # it exits. Its late write/release must not overlap a new sweep.
        task.cancel()
        self._pending.add(task)
        task.add_done_callback(self._finished)
        return _TaskOutcome(finished=False)

    async def _release_all(self) -> _TaskOutcome:
        if not self.host.identification_available:
            return _TaskOutcome(
                finished=True, error=RuntimeError("DAQ is unavailable")
            )
        release = asyncio.create_task(
            self.host.identify_release_all(), name="valve-sweep-release"
        )
        # The stop request itself can be cancelled while this call is pending.
        # Keep release ownership even when _settle never reaches its timeout.
        self._pending.add(release)
        release.add_done_callback(self._finished)
        return await self._settle(release)

    async def _terminate(self) -> tuple[bool, str]:
        async with self._stop_lock:
            return await self._terminate_locked()

    async def _terminate_locked(self) -> tuple[bool, str]:
        self.abort_event.set()
        # Preserve the immediate release attempt made by operator stop. It is
        # bounded because it may wait behind the same stalled DAQ write.
        await self._release_all()
        task = self.task
        sweep = _TaskOutcome(finished=True)
        if task is not None:
            sweep = await self._settle(task)
        if sweep.finished and (task is None or task.done()):
            self.task = None
        # Cancellation may have interrupted _run's finally release. Retry once
        # after the sweep task has stopped, subject to the same bound.
        released = await self._release_all()
        if self.task is not None and self.task.done():
            self.task = None
        details = []
        if not sweep.finished:
            details.append("sweep task did not stop")
        elif sweep.error is not None:
            details.append(f"sweep task failed: {type(sweep.error).__name__}: {sweep.error}")
        if not released.finished or released.cancelled:
            details.append("output release timed out")
        elif released.error is not None:
            details.append(
                f"output release failed: {type(released.error).__name__}: {released.error}"
            )

        pending = (self.task is not None and not self.task.done()) or any(
            not item.done() for item in self._pending
        )
        if pending and not any("did not stop" in detail for detail in details):
            details.append("identification cleanup is still running")

        clean = not details
        if clean:
            self._termination_unconfirmed = False
            self._termination_detail = ""
            self.state.update(
                running=False,
                phase="stopped",
                line_state=False,
                message="sweep stopped; identification outputs released",
            )
            return True, ""

        detail = "; ".join(details) + "; output state is not confirmed"
        self._termination_unconfirmed = True
        self._termination_detail = detail
        self.state.update(
            running=pending,
            phase="stopping" if pending else "error",
            message=detail,
        )
        return False, detail

    async def shutdown(self) -> None:
        clean, detail = await self._terminate()
        if not clean:
            self.host.report_event("error", f"valve sweep shutdown: {detail}")

    @property
    def running(self) -> bool:
        return (self._stop_lock.locked()
                or (self.task is not None and not self.task.done())
                or any(not task.done() for task in self._pending))

    async def start(
        self, lines: list[str], *, reps: int = 3,
        on_s: float = 1.0, off_s: float = 1.0, gap_s: float = 3.0,
        start_index: int = 0,
    ) -> None:
        """Pulse each line in turn so the operator can see which valve moves.

        Per line: (on, off) x reps, then a gap, then the next line. The operator
        watches the box and hits stop if anything is wrong.
        """
        if self.running:
            raise RuntimeError("a valve-identification sweep is already running")
        if not self.host.identification_available:
            raise RuntimeError("DAQ not started")

        lines = [ln for ln in lines if ln]
        if not lines:
            raise ValueError("no lines to sweep")

        reps = max(1, min(10, int(reps)))
        on_s = max(0.1, min(5.0, float(on_s)))
        off_s = max(0.1, min(5.0, float(off_s)))
        gap_s = max(0.0, min(30.0, float(gap_s)))
        start_index = max(0, min(len(lines) - 1, int(start_index)))

        self.abort_event.clear()
        self.state = {
            "running": True, "lines": lines, "total": len(lines),
            "index": start_index, "current_line": None, "line_state": False,
            "rep": 0, "reps": reps, "phase": "starting",
            "on_s": on_s, "off_s": off_s, "gap_s": gap_s,
            "marks": [], "started_at": time.time(), "message": "",
        }
        self.task = asyncio.create_task(
            self._run(lines, reps, on_s, off_s, gap_s, start_index),
            name="valve-sweep",
        )
        self.host.report_event("valve-id",
                    f"sweep started: {len(lines)} lines, {reps}x "
                    f"{on_s:g}s on / {off_s:g}s off, {gap_s:g}s gap")

    async def _run(self, lines, reps, on_s, off_s, gap_s, start_index) -> None:
        try:
            for idx in range(start_index, len(lines)):
                if self.abort_event.is_set():
                    break
                line = lines[idx]
                self.state.update(index=idx, current_line=line, phase="pulse", rep=0)
                self.host.report_event("valve-id", f"pulsing {line}  ({idx + 1}/{len(lines)})")
                for rep in range(reps):
                    if self.abort_event.is_set():
                        break
                    self.state["rep"] = rep + 1
                    await self.host.identify_write(line, True)
                    self.state["line_state"] = True
                    if await self._sleep(on_s):
                        break
                    await self.host.identify_write(line, False)
                    self.state["line_state"] = False
                    if await self._sleep(off_s):
                        break
                await self.host.identify_release(line)
                if self.abort_event.is_set():
                    break
                self.state["phase"] = "gap"
                if await self._sleep(gap_s):
                    break
            self.state["phase"] = "done" if not self.abort_event.is_set() else "stopped"
            self.state["message"] = (
                "sweep complete" if not self.abort_event.is_set()
                else "sweep stopped"
            )
        except Exception as exc:
            self.state["phase"] = "error"
            self.state["message"] = f"{type(exc).__name__}: {exc}"
            self.host.report_event("error", f"valve sweep: {exc}")
        finally:
            with contextlib.suppress(Exception):
                await self.host.identify_release_all()
            self.state["running"] = False
            self.state["line_state"] = False

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, returning True immediately if stop is hit."""
        if seconds <= 0:
            return self.abort_event.is_set()
        try:
            await asyncio.wait_for(self.abort_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        """Stop the sweep and drive every identification line low."""
        clean, detail = await self._terminate()
        if clean:
            self.host.report_event("valve-id", "STOP - all identification lines low")
        else:
            self.host.report_event("error", f"valve sweep STOP: {detail}")

    def mark(self, valve_id: str = "", note: str = "") -> dict:
        """Bind the line being pulsed right now to a valve. Called when the
        operator sees that valve move."""
        line = self.state.get("current_line")
        if not self.state.get("running") or not line:
            raise RuntimeError("no line is being pulsed")
        mark = {"line": line, "valve": valve_id, "note": note, "t": time.time()}
        self.state.setdefault("marks", []).append(mark)
        who = valve_id or note or "?"
        self.host.report_event("valve-id", f"MARK: {line} -> {who}")
        return mark
