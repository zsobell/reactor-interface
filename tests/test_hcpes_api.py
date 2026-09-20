"""HCPES plan persistence, API containment, and linked-run workflow."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from pathlib import Path
from unittest.mock import patch

import yaml

from reactor.server.app import create_app
from reactor.control.hcpes_store import HcpesPlanStore, PlanConflictError
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, asgi_call, autotick, wait_for


def fast_plan(created: dict) -> dict:
    plan = deepcopy(created)
    values = {
        "mfc:ar": 1.0,
        "supply:stage_bias": 10.0,
        "supply:collimating": 1.5,
        "supply:steering": 0.4,
        "supply:grid_bias": 100.0,
    }
    for axis in plan["axes"]:
        target = axis["target"]
        if target in values:
            axis.clear()
            axis.update(target=target, mode="fixed", value=values[target])
    plan["settings"] = {
        "establishment": {
            "stable_window_s": 0.02,
            "maximum_wait_s": 0.08,
            "max_drift_a_per_min": 10.0,
        },
        "parameter_change": {
            "stable_window_s": 0.01,
            "maximum_wait_s": 0.04,
            "max_drift_a_per_min": 10.0,
        },
        "parameter_settle_s": 0.01,
        "plasma_min_current_a": 0.0001,
        "recovery_window_s": 0.08,
        "reignite_pulse_s": 0.01,
        "reignite_settle_s": 0.01,
        "qualified_samples": 2,
    }
    return plan


async def main() -> int:
    c = Checker("test_hcpes_api")
    async with VirtualReactor() as vr:
        app = create_app(supervisor=vr.sup)
        plans_path = vr.sup.paths.hcpes_plans

        c.section("protected template and capability contract")
        status, library = await asgi_call(app, "GET", "/api/hcpes/plans")
        c.check("missing file yields a protected complete template without writing",
                status == 200
                and library["selected_id"] == "current-hcpes-plan"
                and library["plans"][0]["builtin"] is True
                and not plans_path.exists())
        status, catalog = await asgi_call(app, "GET", "/api/hcpes/capabilities")
        c.check("capabilities expose all configured controls",
                status == 200
                and {axis["target"] for axis in catalog["axes"]}
                == {axis["target"] for axis in library["plans"][0]["axes"]})
        status, refused = await asgi_call(app, "POST", "/api/hcpes/start", {
            "plan_id": "current-hcpes-plan", "expected_revision": 1,
            "session_id": "must-not-run", "polarity_confirmed": True,
        })
        c.check("protected zero template cannot launch",
                status == 409 and "duplicate" in refused["detail"])

        c.section("revisioned create, save, preview, and stale containment")
        status, created = await asgi_call(app, "POST", "/api/hcpes/plans", {
            "name": "Cathode map",
        })
        edited = fast_plan(created)
        status, saved = await asgi_call(
            app, "PUT", f"/api/hcpes/plans/{created['id']}",
            {"expected_revision": created["revision"], "plan": edited})
        c.check("saved plan increments revision and persists",
                status == 200 and saved["revision"] == 2 and plans_path.exists())
        status, stale = await asgi_call(
            app, "PUT", f"/api/hcpes/plans/{created['id']}",
            {"expected_revision": created["revision"], "plan": edited})
        c.check("stale edits cannot overwrite the plan",
                status == 409 and "current revision" in stale["detail"])
        status, preview = await asgi_call(app, "POST", "/api/hcpes/preview", {
            "plan_id": saved["id"],
        })
        c.check("server preview reports nesting, exact counts, and cleanup",
                status == 200
                and preview["plan"]["revision"] == saved["revision"]
                and preview["estimate"]["points"] == 1
                and preview["nesting"][:2] == ["mfc:ar", "supply:stage_bias"]
                and all(preview["cleanup"].values()))
        status, stale_start = await asgi_call(app, "POST", "/api/hcpes/start", {
            "plan_id": saved["id"], "expected_revision": 1,
            "session_id": "stale-start", "polarity_confirmed": True,
        })
        c.check("launch requires the exact saved revision",
                status == 409 and "reviewed" in stale_start["detail"])

        c.section("restart, concurrency, and atomic replacement")
        restarted = HcpesPlanStore(plans_path, vr.sup.cfg)
        c.check("saved plans survive a fresh store instance",
                restarted.get(saved["id"]).revision == saved["revision"])
        concurrent = restarted.create("Concurrent writes")
        first = concurrent.model_dump(mode="json")
        second = deepcopy(first)
        first["description"] = "first writer"
        second["description"] = "second writer"

        def save_once(plan):
            try:
                return restarted.save(
                    concurrent.id, plan, expected_revision=concurrent.revision)
            except Exception as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save_once, (first, second)))
        c.check("concurrent saves have one winner and one revision conflict",
                sum(not isinstance(item, Exception) for item in outcomes) == 1
                and sum(isinstance(item, PlanConflictError) for item in outcomes) == 1)
        atomic = restarted.create("Atomic failure")
        before = plans_path.read_text(encoding="utf-8")
        changed = atomic.model_dump(mode="json")
        changed["description"] = "must not land"
        try:
            with patch.object(Path, "replace", side_effect=OSError("injected replace failure")):
                restarted.save(
                    atomic.id, changed, expected_revision=atomic.revision)
            failed_cleanly = False
        except OSError:
            failed_cleanly = True
        c.check("failed replacement preserves the prior complete library",
                failed_cleanly and plans_path.read_text(encoding="utf-8") == before
                and not plans_path.with_name(plans_path.name + ".tmp").exists())

        c.section("completed positive run creates exact negative follow-up")
        vr.instruments["ammeter"].value = 0.001
        ticker = await autotick(vr, period=0.005)
        try:
            status, started = await asgi_call(app, "POST", "/api/hcpes/start", {
                "plan_id": saved["id"], "expected_revision": saved["revision"],
                "session_id": "positive-api", "polarity_confirmed": True,
            })
            completed = await wait_for(
                lambda: not vr.sup.hcpes_running, timeout=3.0)
            c.check("saved positive plan runs through the contained API",
                    status == 200 and started["polarity"] == 1 and completed
                    and vr.sup.hcpes["phase"] == "complete")
            status, linked = await asgi_call(
                app, "POST", f"/api/hcpes/plans/{saved['id']}/opposite", {
                    "expected_revision": saved["revision"],
                    "source_session_id": "positive-api",
                    "name": "Cathode map negative",
                    "campaign_name": "Cathode map full polarity",
                })
            negative = linked["plan"]
            campaign = linked["campaign"]
            c.check("opposite clone is durable and campaign-linked",
                    status == 200 and negative["stage_polarity"] == -1
                    and negative["revision"] == 1
                    and campaign["status"] == "awaiting_opposite"
                    and campaign["sessions"][0]["session_id"] == "positive-api")

            status, negative_started = await asgi_call(
                app, "POST", "/api/hcpes/start", {
                    "plan_id": negative["id"],
                    "expected_revision": negative["revision"],
                    "session_id": "negative-api",
                    "polarity_confirmed": True,
                    "campaign_id": campaign["id"],
                })
            finalized = await wait_for(
                lambda: vr.sup.hcpes_plans.load().campaigns[0].status == "complete",
                timeout=3.0,
            )
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        c.check("negative follow-up finalizes after its own full run and cleanup",
                status == 200 and negative_started["polarity"] == -1 and finalized)
        campaign_dir = vr.sup.logger.dir / f"HCPES-campaign-{campaign['id']}"
        manifest = yaml.safe_load((campaign_dir / "campaign.yaml").read_text(
            encoding="utf-8"))
        rows = list(csv.DictReader((campaign_dir / "combined_points.csv").open(
            encoding="utf-8", newline="")))
        c.check("combined campaign keeps immutable sources and signed ordering",
                manifest["campaign"]["complete"] is True
                and [float(row["signed_stage_bias_v"]) for row in rows] == [-10.0, 10.0]
                and [row["source_session"] for row in rows]
                == ["negative-api", "positive-api"])

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
