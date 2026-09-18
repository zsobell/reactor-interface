"""Pure pre-start recipe schema and capability tests."""
from __future__ import annotations

import asyncio

from reactor.config import load_config
from reactor.control.prestart_model import (
    CURRENT_RECIPE_ID,
    PrestartRecipe,
    capability_catalog,
    current_prestart_recipe,
    resolve_recipe,
)
from tests._support import Checker


async def main() -> int:
    c = Checker("test_prestart_recipes")
    cfg = load_config("config/reactor.yaml")
    catalog = capability_catalog(cfg)
    targets = {target["id"]: target for target in catalog["targets"]}

    c.section("capability catalog")
    c.check("catalog has a stable schema version", catalog["schema_version"] == 1)
    c.check("delay is a logical target", "system:delay" in targets)
    c.check("fill is a logical controller", "controller:fill" in targets)
    c.check("plasma is a logical controller", "controller:plasma" in targets)
    c.check("every configured valve is selectable",
            all(f"valve:{v.id}" in targets for v in cfg.valves))
    c.check("every configured MFC is selectable",
            all(f"mfc:{m.id}" in targets for m in cfg.mfcs))

    glassman = targets["supply:hv"]
    c.check("Glassman remains off-only",
            [a["id"] for a in glassman["actions"]] == ["hv.off"],
            str([a["id"] for a in glassman["actions"]]))
    bias_actions = {a["id"] for a in targets["supply:stage_bias"]["actions"]}
    c.check("stage bias exposes generic controls and arm metadata",
            {"supply.arm_bias", "supply.set_voltage", "supply.set_current",
             "supply.output_on", "supply.output_off"} <= bias_actions)
    c.check("readable sensors offer wait conditions",
            all(any(a["id"] == "sensor.wait_until" for a in target["actions"])
                for target in targets.values() if target["kind"] == "sensor"))

    c.section("current recipe")
    recipe = current_prestart_recipe(cfg)
    c.check("built-in id is stable", recipe.id == CURRENT_RECIPE_ID)
    c.check("round-trip is lossless",
            PrestartRecipe.model_validate(recipe.model_dump()) == recipe)
    resolved = resolve_recipe(recipe, catalog, {
        "ar_sccm": "6.5", "sample_bias_v": "12", "sample_bias_polarity": "-1",
    })
    summaries = [step.summary for step in resolved.start_steps]
    c.check("operator values resolve with their declared types",
            resolved.parameters["ar_sccm"] == 6.5
            and resolved.parameters["sample_bias_v"] == 12.0
            and resolved.parameters["sample_bias_polarity"] == -1,
            str(resolved.parameters))
    c.check("current recipe includes every required phase",
            any("Ar MFC pneumatic isolation" in line for line in summaries)
            and any("6.5 sccm" in line for line in summaries)
            and any("Regulate" in line for line in summaries)
            and any("Strike plasma" in line for line in summaries), str(summaries))
    c.check("current recipe has explicit ordered cleanup",
            [step.action for step in resolved.abort_steps][-1] == "valve.close"
            and resolved.abort_steps[0].action == "mfc.stop_flow")

    c.section("fail-closed validation")
    bad_target = recipe.model_copy(deep=True)
    bad_target.start_steps[0].target = "supply:not-real"
    c.check("unknown target is rejected", _raises(
        lambda: resolve_recipe(bad_target, catalog), "unknown target"))
    bad_action = recipe.model_copy(deep=True)
    bad_action.start_steps[0].action = "supply.launch"
    c.check("unsupported action is rejected", _raises(
        lambda: resolve_recipe(bad_action, catalog), "not supported"))
    bad_field = recipe.model_copy(deep=True)
    delay = next(s for s in bad_field.start_steps if s.action == "delay.wait")
    delay.args["seconds"] = -1
    c.check("field bounds are enforced", _raises(
        lambda: resolve_recipe(bad_field, catalog), "at least"))
    wrong_kind = recipe.model_copy(deep=True)
    fill = next(s for s in wrong_kind.start_steps if s.action == "fill.start")
    fill.args["gauge"] = "valve:rpm_top"
    c.check("target-kind mismatch is rejected", _raises(
        lambda: resolve_recipe(wrong_kind, catalog), "expected sensor"))

    return c.summary()


def _raises(fn, text: str) -> bool:
    try:
        fn()
    except (ValueError, TypeError) as exc:
        return text in str(exc)
    return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
