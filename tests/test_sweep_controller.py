"""Identification sequencing, marking, stop and failure release."""
import asyncio
from reactor.testing.virtual_reactor import VirtualReactor
from reactor.control.sweep import SweepController
from tests._support import Checker, wait_for


class BlockingHost:
    """An adapter whose active write and every release wait forever."""

    identification_available = True

    def __init__(self):
        self.write_entered = asyncio.Event()
        self.never = asyncio.Event()
        self.release_calls = 0
        self.events = []

    async def identify_write(self, line, state):
        self.write_entered.set()
        await self.never.wait()

    async def identify_release(self, line):
        await self.never.wait()

    async def identify_release_all(self):
        self.release_calls += 1
        await self.never.wait()

    def report_event(self, kind, message):
        self.events.append((kind, message))


class ResistantHost(BlockingHost):
    """An in-flight adapter call that only returns when the device responds."""

    async def _wait(self):
        while not self.never.is_set():
            try:
                await self.never.wait()
            except asyncio.CancelledError:
                pass

    async def identify_write(self, line, state):
        self.write_entered.set()
        await self._wait()

    async def identify_release_all(self):
        self.release_calls += 1
        await self._wait()


class ReleaseErrorHost:
    identification_available = True

    def __init__(self):
        self.write_entered = asyncio.Event()
        self.events = []

    async def identify_write(self, line, state):
        self.write_entered.set()

    async def identify_release(self, line):
        pass

    async def identify_release_all(self):
        raise OSError('release failed')

    def report_event(self, kind, message):
        self.events.append((kind, message))


async def main():
    c = Checker('test_sweep_controller')
    async with VirtualReactor() as vr:
        trace = []
        write = vr.daq.id_write
        async def record(line, state):
            trace.append((line, state))
            await write(line, state)
        vr.daq.id_write = record
        await vr.sup.start_valve_sweep(['one', 'two'], reps=1, on_s=0.1, off_s=0.1, gap_s=0)
        c.check('first line observed', await wait_for(lambda: vr.sup.sweep.get('current_line') == 'one'))
        mark = vr.sup.mark_sweep_line('prec1', 'observed')
        c.check('mark binds active line', mark['line'] == 'one' and mark['valve'] == 'prec1')
        c.check('sweep completes', await wait_for(lambda: not vr.sup.sweep_running))
        c.check('line order preserved', trace == [('one', True), ('one', False), ('two', True), ('two', False)])
        c.check('all identification outputs released', not vr.daq.id_state)
        c.check('normal valves untouched', not vr.daq.do_writes)
        await vr.sup.start_valve_sweep(['one', 'two'], reps=10)
        c.check('second sweep starts', await wait_for(lambda: bool(vr.daq.id_state)))
        await vr.sup.stop_valve_sweep()
        c.check('stop releases outputs', not vr.daq.id_state)
        c.check('stop awaits the task before returning', not vr.sup.sweep_running)
        c.check('stopped task exits', await wait_for(lambda: not vr.sup.sweep_running))
        async def fail(line, state):
            raise OSError('injected identification failure')
        vr.daq.id_write = fail
        await vr.sup.start_valve_sweep(['one'])
        c.check('failed task exits', await wait_for(lambda: not vr.sup.sweep_running))
        c.check('failure reported and released', vr.sup.sweep['phase'] == 'error' and not vr.daq.id_state)

    c.section('stalled adapter calls cannot block stop or shutdown')
    for action in ('stop', 'shutdown'):
        host = BlockingHost()
        controller = SweepController(host, stop_grace_s=0.01)
        await controller.start(['one'])
        await host.write_entered.wait()
        sweep_task = controller.task
        await asyncio.wait_for(getattr(controller, action)(), timeout=0.25)
        c.check(f'{action} returns within its configured bound', controller.task is None)
        c.check(f'{action} leaves no live cooperative sweep task', sweep_task.done())
        c.check(f'{action} retries output release', host.release_calls >= 2,
                str(host.release_calls))
        c.check(f'{action} reports unresolved release', controller.state['phase'] == 'error'
                and not controller.state['running']
                and 'not confirmed' in controller.state['message'])
        c.check(f'{action} never claims identification lines are low', not any(
            'all identification lines low' in message for _, message in host.events
        ), str(host.events))

    c.section('release errors remain visible and never claim a confirmed state')
    host = ReleaseErrorHost()
    controller = SweepController(host, stop_grace_s=0.01)
    await controller.start(['one'], reps=1, on_s=0.1, off_s=0.1)
    await host.write_entered.wait()
    await controller.stop()
    c.check('release exception is exposed', controller.state['phase'] == 'error'
            and 'OSError: release failed' in controller.state['message'])
    c.check('release exception emits an error instead of a low claim',
            any(kind == 'error' and 'not confirmed' in message
                for kind, message in host.events)
            and not any('all identification lines low' in message
                        for _, message in host.events), str(host.events))

    c.section('unfinished adapter calls retain sweep ownership')
    host = ResistantHost()
    controller = SweepController(host, stop_grace_s=0.01)
    await controller.start(['old'])
    await host.write_entered.wait()
    sweep_task = controller.task
    try:
        await asyncio.wait_for(controller.stop(), timeout=0.25)
        c.check('bounded stop retains unfinished ownership', controller.running)
        c.check('pending cleanup is reported without a low claim',
                controller.state['phase'] == 'stopping'
                and 'not confirmed' in controller.state['message']
                and not any('all identification lines low' in message
                            for _, message in host.events), str(host.events))
        rejected = False
        try:
            await controller.start(['new'])
        except RuntimeError:
            rejected = True
        c.check('new sweep cannot overlap unfinished adapter calls', rejected)
    finally:
        host.never.set()
        await asyncio.wait_for(sweep_task, timeout=1)
        await wait_for(lambda: not controller._pending, timeout=1)
        await controller.shutdown()
    c.check('ownership is released after adapter calls finish',
            not controller.running and controller.task is None and not controller._pending)

    c.section('cancelled stop retains its release task')
    host = BlockingHost()
    controller = SweepController(host, stop_grace_s=1)
    stop_task = asyncio.create_task(controller.stop())
    await wait_for(lambda: host.release_calls == 1)
    stop_task.cancel()
    await asyncio.gather(stop_task, return_exceptions=True)
    c.check('cancelled stop still owns its in-flight release', controller.running)
    rejected = False
    try:
        await controller.start(['new'])
    except RuntimeError:
        rejected = True
    finally:
        host.never.set()
        await controller.shutdown()
    c.check('cancelled stop cannot leak cleanup into a new sweep', rejected)
    return c.summary()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
