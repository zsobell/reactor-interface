"""Opening-input failures release pre-start ownership without hardware actions."""
import asyncio

from reactor.control.recipe import Recipe, Step
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker
from tests.test_parameters import prestart_trace


async def main():
    c = Checker("test_prestart_invalid")
    for value in ("bad", None):
        trace, events, state, error = await prestart_trace({"ar_sccm": value})
        c.check(f"invalid opening {value!r} issues no commands", trace == [])
        c.check("failure is reported without an unhandled task exception",
                error is None and any(kind == "error" and "pre-start failed" in text
                                      for kind, text in events))
        c.check("failed pre-start releases ownership and is not primed",
                not state["running"] and state.get("done") is False
                and state["phase"].startswith("error:"))

    for next_operation in ("prestart", "run"):
        async with VirtualReactor() as vr:
            await vr.sup.start_prestart({"ar_sccm": "bad"})
            await asyncio.gather(vr.sup._prestart.task, return_exceptions=True)
            c.check("invalid opening does not write any valve output", vr.daq.do_writes == [])
            try:
                if next_operation == "prestart":
                    vr.sup.snapshot["inst.ammeter"] = 0.001
                    await vr.sup.start_prestart(dict(valve_delay_s=0, hold_s=0, reignite_settle_s=0))
                    await asyncio.wait_for(vr.sup._prestart.task, timeout=2)
                    success = vr.sup.prestart["done"]
                else:
                    await vr.sup.start_recipe(Recipe(steps=[Step(op="wait", seconds=0)]))
                    await asyncio.wait_for(vr.sup.recipes._task, timeout=2)
                    success = vr.sup.recipes.progress.state == "done"
            except RuntimeError:
                success = False
            c.check(f"a valid {next_operation} can follow malformed pre-start", success)
    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
