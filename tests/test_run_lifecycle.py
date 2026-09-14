"""Run admission remains held until recording cleanup completes."""
import asyncio
import threading
from reactor.control.recipe import Recipe, Step
from reactor.control.run_coordinator import RunPhase
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, wait_for


async def main():
    c = Checker('test_run_lifecycle')
    async with VirtualReactor() as vr:
        c.check('new coordinator starts idle', vr.sup.runs.phase == RunPhase.IDLE)
        entered, release = threading.Event(), threading.Event()
        original = vr.sup.logger.stop_run_export
        def delayed_close():
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test did not release export close')
            return original()
        # Start first: opening an export closes any old one internally.
        await vr.sup.start_recipe(Recipe(steps=[Step(op='wait', seconds=0.05)]))
        accepted = vr.sup.runs.session
        c.check('accepted run is executing', vr.sup.runs.phase == RunPhase.EXECUTING)
        vr.sup.logger.stop_run_export = delayed_close
        try:
            c.check('run waits on export closure', await asyncio.to_thread(entered.wait, 2))
            c.check('hardware cleanup preceded disk wait', vr.supplies['hv'].hv_off_calls == 1)
            c.check('admission remains busy while draining', vr.sup.run_in_progress
                    and vr.sup.runs.phase == RunPhase.FINISHING)
            try:
                await vr.sup.start_prestart({})
                c.check('prestart cannot overlap draining', False)
            except RuntimeError:
                c.check('prestart cannot overlap draining', True)
            try:
                await vr.sup.start_recipe(Recipe(steps=[]))
                c.check('next run cannot overlap draining', False)
            except RuntimeError:
                c.check('next run cannot overlap draining', True)
        finally:
            release.set()
        c.check('drained run releases admission', await wait_for(lambda: not vr.sup.run_in_progress))
        c.check('completed lifecycle is explicit', vr.sup.runs.phase == RunPhase.FINISHED)
        c.check('accepted metadata snapshot remains stable', accepted.recipe_name == vr.sup.runs.session.recipe_name)
        await vr.sup.start_recipe(Recipe(steps=[Step(op='wait', seconds=0.01)]))
        c.check('next run succeeds after drain', await wait_for(lambda: not vr.sup.run_in_progress))
    async with VirtualReactor() as vr:
        vr.sup.snapshot['inst.ammeter'] = 0.001
        await vr.sup.start_prestart(dict(valve_delay_s=0, hold_s=0.01))
        c.check('prestart reaches primed handover', await wait_for(lambda: vr.sup.prestart.get('done')))
        await vr.sup.start_ald_run(dict(run_name='Handover', cycles=1))
        c.check('primed sequence hands ownership to run', vr.sup.runs.phase == RunPhase.EXECUTING)
        vr.sup.recipes.pause()
        c.check('pause retains session admission', vr.sup.run_in_progress
                and vr.sup.recipes.progress.state == 'paused')
        vr.sup.recipes.resume()
        await vr.sup.abort_recipe()
        c.check('abort finishes session and consumes cleanup', vr.sup.runs.phase == RunPhase.FINISHED
                and not vr.sup.runs.session.end_cleanup and not vr.sup.run_in_progress)
    return c.summary()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
