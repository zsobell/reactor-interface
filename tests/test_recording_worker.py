"""A stalled disk cannot stall acquisition; queued samples retain their values."""
import asyncio
import csv
import sys
import threading
from types import SimpleNamespace

from reactor.control.recipe import Recipe, Step
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, wait_for


async def main():
    c = Checker("test_recording_worker")
    async with VirtualReactor() as vr:
        sup = vr.sup
        manual = await sup.recording.call("start")
        manual_stamp = sup.logger.started_at + 1.0
        await sup.start_recipe(Recipe(steps=[Step(op="wait", seconds=20)]))
        path = sup.logger.run_path
        entered, release = threading.Event(), threading.Event()
        worker = sup.recording
        worker.max_pending = 3
        def stall():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release writer")
        blocked = asyncio.create_task(worker.run(stall))
        try:
            c.check("writer stalled", await asyncio.to_thread(entered.wait, 2))
            progress = SimpleNamespace(cycle_fraction=0.25, paused=False, step_desc="original")
            sample = {"t": sup.recipes.progress.started_at + 1, "pressure": 12.0}
            worker.submit("write_run_sample", sample, progress)
            worker.submit("write_sample", {"pressure": 42}, sampled_at=manual_stamp)
            sample["pressure"] = 999
            progress.step_desc = "mutated"
            await asyncio.wait_for(sup._current_cycle(), 0.5)
            await asyncio.wait_for(sup.set_valve("prec1", True), 0.5)
            c.check("control and telemetry advance while disk stalls",
                    sup._cycle_count == 1 and vr.daq.do_state["prec1"])
            c.check("backlog is bounded", not worker.submit("write_run_sample", sample, progress))
            c.check("overflow is visible", "queue" in worker.status()["errors"])
            abort = asyncio.create_task(sup.abort_recipe())
            c.check("hardware cleanup does not wait for stalled recording",
                    await wait_for(lambda: vr.supplies["hv"].hv_off_calls == 1, timeout=1))
        finally:
            release.set()
            await blocked
        await worker.drain()
        await abort
        rows = list(csv.DictReader(path.open()))
        c.check("measurement copied at submission", rows[0]["pressure"] == "12")
        c.check("recipe progress copied at submission", rows[0]["recipe_step"] == "original")
        c.check("close drains accepted rows", len(rows) == 2)
        c.check("overflow remains visible after draining", "queue" in worker.status()["errors"])
        await worker.call("stop")
        manual_rows = list(csv.DictReader(manual.open(), delimiter="\t"))
        c.check("manual log uses measurement time, not worker time",
                float(manual_rows[0]["Time"]) == 1.0)

    async with VirtualReactor() as vr:
        events = []
        vr.sup.recording._on_error = events.append
        def fail():
            raise OSError("injected worker failure")
        try:
            await vr.sup.recording.run(fail)
        except OSError:
            pass
        await vr.sup.recording.drain()
        c.check("worker failures reach telemetry", bool(vr.sup.recording.status()["errors"]))
        c.check("worker failures reach the event loop", bool(events))
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
