"""Isolated ALD beam timing experiment. Never imported by production control.

One owner calls methods in event order. Returned commands go to an adapter;
acknowledgments start timers only after command completion. A monotonic clock
is injected; the model does no I/O, sleeping, or task creation.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable


@dataclass(frozen=True)
class Command:
    run: str
    generation: int
    grounded: bool
    reason: str


@dataclass(frozen=True)
class Deadline:
    run: str
    generation: int
    at: float
    phase: str


class ExposureModel:
    """Characterized ALD exposure without gas scheduling or run-level cleanup.

    The characterization adapter delivers pause at a sample endpoint; pause
    grounds the beam and resume re-strikes with fresh grace. An in-flight
    restrike completes before that pause. ``cancel`` models coroutine cancellation.
    Timer/ack tokens must match this run and its current generation exactly.
    Caller supplies a unique run ID for each instance.
    """

    def __init__(self, run: str, clock: Callable[[], float], *, seconds: float,
                 threshold: float = 0.0005, tick: float = 0.2,
                 pulse: float = 0.1, settle: float = 0.15):
        if not run:
            raise ValueError("run ID must be nonempty and unique")
        if any(not isfinite(v) or v < 0 for v in (seconds, threshold, pulse, settle)):
            raise ValueError("durations and threshold must be finite and nonnegative")
        if not isfinite(tick) or tick <= 0:
            raise ValueError("tick must be finite and positive")
        self.run, self.clock = run, clock
        self.total, self.remaining = seconds, seconds
        self.threshold, self.tick, self.pulse, self.settle = threshold, tick, pulse, settle
        self.phase = "idle"
        self.deadline: Deadline | None = None
        self.pending: Command | None = None
        self.reasons: set[str] = set()
        self.abort_requested = False
        self._generation = 0
        self._observed = clock()
        self._strike_at = self._observed
        self._sample_at = self._observed

    @property
    def exposure(self) -> float:
        return self.total - max(0.0, self.remaining)

    def _now(self) -> float:
        now = self.clock()
        if not isfinite(now) or now < self._observed:
            raise ValueError("event clock must be finite and monotonic")
        self._observed = now
        return now

    def _command(self, grounded: bool, reason: str) -> tuple[Command, ...]:
        self._generation += 1
        self.deadline = None
        self.pending = Command(self.run, self._generation, grounded, reason)
        return (self.pending,)

    def _timer(self, phase: str, delay: float) -> None:
        self.phase = phase
        self._generation += 1
        self.deadline = Deadline(self.run, self._generation, self._now() + delay, phase)

    def start(self) -> tuple[Command, ...]:
        self._now()
        if self.phase != "idle":
            raise RuntimeError("an exposure instance can only start once")
        self.phase = "starting"
        return self._command(False, "beam on")

    def acknowledge(self, command: Command) -> tuple[Command, ...]:
        self._now()
        if command != self.pending:
            return ()
        self.pending = None
        if self.phase == "finishing":
            self.phase = "done"
        elif command.reason in ("beam on", "resumed - beam on"):
            self._strike_at = self.clock()
            return self._next_sample()
        elif command.reason == "reignite pulse":
            self._timer("pulse", self.pulse)
        elif command.reason == "reignite - beam on":
            self._timer("settle", self.settle)
        return ()

    def _finish(self) -> tuple[Command, ...]:
        self.phase = "finishing"
        self.reasons.discard("reignite")
        return self._command(True, "beam off")

    def _next_sample(self) -> tuple[Command, ...]:
        if self.abort_requested or self.remaining <= 0:
            return self._finish()
        if "operator" in self.reasons:
            self.phase = "paused"
            return self._command(True, "paused - beam off")
        self._sample_at = self._now()
        self._timer("sample", min(self.tick, self.remaining))
        return ()

    def timer(self, token: Deadline, current: float | None = None) -> tuple[Command, ...]:
        now = self._now()
        if token != self.deadline or now < token.at:
            return ()
        self.deadline = None
        if token.phase == "pulse":
            self.phase = "restriking"
            return self._command(False, "reignite - beam on")
        if token.phase == "settle":
            return self._next_sample()
        lit = isinstance(current, (int, float)) and abs(current) >= self.threshold
        if lit:
            self.reasons.discard("reignite")
            self.remaining -= now - self._sample_at
        else:
            self.reasons.add("reignite")
            if self._sample_at - self._strike_at >= self.settle:
                self.phase = "grounding"
                return self._command(True, "reignite pulse")
        return self._next_sample()

    def pause(self) -> None:
        self._now()
        if self.phase not in ("idle", "done", "finishing"):
            self.reasons.add("operator")

    def resume(self) -> tuple[Command, ...]:
        self._now()
        self.reasons.discard("operator")
        if self.phase == "paused":
            self.phase = "resuming"
            return self._command(False, "resumed - beam on")
        return ()

    def abort(self) -> tuple[Command, ...]:
        self._now()
        if self.phase in ("idle", "done", "finishing"):
            return ()
        self.abort_requested = True
        self.reasons.discard("operator")
        return self._next_sample() if self.phase == "paused" else ()

    def cancel(self) -> tuple[Command, ...]:
        self._now()
        if self.phase in ("idle", "done", "finishing"):
            return ()
        self.abort_requested = True
        return self._finish()
