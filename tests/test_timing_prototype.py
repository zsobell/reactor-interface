"""Deterministic characterization of the actual ALD exposure loop and prototype."""
from __future__ import annotations

import asyncio
from itertools import product
from types import SimpleNamespace
from unittest.mock import patch

from reactor.control import recipe as production
from reactor.control.recipe_model import Step
from reactor.control.clock import Clock as TimeSources
from reactor.testing.timing_prototype import ExposureModel
from tests._support import Checker


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def characterize(currents, *, abort_sleep=None, pause_sleep=None, cancel_sleep=None):
    """Run the production coroutine using local clock/sleep adapters, no devices."""
    clock = Clock()
    trace = []
    sleeps = []
    reasons = []
    readings = iter(currents)
    supervisor = SimpleNamespace(snapshot={"inst.ammeter": 0.001}, report_event=lambda *a: None)

    async def valve(switch, state, *, reason):
        trace.append((round(clock(), 6), state, reason))

    supervisor.set_valve = valve
    runner = production.RecipeRunner(supervisor, clock=TimeSources(elapsed=clock, wall=clock))
    runner.progress.state = "running"
    runner._task = asyncio.current_task()
    original_pause = runner._cycle_pause

    def cycle_pause(reason, active):
        original_pause(reason, active)
        reasons.append(frozenset(runner._pause_reasons))

    runner._cycle_pause = cycle_pause

    class Gate:
        paused = False

        def clear(self):
            self.paused = True

        def set(self):
            self.paused = False

        def is_set(self):
            return not self.paused

        async def wait(self):
            if self.paused:
                clock.now += 1.0
                runner.resume()

    runner._pause = Gate()

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 100:
            raise AssertionError("characterization did not terminate")
        clock.now += max(seconds, 1e-9)
        supervisor.snapshot["inst.ammeter"] = next(readings, 0.001)
        if len(sleeps) == cancel_sleep:
            raise asyncio.CancelledError
        if len(sleeps) == pause_sleep:
            runner.pause()
        if len(sleeps) == abort_sleep:
            runner._abort.set()

    # Replace module references, never mutate asyncio.sleep or global time.time.
    with patch.object(runner, "_tick", sleep), \
            patch.object(production, "asyncio", SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)):
        try:
            await runner._electron_beam(Step(op="electron_beam", switch="plasma", seconds=0.3,
                                            reignite_pulse_s=0.1, reignite_settle_s=0.15))
        except asyncio.CancelledError:
            if cancel_sleep is None:
                raise
    return trace, sleeps, reasons


def prototype(currents, *, abort_sleep=None, pause_sleep=None, cancel_sleep=None):
    clock = Clock()
    model = ExposureModel("trace", clock, seconds=0.3)
    commands = model.start()
    trace = []
    readings = iter(currents)
    sleeps = 0
    for _ in range(150):
        if commands:
            command, = commands
            trace.append((round(clock(), 6), command.grounded, command.reason))
            commands = model.acknowledge(command)
        elif model.phase == "done":
            return trace
        elif model.phase == "paused":
            clock.now += 1.0
            commands = model.resume()
        else:
            token = model.deadline
            assert token is not None, model.phase
            clock.now = max(token.at, clock.now + 1e-9)
            sleeps += 1
            current = next(readings, 0.001)
            if sleeps == cancel_sleep:
                commands = model.cancel()
                continue
            if sleeps == pause_sleep:
                model.pause()
            if sleeps == abort_sleep:
                commands = model.abort()
                assert not commands  # in-flight timer still belongs to this step
            commands = model.timer(token, current)
    raise AssertionError("prototype did not terminate")


