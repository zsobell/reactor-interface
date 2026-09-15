"""Wall-clock adjustments cannot change exposure, holds or elapsed deadlines."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from reactor.control.clock import Clock
from reactor.control import recipe as recipe_module, prestart as prestart_module
from reactor.control.recipe import RecipeRunner, RecipeProgress, Recipe, Step
from reactor.control.parameters import PrestartParameters
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


class Time:
    def __init__(self):
        self.elapsed = 100.0
        self.wall = 1700000000.0
        self.advances = 0
        self.sources = Clock(elapsed=lambda: self.elapsed, wall=lambda: self.wall)
        self.jump = -10000

    def advance(self, seconds):
        self.advances += 1
        if self.advances > 1000:
            raise AssertionError('fake clock exceeded bounded test iterations')
        self.elapsed += max(seconds, 1e-9)
        self.wall += self.jump
        self.jump *= -1


async def main():
    c = Checker('test_clock_domains')
    t = Time()
    host = SimpleNamespace(snapshot={'inst.ammeter': 0.001}, report_event=lambda *args: None)
    runner = RecipeRunner(host, clock=t.sources)
    runner.progress = RecipeProgress(state='running', phase='cycling', cycle=1, cycles_total=2, clock=t.sources)
    runner._cycle_len = 10
    runner._begin_cycle_clock()
    t.advance(2)
    c.check('cycle progress uses elapsed time', abs(runner.cycle_fraction() - 0.2) < 1e-9)
    runner._cycle_pause('reignite', True)
    t.advance(1)
    runner._cycle_pause('operator', True)
    t.advance(2)
    runner._cycle_pause('reignite', False)
    t.advance(1)
    runner._cycle_pause('operator', False)
    t.advance(2)
    c.check('overlapping pauses subtract once despite wall jumps', abs(runner.cycle_fraction() - 0.4) < 1e-9)
    c.check('ETA uses the same elapsed progress', abs(runner.run_remaining_s() - 16) < 1e-9)
    progress = RecipeProgress(step_started=t.wall, step_elapsed_started=t.elapsed,
                              step_duration=5, started_at=t.wall, clock=t.sources)
    stamp = progress.started_at
    t.advance(2)
    c.check('step countdown is independent of wall timestamps', progress.as_dict()['step_remaining_s'] == 3
            and progress.as_dict()['started_at'] == stamp)

    t = Time()
    trace = []
    async def valve(switch, state, *, reason):
        trace.append((t.elapsed, state))
    host.set_valve = valve
    runner = RecipeRunner(host, clock=t.sources)
    async def sleep(seconds):
        t.advance(seconds)
    with patch.object(runner, '_tick', sleep):
        await runner._electron_beam(Step(op='electron_beam', switch='plasma', seconds=0.3))
    c.check('ALD exposure survives backward wall jumps', len(trace) == 2
            and abs(trace[-1][0] - trace[0][0] - 0.3) < 1e-8)

    t = Time()
    runner = RecipeRunner(host, clock=t.sources)
    polls = 0
    async def watch_sleep(seconds):
        nonlocal polls
        polls += 1
        if polls == 3:
            raise asyncio.CancelledError
        t.advance(seconds)
    with patch.object(runner, '_tick', watch_sleep):
        try:
            await runner._beam_watch(Step(op='beam_start', switch='plasma'))
        except asyncio.CancelledError:
            pass
    c.check('CVD lit and cycle clocks advance together', abs(runner._lit_s - 0.4) < 1e-8
            and abs(runner._cycle_clock - 0.4) < 1e-8)

    for gated in (False, True):
        t = Time()
        runner = RecipeRunner(host, clock=t.sources)
        runner._clock_gated = gated
        polls = 0
        async def dropout_sleep(seconds):
            nonlocal polls
            t.advance(seconds)
            if seconds == recipe_module.BEAM_TICK_S:
                polls += 1
                if polls == 3:
                    raise asyncio.CancelledError
                host.snapshot['inst.ammeter'] = 0 if polls == 1 else 0.001
        with patch.object(runner, '_tick', dropout_sleep), \
                patch.object(recipe_module, 'asyncio', SimpleNamespace(sleep=dropout_sleep, CancelledError=asyncio.CancelledError)):
            try:
                await runner._beam_watch(Step(op='beam_start', switch='plasma',
                                             reignite_pulse_s=0.1, reignite_settle_s=0.15))
            except asyncio.CancelledError:
                pass
        c.check(f'CVD dropout preserves qualified and gated={gated} cycle accounting',
                abs(runner._lit_s - 0.2) < 1e-8
                and abs(runner._cycle_clock - (0.2 if gated else 0.4)) < 1e-8)

    t = Time()
    runner = RecipeRunner(host, clock=t.sources)
    with patch.object(recipe_module, 'asyncio', SimpleNamespace(sleep=sleep)):
        try:
            await runner._wait_for_pressure(Step(op='wait_for_pressure', below_torr=0.01, timeout_s=1))
            c.check('pressure deadline expires', False)
        except TimeoutError:
            c.check('pressure deadline ignores wall jumps', t.elapsed == 101)

    t = Time()
    async def command(*args, **kwargs):
        pass
    for name in ('supplies_output_on', 'set_valve', 'set_mfc_setpoint', 'start_fill_regulation'):
        setattr(host, name, command)
    controller = prestart_module.PrestartController(host, clock=t.sources)
    async def wait_for(coro, *, timeout):
        coro.close()  # simulate the timeout of Event.wait without leaving a coroutine
        t.advance(timeout)
        raise asyncio.TimeoutError
    with patch.object(prestart_module, 'asyncio', SimpleNamespace(wait_for=wait_for, TimeoutError=asyncio.TimeoutError)):
        await controller._run(PrestartParameters.normalize(dict(hold_s=0.39, valve_delay_s=0)))
    c.check('prestart hold credits elapsed time', controller.state['done']
            and abs(controller.state['held_s'] - 0.4) < 1e-8)

    t = Time()
    async with VirtualReactor(clock=t.sources) as vr:
        await vr.sup.start_recipe(Recipe(steps=[]))
        c.check('run start retains wall epoch', vr.sup.recipes.progress.started_at == t.wall)
        c.check('telemetry timestamp remains a wall epoch', vr.sup.state()['t'] == t.wall)
        await vr.sup.abort_recipe()
        await vr.sup.set_valve('prec1', True)
        marks = vr.sup.state()['marks']
        c.check('fresh marks use the injected wall epoch', bool(marks) and marks[-1]['t'] == t.wall)
        t.wall += 901
        c.check('marks expire using the same wall clock', not vr.sup.state()['marks'])
    return c.summary()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
