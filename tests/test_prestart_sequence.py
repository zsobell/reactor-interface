"""Generic pre-start execution, immutable cleanup snapshots, and failure state."""
from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick, wait_for


async def finish(vr, timeout: float = 5.0) -> bool:
    return await wait_for(lambda: not vr.sup.prestart.get("running"), timeout)


async def main() -> int:
    c = Checker("test_prestart_sequence")

    async with VirtualReactor() as vr:
        tick = await autotick(vr, period=0.02)
        try:
            store = vr.sup.prestart_recipes
            created = store.create("Alternate gas setup")
            custom = deepcopy(created.model_dump(mode="json"))
            custom["start_steps"] = [
                {"id":"set-current", "target":"supply:steering",
                 "action":"supply.set_current", "args":{"amps":0.4}},
                {"id":"set-voltage", "target":"supply:steering",
                 "action":"supply.set_voltage", "args":{"volts":12.0}},
                {"id":"output-on", "target":"supply:steering",
                 "action":"supply.output_on"},
                {"id":"flow-mfc1", "target":"mfc:mfc1",
                 "action":"mfc.start_flow", "args":{"sccm":{"parameter":"ar_sccm"}}},
                {"id":"wait-mfc1", "target":"mfc:mfc1",
                 "action":"mfc.wait_flow", "args":{"operator":"above", "value":3.0,
                  "upper":0.0, "hold_s":0.0, "timeout_s":1.0, "absolute":False}},
                {"id":"open-prec2", "target":"valve:prec2", "action":"valve.open"},
                {"id":"settle", "target":"system:delay", "action":"delay.wait",
                 "args":{"seconds":0.01}},
            ]
            custom["abort_steps"] = [
                {"id":"stop-mfc1", "target":"mfc:mfc1", "action":"mfc.stop_flow",
                 "on_error":"continue"},
                {"id":"close-prec2", "target":"valve:prec2", "action":"valve.close",
                 "on_error":"continue"},
                {"id":"output-off", "target":"supply:steering",
                 "action":"supply.output_off", "on_error":"continue"},
            ]
            saved = store.save(created.id, custom, expected_revision=created.revision)

            c.section("capability-driven custom sequence")
            await vr.sup.start_prestart({
                "recipe_id":saved.id, "recipe_revision":saved.revision,
                "ar_sccm":3.5,
            })
            c.check("custom sequence completes", await finish(vr))
            steering = vr.supplies["steering"]
            c.check("typed supply actions used public commands",
                    steering.current_calls[-1:] == [0.4]
                    and steering.voltage_calls[-1:] == [12.0]
                    and steering.output_on is True)
            c.check("parameter-bound alternate MFC reached its wait",
                    vr.mfcs["mfc1"].commanded_sccm == 3.5
                    and vr.sup.prestart.get("step_id") == "settle")
            c.check("alternate valve opened", vr.daq.do_state.get("prec2") is True)
            c.check("successful session is primed with cleanup available",
                    vr.sup.prestart.get("primed") is True
                    and vr.sup.prestart.get("cleanup_available") is True)

            c.section("cleanup is the launch snapshot")
            edited = saved.model_dump(mode="json")
            edited["abort_steps"] = []
            store.save(saved.id, edited, expected_revision=saved.revision)
            await vr.sup.abort_prestart()
            c.check("later edit did not change active cleanup",
                    vr.mfcs["mfc1"].commanded_sccm == 0.0
                    and vr.daq.do_state.get("prec2") is False
                    and steering.output_on is False)
            c.check("cleanup is consumed exactly once",
                    vr.sup.prestart.get("cleanup_available") is False)
            calls = len(steering.output_calls)
            await vr.sup.abort_prestart()
            c.check("repeated abort does not actuate again",
                    len(steering.output_calls) == calls)

            c.section("partial failure requires cleanup")
            failure = store.get(saved.id).model_dump(mode="json")
            failure["start_steps"] = [
                {"id":"open-prec1", "target":"valve:prec1", "action":"valve.open"},
                {"id":"wait-pressure", "target":"sensor:pressure",
                 "action":"sensor.wait_until", "args":{"operator":"above",
                  "value":999.0, "upper":0.0, "hold_s":0.0,
                  "timeout_s":0.1, "absolute":False}},
            ]
            failure["abort_steps"] = [
                {"id":"close-prec1", "target":"valve:prec1", "action":"valve.close",
                 "on_error":"continue"},
            ]
            latest = store.save(saved.id, failure, expected_revision=saved.revision + 1)
            await vr.sup.start_prestart({
                "recipe_id":latest.id, "recipe_revision":latest.revision})
            c.check("timed-out wait reaches an error", await finish(vr)
                    and vr.sup.prestart.get("state") == "error")
            c.check("partial start keeps abort available",
                    vr.daq.do_state.get("prec1") is True
                    and vr.sup.prestart.get("cleanup_available") is True)
            try:
                await vr.sup.start_ald_run({"cycles":1, "dose_s":0.01,
                    "pump_a_s":0.01, "beam_s":0.01, "pump_b_s":0.01})
                run_refused = False
            except RuntimeError as exc:
                run_refused = "did not complete" in str(exc)
            c.check("run is refused after partial pre-start failure", run_refused)
            await vr.sup.abort_prestart()
            c.check("declared failure cleanup closes the affected valve",
                    vr.daq.do_state.get("prec1") is False)

            c.section("cleanup failures do not suppress later steps")
            cleanup_failure = store.get(saved.id).model_dump(mode="json")
            cleanup_failure["start_steps"] = [
                {"id":"open-prec1-again", "target":"valve:prec1", "action":"valve.open"},
                {"id":"flow-mfc1-again", "target":"mfc:mfc1",
                 "action":"mfc.start_flow", "args":{"sccm":2.0}},
            ]
            cleanup_failure["abort_steps"] = [
                {"id":"failing-close", "target":"valve:prec1", "action":"valve.close",
                 "on_error":"continue"},
                {"id":"later-stop", "target":"mfc:mfc1", "action":"mfc.stop_flow",
                 "on_error":"continue"},
            ]
            cleanup_recipe = store.save(
                saved.id, cleanup_failure, expected_revision=latest.revision)
            await vr.sup.start_prestart({
                "recipe_id":cleanup_recipe.id,
                "recipe_revision":cleanup_recipe.revision})
            c.check("cleanup-failure setup completes", await finish(vr)
                    and vr.sup.prestart.get("primed") is True)
            original_set_valve = vr.sup.set_valve
            async def fail_prec1_close(valve_id, state, *, reason=""):
                if valve_id == "prec1" and state is False:
                    raise OSError("injected close failure")
                return await original_set_valve(valve_id, state, reason=reason)
            vr.sup.set_valve = fail_prec1_close
            try:
                await vr.sup.abort_prestart()
            finally:
                vr.sup.set_valve = original_set_valve
            cleanup_receipts = [r for r in vr.sup.prestart.get("receipts", [])
                                if r["section"] == "cleanup"]
            c.check("later cleanup runs after an earlier failure",
                    vr.mfcs["mfc1"].commanded_sccm == 0.0
                    and [r["status"] for r in cleanup_receipts[-2:]]
                    == ["error", "ok"])
            c.check("cleanup failure is retained in terminal state",
                    vr.sup.prestart.get("state") == "error"
                    and "injected close failure" in
                    " ".join(vr.sup.prestart.get("cleanup_errors", [])))
            await original_set_valve("prec1", False, reason="test reset")

            c.section("review revision is enforced before commands")
            writes = len(vr.daq.do_writes)
            await vr.sup.start_prestart({
                "recipe_id":cleanup_recipe.id,
                "recipe_revision":cleanup_recipe.revision - 1})
            c.check("stale launch reports error", await finish(vr)
                    and "changed since review" in vr.sup.prestart.get("error", ""))
            c.check("stale launch issued no hardware commands",
                    len(vr.daq.do_writes) == writes
                    and vr.sup.prestart.get("cleanup_available") is False)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
