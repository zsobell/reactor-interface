"""Controllers execute with only their declared capabilities."""
import asyncio
from reactor.control.recipe import RecipeRunner, Recipe, Step
from reactor.control.prestart import PrestartController
from tests._support import Checker, wait_for


class Host:
    def __init__(self):
        self.snapshot = {}
        self.commands = []
        self.run_in_progress = False
        self.finished = False

    async def set_valve(self, valve_id, state, *, reason=''):
        self.commands.append((valve_id, state))

    def report_event(self, kind, message):
        pass

    async def finish_run(self):
        self.finished = True


async def main():
    c = Checker('test_controller_contracts')
    host = Host()
    runner = RecipeRunner(host)
    await runner.start(Recipe(steps=[Step(op='dose', valve='dose', seconds=0.01)]))
    c.check('minimal host completes recipe', await wait_for(lambda: not runner.busy))
    c.check('dose command ordering preserved', host.commands == [('dose', True), ('dose', False)])
    c.check('cleanup returns to owner', host.finished)
    pre = PrestartController(host)
    host.run_in_progress = True
    try:
        await pre.start({})
        c.check('public admission query refuses prestart', False)
    except RuntimeError:
        c.check('public admission query refuses prestart', not pre.state['running'])
    c.check('no private lock or recipes object needed', not hasattr(host, '_run_start_lock')
            and not hasattr(host, 'recipes'))
    return c.summary()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
