"""Merged event/report failures and nested diagnostics retain their contracts."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reactor.control.clock import Clock
from reactor.control.recipe import Recipe, RecipeRunner, Step
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker
from tests.test_recording_errors import BrokenFile


async def main():
    c = Checker("test_merge_acceptance")
    for field in ("_events_fh", "_errors_fh"):
        for failure in ("write", "flush", "close"):
            async with VirtualReactor() as vr:
                sup = vr.sup
                await sup.start_recipe(Recipe(steps=[Step(op="wait", seconds=30)]))
                await sup.recording.drain()
                old = getattr(sup.logger, field)
                await sup.recording.run(old.close)
                broken = BrokenFile(failure)
                setattr(sup.logger, field, broken)
                sibling = getattr(sup.logger, "_errors_fh" if field == "_events_fh" else "_events_fh")
                sup.report_event("error", "fault injection marker")
                await sup.recording.drain()
                if failure != "close":
                    c.check(f"{field} {failure}: error is visible",
                            "events" in sup.recording.status()["errors"])
                    before = len(sup.errors)
                    sup.report_event("error", "second marker")
                    await sup.recording.drain()
                    c.check(f"{field} {failure}: no recursive error storm",
                            len(sup.errors) == before + 1)
                await sup.abort_recipe()
                await sup.recording.drain()
                c.check(f"{field} {failure}: both streams close despite failure",
                        broken.close_attempted and sibling.closed)
                c.check(f"{field} {failure}: recording failure remains latched",
                        bool(sup.recording.status()["errors"]))
                c.check(f"{field} {failure}: run admission is released", not sup.run_in_progress)

    for suffix in ("_events.log", "_errors.log", "_run_params.txt"):
        async with VirtualReactor() as vr:
            sup = vr.sup
            opened = []
            original_open = Path.open

            def fail_open(path, *args, **kwargs):
                if path.name.endswith(suffix):
                    raise OSError("injected open failure")
                handle = original_open(path, *args, **kwargs)
                if path.parent.is_relative_to(sup.logger.dir):
                    opened.append(handle)
                return handle

            with patch.object(Path, "open", fail_open):
                await sup.start_ald_run(dict(cycles=1, pump_a_s=30, ar_close_delay_s=0))
            c.check(f"{suffix}: recording failure visible without denying control",
                    bool(sup.recording.status()["errors"]) and sup.run_in_progress)
            await sup.abort_recipe()
            c.check(f"{suffix}: partial files close on cleanup",
                    bool(opened) and all(handle.closed for handle in opened))
            c.check(f"{suffix}: cleanup powers down HV", vr.supplies["hv"].hv_off_calls > 0)

    async with VirtualReactor() as vr:
        sup = vr.sup
        await sup.start_ald_run(dict(cycles=1, pump_a_s=30, ar_close_delay_s=0))
        original_write = Path.write_text
        def fail_report(path, *args, **kwargs):
            if path.name.endswith("_run_params.txt"):
                raise OSError("report rewrite failed")
            return original_write(path, *args, **kwargs)
        with patch.object(Path, "write_text", fail_report):
            result = await sup.update_run_params({"pump_a_s": 20})
        c.check("failed report rewrite preserves accepted live control parameters",
                "pump_a_s" in result["changed"] and sup.runs.params["pump_a_s"] == 20)
        c.check("failed report rewrite remains visible", "parameters" in sup.recording.status()["errors"])
        await sup.abort_recipe()
        c.check("failed report rewrite cannot hold run admission", not sup.run_in_progress)

    async with VirtualReactor() as vr:
        sup = vr.sup
        retry = {"attempt": 1, "detail": {"message": "before"}}
        sup.reconnect["ammeter"] = retry
        flags = [{"id": "mfc1", "commanded": 5, "measured": 0}]
        sup.report_event("error", "before frame")
        with patch.object(sup, "setpoint_flags", return_value=flags):
            frame = sup.state()
        retry["detail"]["message"] = "after"
        flags[0]["commanded"] = 99
        sup.errors[-1]["message"] = "after frame"
        c.check("nested retry diagnostics are frozen",
                next(i for i in frame["instruments"] if i["id"] == "ammeter")["retry"]["detail"]["message"] == "before")
        c.check("setpoint flags are frozen", frame["setpoint_flags"][0]["commanded"] == 5)
        c.check("error history is frozen independently of recording status",
                frame["errors"][-1]["message"] == "before frame" and not frame["logging"]["errors"])
        c.check("auxiliary thermocouples retain all display fields",
                all({"channel", "kind", "tc_type", "volts", "unit", "value"} <= a.keys() for a in frame["aux"]))

    wall = [1000.0]
    host = SimpleNamespace(set_sample_bias_output=AsyncMock(), report_event=lambda *args: None)
    runner = RecipeRunner(host, clock=Clock(elapsed=lambda: 10.0, wall=lambda: wall[0]))
    step = Step(op="electron_beam", bias_v=30)
    runner._schedule_bias(step, True, 60)
    first = runner._bias_pending[0][1]
    wall[0] -= 10000
    runner._schedule_bias(step, False, 90)
    superseded = runner._bias_pending[-1][1]
    wall[0] += 20000
    runner._schedule_bias(step, True, 75)
    c.check("bias deadline ordering ignores forward/backward wall jumps",
            [due for due, _ in runner._bias_pending] == [70.0, 85.0]
            and runner._bias_pending[0][1] is first)
    tasks = [task for _, task in runner._bias_pending] + [superseded]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    c.check("cancelled pending bias flips issue no commands", host.set_sample_bias_output.await_count == 0)
    runner._pause.clear()  # exercise the bias task's gate without a recipe task
    runner._schedule_bias(step, True, 0)
    await asyncio.sleep(0)
    wall[0] += 100000
    c.check("paused bias lead cannot energize on a wall jump", host.set_sample_bias_output.await_count == 0)
    runner._pause.set()
    await asyncio.gather(*(task for _, task in runner._bias_pending))
    c.check("resumed bias lead executes exactly once", host.set_sample_bias_output.await_count == 1)
    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
