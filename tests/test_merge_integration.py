"""Cross-feature contracts introduced by combining control and recording owners."""
import asyncio
import threading

from reactor.control.recipe_model import build_ald_recipe
from reactor.run_report import format_run_params
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, wait_for


async def main():
    c = Checker('test_merge_integration')
    async with VirtualReactor() as vr:
        sup = vr.sup
        await sup.start_ald_run(dict(run_name='EditDrain', cycles=1,
                                     pump_a_s=30, ar_close_delay_s=0))
        entered, release = threading.Event(), threading.Event()
        original = sup.logger.write_run_params

        def stalled_report(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise TimeoutError('test did not release report writer')
            return original(*args, **kwargs)

        sup.logger.write_run_params = stalled_report
        edit = asyncio.create_task(sup.update_run_params({'pump_a_s': 20}))
        abort = None
        try:
            c.check('live edit reaches the recording worker',
                    await asyncio.to_thread(entered.wait, 2))
            abort = asyncio.create_task(sup.abort_recipe())
            c.check('hardware cleanup proceeds while live report is stalled',
                    await wait_for(lambda: vr.supplies['hv'].hv_off_calls > 0, timeout=2))
            c.check('admission stays closed until accepted report drains', sup.run_in_progress)
        finally:
            release.set()
            results = await asyncio.gather(edit, *([abort] if abort else []),
                                           return_exceptions=True)
        c.check('edit and abort both complete', not any(isinstance(x, BaseException) for x in results))
        reports = list(sup.logger.dir.rglob('*_run_params.txt'))
        c.check('accepted edit survives ordered report close', len(reports) == 1 and
                'CHANGES DURING THE RUN' in reports[0].read_text(encoding='utf-8'))

    async with VirtualReactor() as one, VirtualReactor() as two:
        one.mfcs['mfc1'].gas = '2: NH3'
        two.mfcs['mfc1'].gas = '4: H2'
        await one.tick()
        await two.tick()
        frame = one.sup.state()
        recipe = build_ald_recipe({'mfc1_gas_enable': True})
        text_one = format_run_params({}, recipe, gas_names=one.sup.gas_names())
        text_two = format_run_params({}, recipe, gas_names=two.sup.gas_names())
        c.check('report step gas names belong to each instance',
                'set NH3 to' in text_one and 'set H2 to' in text_two)
        one.mfcs['mfc1'].gas = '32: N2'
        await one.tick()
        c.check('published gas metadata stays frozen',
                next(m for m in frame['mfcs'] if m['id'] == 'mfc1')['gas_name'] == 'NH3')
        c.check('instance settings and registries are disjoint',
                one.sup.paths.run_params != two.sup.paths.run_params and
                one.sup.paths.instances != two.sup.paths.instances)
    return c.summary()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
