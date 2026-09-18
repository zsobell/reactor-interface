"""Pre-start recipe persistence and API without hardware startup."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from reactor.config import load_config
from reactor.dependencies import DeviceFactory, StatePaths
from reactor.server.app import create_app
from reactor.control.prestart_store import RecipeConflictError
from reactor.supervisor import Supervisor
from reactor.testing.virtual_reactor import (
    DEFAULT_CONFIG_PATH, FakeDaq, FakeInstrument, FakeKeithley, FakeMfc, FakeSupply,
)
from tests._support import Checker, asgi_call


def factory() -> DeviceFactory:
    return DeviceFactory(
        daq=FakeDaq, mfc=FakeMfc, instrument=FakeInstrument,
        supply=lambda cfg: FakeSupply(cfg) if cfg.driver == "glassman_fl" else FakeKeithley(cfg),
        ellipsometer=lambda *args, **kwargs: None)


async def main() -> int:
    c = Checker("test_prestart_recipe_api")
    with tempfile.TemporaryDirectory(prefix="prestart_recipe_api_") as directory:
        root = Path(directory)
        cfg = load_config(DEFAULT_CONFIG_PATH)
        cfg.site.data_dir = str(root / "data")
        paths = StatePaths.in_directory(root)
        sup = Supervisor(cfg, devices=factory(), paths=paths)
        app = create_app(supervisor=sup)

        c.section("pure initial state")
        status, library = await asgi_call(app, "GET", "/api/prestart/recipes")
        c.check("missing file yields protected current recipe without writing",
                status == 200 and library["selected_id"] == "current-prestart"
                and library["recipes"][0]["builtin"] is True
                and not paths.prestart_recipes.exists())
        status, catalog = await asgi_call(app, "GET", "/api/prestart/capabilities")
        c.check("capabilities are served without startup",
                status == 200 and any(t["id"] == "controller:plasma"
                                      for t in catalog["targets"]))
        c.check("no device was constructed as connected",
                sup.daq is None and not sup.mfcs and not sup.supplies)

        c.section("create, save, select, preview")
        status, created = await asgi_call(app, "POST", "/api/prestart/recipes", {
            "name": "TEMAZr setup", "from_id": "current-prestart",
        })
        c.check("duplicate creates and persists an editable recipe",
                status == 200 and created["id"] == "temazr-setup"
                and created["builtin"] is False and paths.prestart_recipes.exists())
        edited = deepcopy(created)
        edited["name"] = "TEMAZr + NH3 setup"
        delay = next(step for step in edited["start_steps"] if step["action"] == "delay.wait")
        delay["args"]["seconds"] = 2.5
        status, saved = await asgi_call(
            app, "PUT", "/api/prestart/recipes/temazr-setup",
            {"expected_revision": created["revision"], "recipe": edited})
        c.check("save increments the revision", status == 200
                and saved["revision"] == created["revision"] + 1)
        status, stale = await asgi_call(
            app, "PUT", "/api/prestart/recipes/temazr-setup",
            {"expected_revision": created["revision"], "recipe": edited})
        c.check("stale save is refused without overwriting",
                status == 409 and "current revision" in stale["detail"])
        status, selected = await asgi_call(
            app, "POST", "/api/prestart/recipes/temazr-setup/select")
        c.check("selection is server-owned", status == 200
                and selected["selected_id"] == "temazr-setup")
        status, preview = await asgi_call(app, "POST", "/api/prestart/preview", {
            "recipe_id": "temazr-setup", "values": {"ar_sccm": 7.25},
        })
        c.check("preview resolves exact saved revision and launch values",
                status == 200 and preview["revision"] == saved["revision"]
                and preview["parameters"]["ar_sccm"] == 7.25
                and any("2.5 s" in step["summary"] for step in preview["start_steps"]))

        c.section("protected baseline and deletion")
        status, protected = await asgi_call(
            app, "DELETE", "/api/prestart/recipes/current-prestart")
        c.check("baseline cannot be deleted", status == 409
                and "cannot be deleted" in protected["detail"])
        status, _ = await asgi_call(
            app, "DELETE", "/api/prestart/recipes/temazr-setup")
        status2, after = await asgi_call(app, "GET", "/api/prestart/recipes")
        c.check("deleting selected custom recipe restores baseline selection",
                status == 200 and status2 == 200
                and after["selected_id"] == "current-prestart"
                and [r["id"] for r in after["recipes"]] == ["current-prestart"])

        c.section("restart and corruption behavior")
        recreated = sup.prestart_recipes.create("Persistent")
        second = Supervisor(cfg, devices=factory(), paths=paths)
        c.check("custom recipes survive a fresh Supervisor",
                second.prestart_recipes.get(recreated.id).name == "Persistent")

        c.section("atomic and concurrent writes")
        concurrent = second.prestart_recipes.create("Concurrent")
        first = concurrent.model_dump(mode="json")
        second_edit = deepcopy(first)
        first["description"] = "first writer"
        second_edit["description"] = "second writer"
        def attempt(payload):
            try:
                return second.prestart_recipes.save(
                    concurrent.id, payload, expected_revision=concurrent.revision)
            except Exception as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (first, second_edit)))
        c.check("concurrent stale saves have one winner and one conflict",
                sum(not isinstance(item, Exception) for item in outcomes) == 1
                and sum(isinstance(item, RecipeConflictError) for item in outcomes) == 1)

        atomic = second.prestart_recipes.create("Atomic failure")
        before = paths.prestart_recipes.read_text(encoding="utf-8")
        changed = atomic.model_dump(mode="json")
        changed["description"] = "must not land"
        try:
            with patch.object(Path, "replace", side_effect=OSError("injected replace failure")):
                second.prestart_recipes.save(
                    atomic.id, changed, expected_revision=atomic.revision)
            failed_cleanly = False
        except OSError:
            failed_cleanly = True
        c.check("failed atomic replace preserves prior valid file",
                failed_cleanly
                and paths.prestart_recipes.read_text(encoding="utf-8") == before
                and not paths.prestart_recipes.with_name(
                    paths.prestart_recipes.name + ".tmp").exists())

        raw = json.loads(paths.prestart_recipes.read_text(encoding="utf-8"))
        raw["schema_version"] = 999
        paths.prestart_recipes.write_text(json.dumps(raw), encoding="utf-8")
        c.check("unknown schema fails closed", _raises(
            lambda: second.prestart_recipes.load(), "could not load"))

    return c.summary()


def _raises(fn, text: str) -> bool:
    try:
        fn()
    except Exception as exc:
        return text in str(exc)
    return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
