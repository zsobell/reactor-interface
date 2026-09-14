"""Regulation pulse, warning, recovery and stop behavior through real commands."""
import asyncio
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, wait_for


async def main():
    c = Checker('test_fill_controller')
    async with VirtualReactor() as vr:
        sup = vr.sup
        sup.snapshot['test-pressure'] = 0.0
        await sup.start_fill_regulation(valve='rpm_top', gauge='test-pressure', target_torr=1,
                                        pulse_on_s=0.02, pulse_off_s=0.02)
        c.check('below target opens fill', await wait_for(lambda: vr.daq.do_state.get('rpm_top')))
        c.check('out of bounds warns and keeps running', sup.regulator['running']
                and any(e['kind'] == 'flag' for e in sup.events))
        sup.snapshot['test-pressure'] = 1.0
        c.check('recovery is observed', await wait_for(lambda: sup.regulator.get('in_bounds')))
        await sup.stop_fill_regulation()
        n = len(vr.daq.do_writes)
        await asyncio.sleep(0.06)
        c.check('stop closes fill and ends pulsing', vr.daq.do_state['rpm_top'] is False
                and len(vr.daq.do_writes) == n and not sup.regulator['running'])
        c.check('only fill valve was driven', {key for _,key,_ in vr.daq.do_writes} == {'rpm_top'})
        try:
            await sup.start_fill_regulation(valve='missing', gauge='test-pressure', target_torr=1)
            c.check('unknown valve refused', False)
        except KeyError:
            c.check('unknown valve refused', not sup.regulator['running'])
    return c.summary()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
