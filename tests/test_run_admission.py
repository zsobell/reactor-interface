"""Rejected and competing starts must not change the experiment already running."""
import asyncio
import sys
import threading
from pathlib import Path

from reactor.control.recipe import Recipe, Step
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


def metadata(sup):
    return (sup.runs.phase, sup.runs.session, sup.last_run_name, sup.logger.run_name, sup.logger.run_path,
            sup.logger.run_dir, sup.runs.session.dose_valve, sup.runs.session.plasma_switch,
            sup.runs.session.fill_valve, sup.runs.session.end_cleanup, list(sup.events))


async def main():
    c = Checker("test_run_admission")
    real_name = Path("config/last_run.json").read_bytes()
    async with VirtualReactor() as vr:
        sup = vr.sup
        await sup.start_ald_run(dict(run_name="First", cycles=1))
        before = metadata(sup)
        for start in (sup.start_ald_run, sup.start_cvd_run):
            try:
                await start(dict(run_name="Rejected", dose_valve="prec2",
                                 fill_valve="rpm_bottom", plasma_switch="prec2"))
            except RuntimeError:
                pass
            else:
                c.check("duplicate start rejected", False)
            c.check("rejection leaves all run metadata and events alone",
                    metadata(sup) == before)
    c.check("virtual runs leave operator metadata alone",
            Path("config/last_run.json").read_bytes() == real_name)

    async with VirtualReactor() as vr:
        outcomes = await asyncio.gather(
            vr.sup.start_ald_run(dict(run_name="Winner", cycles=1)),
            vr.sup.start_cvd_run(dict(run_name="Loser", cycles=1)),
            return_exceptions=True)
        c.check("exactly one competing request starts",
                sum(isinstance(r, RuntimeError) for r in outcomes) == 1)
        c.check("accepted request owns the name", vr.sup.last_run_name == "Winner")

    async with VirtualReactor() as vr:
        await vr.sup.start_prestart(dict(valve_delay_s=10))
        try:
            await vr.sup.start_recipe(Recipe(steps=[Step(op="wait", seconds=10)]))
        except RuntimeError:
            c.check("file recipes also reject overlapping pre-start", True)
        else:
            c.check("file recipes also reject overlapping pre-start", False)

    for cancel_request in (False, True):
        async with VirtualReactor() as vr:
            entered, release = threading.Event(), threading.Event()
            original = vr.sup.logger.start_run_export
            def slow_open(*args):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release recording startup")
                return original(*args)
            vr.sup.logger.start_run_export = slow_open
            pending = asyncio.create_task(vr.sup.start_ald_run(dict(run_name="Cancelled")))
            c.check("startup waits for recording", await asyncio.to_thread(entered.wait, 1))
            c.check("preparing phase is explicit", vr.sup.runs.phase.value == "preparing")
            if cancel_request:
                pending.cancel()
                abort = asyncio.create_task(asyncio.sleep(0))
            else:
                abort = asyncio.create_task(vr.sup.abort_recipe())
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(pending, abort, return_exceptions=True)
            c.check("abort cancels a preparing run", isinstance(results[0], asyncio.CancelledError if cancel_request else RuntimeError))
            c.check("cancelled preparation issues no valve commands", not vr.daq.do_writes)
            c.check("cancelled preparation is not the last started run", vr.sup.last_run_name == "")
            c.check("cancelled preparation restores metadata and cleanup policy",
                    vr.sup.logger.run_name == "" and not vr.sup.runs.session.end_cleanup)
            await vr.sup.start_recipe(Recipe(steps=[Step(op="wait", seconds=0.01)]))
            c.check("next file recipe does not inherit ALD cleanup", not vr.sup.runs.session.end_cleanup)

    for cancel_request in (False, True):
        async with VirtualReactor() as vr:
            old_name = await vr.sup.recording.set_run_name("Prior-007")
            entered, release = threading.Event(), threading.Event()
            original = vr.sup.logger.set_run_name

            def slow_set_run_name(name):
                if name == "Cancelled":
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test did not release run-name update")
                return original(name)

            vr.sup.logger.set_run_name = slow_set_run_name
            pending = asyncio.create_task(
                vr.sup.start_ald_run(dict(run_name="Cancelled"))
            )
            c.check("startup waits for run-name recording",
                    await asyncio.to_thread(entered.wait, 1))
            if cancel_request:
                pending.cancel()
                abort = asyncio.create_task(asyncio.sleep(0))
            else:
                abort = asyncio.create_task(vr.sup.abort_recipe())
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(pending, abort, return_exceptions=True)
            expected = asyncio.CancelledError if cancel_request else RuntimeError
            c.check("cancellation during naming prevents run start",
                    isinstance(results[0], expected))
            c.check("cancellation during naming issues no valve commands",
                    not vr.daq.do_writes)
            c.check("logger and service restore the prior run name",
                    vr.sup.logger.run_name == old_name
                    and vr.sup.recording.run_name == old_name)
            c.check("cancelled name is not remembered as a started run",
                    vr.sup.last_run_name == "")
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
