"""Golden recipe/report compatibility and pre-start conversion-stage behavior."""
from __future__ import annotations
import asyncio
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

from reactor.control.recipe_model import build_ald_recipe, build_cvd_recipe
from reactor.control.parameters import PrestartParameters, RunParameters
from reactor.control.prestart import PrestartController
from reactor.run_report import format_run_params
from tests._support import Checker

CASES = [
    ("ald", {}),
    ("cvd", {}),
    ("ald", {"cycles": "2", "dose_s": True, "beam_s": "0.4", "run_name": "Raw-007",
             "mfc1_gas_enable": "false", "mfc1_gas_pct": "40", "mfc1_gas_flow_sccm": "2.5",
             "mfc2_gas_enable": 1, "mfc2_gas_order": "second", "mfc2_gas_pct": "60",
             "unknown": {"future": [1, 2]}, "ar_close_delay_s": "0"}),
    ("cvd", {"cycles": 2.9, "beam_s": "ignored", "pump_b_s": None,
             "mfc1_gas_enable": False, "mfc1_gas_order": "ignored", "mfc1_gas_pct": "ignored",
             "sample_bias_v": 0, "name": "custom", "gas_overlap_s": "0.25"}),
]


def outputs():
    result = []
    for mode, raw in CASES:
        recipe = (build_ald_recipe if mode == "ald" else build_cvd_recipe)(raw)
        result.append({"recipe": recipe.model_dump(), "report": format_run_params(
            raw, recipe, "Raw-007", datetime(2026, 9, 13, 12, 0))})
    return result


async def prestart_trace(params):
    trace = []
    events = []
    host = SimpleNamespace(run_in_progress=False, snapshot={"inst.ammeter": 0.001},
                           report_event=lambda *event: events.append(event))
    for name in ("supplies_output_on", "set_valve", "set_mfc_setpoint", "start_fill_regulation"):
        async def command(*args, _name=name, **kwargs):
            trace.append((_name, args, kwargs))
        setattr(host, name, command)
    controller = PrestartController(host)
    await controller.start(dict(valve_delay_s=0, hold_s=0, reignite_settle_s=0, **params))
    error = None
    try:
        await controller.task
    except (ValueError, TypeError) as exc:
        error = type(exc).__name__
    return trace, events, controller.state, error


async def main():
    c = Checker("test_parameters")
    golden = json.loads(Path(__file__).with_name("fixtures").joinpath("parameters.json").read_text(encoding="utf-8"))
    c.check("ALD/CVD recipes and raw reports match reviewed integrated fixtures", outputs() == golden)
    trace, events, state, error = await prestart_trace({"ar_sccm": "bad"})
    c.check("initial conversion failure is reported before commands or cleanup",
            trace == [] and error is None and state["running"] is False
            and state["phase"].startswith("error:"))
    trace, events, state, error = await prestart_trace({"sample_bias_v": "bad"})
    c.check("supply conversion failure runs only existing beam-ground cleanup",
            [t[0] for t in trace] == ["set_valve"]
            and trace[0][1] == ("plasma_ground", True) and error is None and not state["running"])
    trace, events, state, error = await prestart_trace({"dose_pressure_torr": "bad"})
    c.check("fill conversion failure preserves preceding supply and Ar commands",
            [t[0] for t in trace] == ["supplies_output_on", "set_valve", "set_mfc_setpoint", "set_valve"]
            and trace[1][1] == ("ar_pneumatic", True) and trace[-1][1] == ("plasma_ground", True))
    trace, events, state, error = await prestart_trace({"ar_sccm": "3.5", "sample_bias_polarity": "-1"})
    c.check("representative prestart converts numbers and keeps full command ordering",
            [t[0] for t in trace] == ["supplies_output_on", "set_valve", "set_mfc_setpoint",
                                    "start_fill_regulation", "set_valve", "set_valve"]
            and trace[0][2]["polarity"] == -1 and trace[2][1] == ("ar", 3.5) and state["done"])
    c.section("typed boundaries preserve raw snapshots and schema policy")
    raw = {"cycles": "2", "unknown": {"future": [1, 2]}}
    typed = RunParameters.normalize(raw, mode="ald")
    c.check("already normalized run objects are reused",
            RunParameters.normalize(typed, mode="ald") is typed)
    c.check("raw report values retain legacy representations", typed.cycles == 2 and typed.raw["cycles"] == "2")
    raw["unknown"]["future"].append(3)
    returned = typed.raw
    returned["unknown"]["future"].append(4)
    c.check("callers cannot mutate captured unknown report parameters",
            typed.raw["unknown"] == {"future": [1, 2]})
    c.check("typed and dictionary builders produce equivalent recipes",
            build_ald_recipe(typed) == build_ald_recipe(typed.raw))
    pre_raw = {"ar_sccm": "3.5", "future": [1]}
    pre_typed = PrestartParameters.normalize(pre_raw)
    pre_raw["ar_sccm"] = "9"
    pre_raw["future"].append(2)
    c.check("prestart captures input before background task begins",
            pre_typed.opening().ar_sccm == 3.5 and pre_typed.raw["future"] == [1])
    c.check("legacy null optional valve remains accepted by existing schema",
            build_ald_recipe({"fill_valve": None}).setup[1].valve is None)
    for payload in ({"dose_s": -1}, {"mfc1_gas_enable": True, "mfc2_gas_enable": True},
                    {"mfc1_gas_enable": True, "mfc1_gas_order": "invalid"}):
        try:
            build_ald_recipe(payload)
        except ValueError:
            rejected = True
        else:
            rejected = False
        c.check(f"existing invalid recipe input remains rejected: {payload}", rejected)
    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