async def main():
    c = Checker("test_timing_prototype")
    trace, sleeps, _ = await characterize([0.001])
    c.check("initial grace earns exposure and final sample is clamped",
            trace == [(0.0, False, "beam on"), (0.3, True, "beam off")]
            and abs(sleeps[-1] - 0.1) < 1e-9)
    trace, _, _ = await characterize([0, 0, 0, 0, 0.001])
    c.check("first dead tick gets grace; second triggers pulse and settle",
            trace == [(0.0, False, "beam on"), (0.4, True, "reignite pulse"),
                      (0.5, False, "reignite - beam on"), (0.95, True, "beam off")])
    trace, _, _ = await characterize([0.001], pause_sleep=1)
    c.check("pause credits the lit tick, grounds the beam, then resumes exposure",
            trace == [(0.0, False, "beam on"), (0.2, True, "paused - beam off"),
                      (1.2, False, "resumed - beam on"), (1.3, True, "beam off")])
    trace, _, reasons = await characterize([0, 0, 0, 0, 0.001], pause_sleep=2)
    c.check("operator pause overlapping loss does not interrupt restrike",
            trace == [(0.0, False, "beam on"), (0.4, True, "reignite pulse"),
                      (0.5, False, "reignite - beam on"), (0.65, True, "paused - beam off"),
                      (1.65, False, "resumed - beam on"), (1.95, True, "beam off")]
            and frozenset({"operator", "reignite"}) in reasons)
    trace, _, _ = await characterize([0, 0, 0, 0], abort_sleep=2)
    c.check("graceful abort completes in-flight restrike before grounding",
            trace[-2:] == [(0.5, False, "reignite - beam on"), (0.65, True, "beam off")])
    c.section("prototype compared with production traces")
    comparisons = 0
    for readings in product((0.0, -0.001), repeat=4):
        for controls in ({}, {"pause_sleep": 2}, {"abort_sleep": 2},
                         {"pause_sleep": 2, "abort_sleep": 3},
                         {"cancel_sleep": 2}, {"cancel_sleep": 3}):
            actual, _, _ = await characterize(readings, **controls)
            expected = prototype(readings, **controls)
            assert actual == expected, (readings, controls, actual, expected)
            comparisons += 1
    c.check("command order and times match the real loop", comparisons == 96,
            f"{comparisons} plasma/pause/abort scenarios")

    c.section("explicit clock, acknowledgments and event ownership")
    clock = Clock()
    model = ExposureModel("first", clock, seconds=0.3)
    start, = model.start()
    clock.now = 5.0
    c.check("slow command I/O cannot consume exposure", model.exposure == 0 and model.deadline is None)
    model.acknowledge(start)
    tick = model.deadline
    c.check("initial timer starts at command acknowledgment", tick.at == 5.2)
    c.check("early timer cannot execute", model.timer(tick, 0.001) == () and model.deadline == tick)
    clock.now = tick.at
    model.timer(tick, None)
    c.check("missing reading earns no exposure", model.exposure == 0 and "reignite" in model.reasons)
    model.pause()
    c.check("pause reasons overlap independently", model.reasons == {"operator", "reignite"})
    model.resume()
    c.check("resume leaves plasma-loss pause intact", model.reasons == {"reignite"})
    current_tick = model.deadline
    c.check("duplicate timer is ignored", model.timer(tick, 0.001) == () and model.deadline == current_tick)
    off, = model.cancel()
    c.check("cancellation grounds and invalidates timers", off.grounded and model.deadline is None)
    c.check("late timer and acknowledgment cannot undo cancellation",
            model.timer(current_tick, 0.001) == () and model.acknowledge(start) == ()
            and model.pending == off)
    model.acknowledge(off)
    replacement = ExposureModel("second", clock, seconds=0.3)
    replacement_start, = replacement.start()
    replacement.acknowledge(replacement_start)
    c.check("previous run tokens cannot actuate a successor",
            replacement.timer(current_tick, 0.001) == ()
            and replacement.acknowledge(start) == () and replacement.exposure == 0)
    clock.now += 0.5  # one late tick: same endpoint-sample accounting as production
    final, = replacement.timer(replacement.deadline, 0.001)
    c.check("late lit tick credits measured elapsed, capped for reporting",
            replacement.exposure == 0.3 and final.reason == "beam off")
    clock.now -= 1.0
    try:
        replacement.acknowledge(final)
    except ValueError:
        monotonic_rejected = True
    else:
        monotonic_rejected = False
    c.check("backward elapsed clock fails explicitly", monotonic_rejected)
    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
