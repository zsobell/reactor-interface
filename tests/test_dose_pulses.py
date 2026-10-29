"""Repeated precursor doses keep the valve closed between pulses and before pump A."""
import asyncio

from reactor.control.recipe import RecipeRunner
from reactor.control.recipe_model import build_ald_recipe, build_cvd_recipe
from tests._support import Checker


class Host:
    def __init__(self):
        self.writes = []
        self.state = False

    async def set_valve(self, valve, state, *, reason):
        self.state = state
        self.writes.append((valve, state, reason))


async def main():
    c = Checker("test_dose_pulses")
    for builder in (build_ald_recipe, build_cvd_recipe):
        default = builder({"dose_s": 0.01, "pump_a_s": 0.02})
        c.check("default remains one pulse", default.steps[0].dose_pulses == 1)
        recipe = builder({"dose_s": 0.01, "dose_pulses": 3,
                          "dose_pulse_gap_s": 0.02, "pump_a_s": 0.02})
        dose = recipe.steps[0]
        c.check("duration includes pulses and only inter-pulse waits",
                abs(recipe.cycle_seconds() - default.cycle_seconds() - 0.06) < 1e-9)
        host = Host()
        runner = RecipeRunner(host)
        waits = []
        async def observe_sleep(seconds, **_kwargs):
            waits.append((seconds, host.state))
        runner._sleep = observe_sleep
        await runner._exec(dose)
        states = [w[1] for w in host.writes]
        c.check("dose toggles exactly three times", states == [True, False] * 3,
                str(states))
        c.check("requested waits run only with valve closed",
                waits == [(0.01, True), (0.02, False), (0.01, True),
                          (0.02, False), (0.01, True)], str(waits))
        c.check("dose exits closed before pump A", host.writes[-1][1] is False)
        zero = builder({"dose_s": 0, "dose_pulses": 3,
                        "dose_pulse_gap_s": 0.02, "pump_a_s": 0.02}).steps[0]
        c.check("zero-time dose has no pulse or gap duration", zero.duration_s() == 0)
        host = Host()
        runner = RecipeRunner(host)
        waits = []
        async def observe_zero_sleep(seconds, **_kwargs):
            waits.append(seconds)
        runner._sleep = observe_zero_sleep
        await runner._exec(zero)
        c.check("zero-time dose sends no valve commands", host.writes == [], str(host.writes))
        c.check("zero-time dose skips pulse gaps", waits == [], str(waits))
        try:
            builder({"dose_pulses": 0})
        except ValueError:
            pass
        else:
            c.check("zero pulses rejected", False)
    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
