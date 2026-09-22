"""Exclusive HCPES execution, inaccessible points, and cleanup receipts."""
from __future__ import annotations

import asyncio
import csv
import json
import sys
import threading
from pathlib import Path

import yaml

from reactor.control.hcpes import _robust_slope_a_per_min
from reactor.control.hcpes_model import HcpesPlan, resolve_plan
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick, wait_for


def plan(
    cfg, *, grids=(100.0,), settle=0.01, recovery=0.08,
    stability_window=0.02, stability_wait=0.08, max_drift=10.0,
    parameter_window=0.01, parameter_wait=0.04, parameter_drift=5.0,
    qualified=3, condition_mode="time", initial=None,
) -> HcpesPlan:
    axes = [
        {"target": "mfc:ar", "mode": "fixed", "value": 1.0},
        {"target": "supply:stage_bias", "mode": "fixed", "value": 10.0},
        {"target": "supply:collimating", "mode": "fixed", "value": 1.5},
        {"target": "supply:steering", "mode": "fixed", "value": 0.4},
        {"target": "supply:grid_bias", "mode": "list", "values": list(grids)},
    ]
    axes.extend(
        {"target": f"mfc:{mfc.id}", "mode": "locked_zero"}
        for mfc in cfg.mfcs if mfc.id != "ar")
    payload = {
        "id": "controller-test", "name": "Controller test", "axes": axes,
        "settings": {
            "establishment": {
                "stable_window_s": stability_window,
                "maximum_wait_s": stability_wait,
                "max_drift_a_per_min": max_drift,
            },
            "parameter_change": {
                "stable_window_s": parameter_window,
                "maximum_wait_s": parameter_wait,
                "max_drift_a_per_min": parameter_drift,
            },
            "parameter_settle_s": settle,
            "condition_settle_mode": condition_mode,
            "plasma_min_current_a": 0.0001,
            "recovery_window_s": recovery,
            "reignite_pulse_s": 0.01,
            "reignite_settle_s": 0.01,
            "qualified_samples": qualified,
        },
    }
    if initial is not None:
        payload["initial_setpoints"] = initial
    return HcpesPlan.model_validate(payload)


async def rejects(awaitable, text: str) -> bool:
    try:
        await awaitable
    except RuntimeError as exc:
        return text in str(exc)
    return False


async def main() -> int:
    c = Checker("test_hcpes_controller")
    c.section("exclusive owner and successful condition")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 0.001
        resolved = resolve_plan(plan(vr.sup.cfg, settle=0.03), vr.sup.cfg)
        ticker = await autotick(vr, period=0.005)
        try:
            await vr.sup.start_hcpes(
                resolved, "exclusive-positive", polarity_confirmed=True)
            c.check("HCPES owns session immediately", vr.sup.hcpes_running)
            c.check("manual Ar write refused", await rejects(
                vr.sup.set_mfc_setpoint("ar", 2), "HCPES characterization owns"))
            c.check("manual background MFC write refused", await rejects(
                vr.sup.set_mfc_setpoint("mfc1", 1), "HCPES characterization owns"))
            c.check("manual sweep-supply write refused", await rejects(
                vr.sup.set_supply_voltage("grid_bias", 50),
                "HCPES characterization owns"))
            c.check("normal run start refused", await rejects(
                vr.sup.start_ald_run({"run_name": "Blocked"}), "HCPES"))
            c.check("main pre-start refused", await rejects(
                vr.sup.start_prestart({}), "HCPES"))
            c.check("background fill refused", await rejects(
                vr.sup.start_fill_regulation(
                    valve="rpm_top", gauge="gauge.prec1_dose",
                    target_torr=0.02), "HCPES"))
            c.check("valve identification refused", await rejects(
                vr.sup.start_valve_sweep(["cDAQ2Mod2/port0/line0"]), "HCPES"))
            c.check("session completes", await wait_for(
                lambda: not vr.sup.hcpes_running, timeout=3))
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        state = vr.sup.hcpes
        c.check("successful session reports complete", state["phase"] == "complete")
        c.check("all MFCs end at zero",
                all(mfc.commanded_sccm == 0 for mfc in vr.mfcs.values()))
        c.check("Ar closes only after collection", not vr.sup.valve_state["ar_pneumatic"])
        c.check("relay is parked", not vr.sup.valve_state["plasma_ground"])
        c.check("all four Keithley outputs are off",
                all(not vr.supplies[supply].output_on for supply in (
                    "stage_bias", "grid_bias", "collimating", "steering")))
        commanded = state["applied_setpoints"]
        c.check("cleanup display retains only authoritative zeroed MFCs",
                commanded
                and all(target.startswith("mfc:") and value == 0
                        for target, value in commanded.items()),
                str(commanded))
        c.check("HV off command has a receipt",
                vr.supplies["hv"].hv_off_calls == 1
                and any(r["what"] == "command HV off" and r["ok"]
                        for r in state["cleanup_receipts"]))
        expected_cleanup = [
            *(f"zero MFC {mfc.id}" for mfc in vr.sup.cfg.mfcs),
            "close Ar isolation", "command HV off",
            "switch off stage_bias", "switch off grid_bias",
            "switch off collimating", "switch off steering",
            "park plasma relay",
        ]
        c.check("cleanup receipts preserve the authorized command order",
                [row["what"] for row in state["cleanup_receipts"]]
                == expected_cleanup,
                str([row["what"] for row in state["cleanup_receipts"]]))
        bundle = vr.sup.recording.status()["hcpes"]
        rows = list(csv.DictReader(open(
            bundle["directory"] + "/points.csv", encoding="utf-8", newline="")))
        c.check("qualified point is analysis ready",
                len(rows) == 1 and rows[0]["accessibility"] == "accessible"
                and rows[0]["actual_qualified_samples"] == "3"
                and rows[0]["started_elapsed_s"] != ""
                and rows[0]["ended_elapsed_s"] != ""
                and rows[0]["chamber_pressure_mean_torr"] != ""
                and rows[0]["stage_temperature_mean_c"] != ""
                and rows[0]["aperture_lifetime_mean_s"] != ""
                and rows[0]["ar_baratron_mean_torr"] != ""
                and rows[0]["hv_voltage_mean_v"] != ""
                and rows[0]["hv_current_mean_ma"] != "")
        channel_row = json.loads(next(open(
            bundle["directory"] + "/point_channels.jsonl", encoding="utf-8")))
        required_channels = {
            "pressure", "stage.temp", "gauge.ar_baratron",
            "hv.hv.voltage", "hv.hv.current", "inst.ammeter",
            "aperture_lifetime_s",
        }
        c.check("point channel summary covers the complete numeric snapshot",
                required_channels <= set(channel_row["channels"])
                and all(channel_row["channels"][key]["count"] == 3
                        for key in required_channels))
        raw = [json.loads(line) for line in open(
            bundle["directory"] + "/raw.jsonl", encoding="utf-8")]
        qualified = [row for row in raw if row.get("qualified")]
        c.check("qualified raw observations retain complete snapshots",
                len(qualified) == 3
                and all(required_channels <= set(row["measurements"])
                        for row in qualified))
        qualified_times = [row["elapsed_s"] for row in qualified]
        c.check("qualified samples are the next fresh instrument readings",
                all(0 < later - earlier < 0.25
                    for earlier, later in zip(qualified_times, qualified_times[1:])),
                str(qualified_times))
        c.check("no artificial sample-interval observations are inserted",
                not any(row.get("exclusion_reason") == "sample_interval" for row in raw))
        c.check("startup programs immediately then uses one current gate",
                not any(row.get("exclusion_reason") == "parameter_settle"
                        for row in raw)
                and any(row.get("exclusion_reason") == "stability_window"
                        for row in raw))

    c.section("independent initial plasma condition precedes sweep point one")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 0.001
        initial = {
            "mfc:ar": 5.0,
            "supply:stage_bias": 20.0,
            "supply:collimating": 2.0,
            "supply:steering": 1.0,
            "supply:grid_bias": 150.0,
        }
        resolved = resolve_plan(plan(
            vr.sup.cfg, condition_mode="current", initial=initial,
            stability_window=0.02, stability_wait=0.2,
            parameter_window=0.01, parameter_wait=0.2), vr.sup.cfg)
        ticker = await autotick(vr, period=0.005)
        try:
            await vr.sup.start_hcpes(
                resolved, "independent-initial", polarity_confirmed=True)
            complete = await wait_for(lambda: not vr.sup.hcpes_running, timeout=3)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        bundle = vr.sup.recording.status()["hcpes"]
        raw = [json.loads(line) for line in open(
            bundle["directory"] + "/raw.jsonl", encoding="utf-8")]
        first_qualified = next(row for row in raw if row.get("qualified"))
        initial_rows = [row for row in raw
                        if row.get("phase") == "initial_parameter"]
        first_point_gate = [
            row for row in raw
            if row.get("point_index") == 1
            and row.get("flags", {}).get("stability_profile")
            == "parameter_change"
        ]
        c.check("startup values are commanded before point-one values",
                complete
                and vr.supplies["stage_bias"].voltage_calls[:2] == [20.0, 10.0]
                and vr.supplies["grid_bias"].voltage_calls[:2] == [150.0, 100.0]
                and vr.supplies["collimating"].current_calls[:2] == [2.0, 1.5]
                and vr.supplies["steering"].current_calls[:2] == [1.0, 0.4],
                str({key: {
                    "voltage": vr.supplies[key].voltage_calls,
                    "current": vr.supplies[key].current_calls,
                } for key in ("stage_bias", "grid_bias", "collimating", "steering")}))
        c.check("initial condition is recorded but never qualified",
                len(initial_rows) == 5
                and all(row.get("point_index") is None
                        and not row.get("qualified") for row in initial_rows)
                and first_qualified["point_index"] == 1)
        c.check("changed first point receives parameter stability gate",
                bool(first_point_gate)
                and first_point_gate[-1]["elapsed_s"]
                < first_qualified["elapsed_s"])
        manifest = yaml.safe_load(
            Path(bundle["directory"] + "/manifest.yaml").read_text(
                encoding="utf-8"))
        c.check("bundle reports the separate initial outcome",
                manifest["initial_plasma"]["setpoints"]["mfc:ar"] == 5.0
                and manifest["initial_plasma"]["settled"] is True)

    c.section("inaccessible regime advances and does not cycle supplies")
    async with VirtualReactor() as vr:
        resolved = resolve_plan(plan(vr.sup.cfg, grids=(100, 200)), vr.sup.cfg)
        vr.instruments["ammeter"].value = 0.001

        async def staged_ticks():
            while True:
                grid = vr.supplies["grid_bias"].voltage_setpoint or 0
                vr.instruments["ammeter"].value = 0.0 if grid >= 200 else 0.001
                await vr.tick()
                await asyncio.sleep(0.005)

        ticker = asyncio.create_task(staged_ticks())
        try:
            await vr.sup.start_hcpes(
                resolved, "inaccessible-positive", polarity_confirmed=True)
            c.check("sweep completes past inaccessible condition", await wait_for(
                lambda: not vr.sup.hcpes_running, timeout=4))
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        bundle = vr.sup.recording.status()["hcpes"]
        rows = list(csv.DictReader(open(
            bundle["directory"] + "/points.csv", encoding="utf-8", newline="")))
        c.check("both conditions retain summaries",
                len(rows) == 2 and rows[0]["accessibility"] == "accessible"
                and rows[1]["accessibility"] == "inaccessible"
                and rows[1]["actual_qualified_samples"] == "0")
        c.check("recovery pulses do not cycle any supply",
                all(vr.supplies[supply].output_calls.count(True) == 1
                    for supply in ("stage_bias", "grid_bias", "collimating", "steering")))
        c.check("controller continues rather than failing session",
                vr.sup.hcpes["phase"] == "complete"
                and vr.sup.hcpes["inaccessible_points"] == 1)
        c.check("progress distinguishes accepted and rejected conditions",
                vr.sup.hcpes["points_completed"] == 2
                and vr.sup.hcpes["points_collected"] == 1
                and vr.sup.hcpes["points_rejected"] == 1
                and vr.sup.hcpes["estimated_remaining_s"] == 0)

    c.section("condition settling mode is explicit")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 0.001
        resolved = resolve_plan(plan(
            vr.sup.cfg, grids=(100, 200), condition_mode="current",
            settle=0.2, stability_window=0.02, stability_wait=0.08,
            parameter_wait=0.2), vr.sup.cfg)
        ticker = await autotick(vr, period=0.005)
        try:
            await vr.sup.start_hcpes(
                resolved, "current-settle-mode", polarity_confirmed=True)
            complete = await wait_for(lambda: not vr.sup.hcpes_running, timeout=3)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        bundle = vr.sup.recording.status()["hcpes"]
        raw = [json.loads(line) for line in open(
            bundle["directory"] + "/raw.jsonl", encoding="utf-8")]
        parameter_gate = [
            row for row in raw
            if row.get("point_index") == 2
            and row.get("exclusion_reason") == "stability_window"
            and row.get("flags", {}).get("stability_profile")
            == "parameter_change"
        ]
        c.check("current mode gates the changed second condition without timed delay",
                complete
                and any(row.get("flags", {}).get("stable_window_required_s")
                        == 0.01
                        and row.get("flags", {}).get("max_drift_a_per_min") == 5.0
                        for row in parameter_gate)
                and not any(row.get("point_index") == 2
                            and row.get("exclusion_reason") == "parameter_settle"
                            for row in raw))
        stable_ticks_after_trend = [
            float(row["flags"]["stable_window_elapsed_s"])
            for row in parameter_gate
            if row.get("flags", {}).get("observed_drift_a_per_min") is not None
        ]
        c.check("stable timer starts at zero after trend qualifies",
                stable_ticks_after_trend
                and stable_ticks_after_trend[0] == 0
                and max(stable_ticks_after_trend) >= 0.01,
                str(stable_ticks_after_trend))

    c.section("robust drift rejects spikes but preserves a real trend")
    stable = [(i, 0.001 + (0.0005 if i == 10 else 0.0)) for i in range(21)]
    endpoint_spike = [(i, 0.001 + (0.0005 if i == 9 else 0.0))
                      for i in range(10)]
    ramp = [(i, 0.001 + (0.002 / 60.0) * i) for i in range(21)]
    c.check("isolated current spike does not invent long-term drift",
            abs(_robust_slope_a_per_min(stable)) < 1e-9
            and abs(_robust_slope_a_per_min(endpoint_spike)) < 1e-9)
    c.check("slow monotonic drift retains its physical A/min rate",
            abs(_robust_slope_a_per_min(ramp) - 0.002) < 1e-9)

    c.section("recorded point drift follows the qualified collection")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 0.001
        resolved = resolve_plan(plan(
            vr.sup.cfg, grids=(100, 200), settle=0.04,
            stability_window=0.02, qualified=5), vr.sup.cfg)

        async def collection_ramp_ticks():
            while True:
                state = vr.sup.hcpes
                if (state.get("point_index") == 2
                        and state.get("phase") == "collecting qualified samples"):
                    vr.instruments["ammeter"].value += 0.00002
                else:
                    vr.instruments["ammeter"].value = 0.001
                await vr.tick()
                await asyncio.sleep(0.005)

        ticker = asyncio.create_task(collection_ramp_ticks())
        try:
            await vr.sup.start_hcpes(
                resolved, "collection-drift-positive", polarity_confirmed=True)
            complete = await wait_for(lambda: not vr.sup.hcpes_running, timeout=3)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        bundle = vr.sup.recording.status()["hcpes"]
        points = list(csv.DictReader(open(
            bundle["directory"] + "/points.csv", encoding="utf-8", newline="")))
        raw = [json.loads(line) for line in open(
            bundle["directory"] + "/raw.jsonl", encoding="utf-8")]
        second_qualified = [row for row in raw
                            if row.get("point_index") == 2 and row.get("qualified")]
        final_live_slope = second_qualified[-1]["flags"].get(
            "observed_drift_a_per_min")
        recorded_slope = float(points[1]["observed_drift_a_per_min"])
        c.check("point summary uses the trend through its final accepted reading",
                complete and final_live_slope is not None
                and abs(recorded_slope - final_live_slope) < 1e-12
                and abs(recorded_slope) > 0.001,
                f"recorded={recorded_slope}, final={final_live_slope}")
        c.check("timed parameter display uses the parameter drift profile",
                vr.sup.hcpes["drift_window_s"] == 0.01
                and vr.sup.hcpes["drift_threshold_a_per_min"] == 5.0,
                str({
                    "window": vr.sup.hcpes["drift_window_s"],
                    "threshold": vr.sup.hcpes[
                        "drift_threshold_a_per_min"],
                }))

    c.section("successful recovery repeats the full stability gate")
    async with VirtualReactor() as vr:
        resolved = resolve_plan(plan(
            vr.sup.cfg, grids=(100, 200), recovery=0.5,
            stability_window=0.05, stability_wait=0.3), vr.sup.cfg)
        pulsed = False

        async def recovering_ticks():
            nonlocal pulsed
            while True:
                grid = vr.supplies["grid_bias"].voltage_setpoint or 0
                relay = vr.sup.valve_state["plasma_ground"]
                if grid < 200:
                    current = 0.001
                elif relay:
                    pulsed = True
                    current = 0.0
                else:
                    current = 0.001 if pulsed else 0.0
                vr.instruments["ammeter"].value = current
                await vr.tick()
                await asyncio.sleep(0.005)

        ticker = asyncio.create_task(recovering_ticks())
        try:
            await vr.sup.start_hcpes(
                resolved, "recovered-positive", polarity_confirmed=True)
            c.check("recovered sweep completes", await wait_for(
                lambda: not vr.sup.hcpes_running, timeout=4))
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        bundle = vr.sup.recording.status()["hcpes"]
        rows = list(csv.DictReader(Path(bundle["directory"], "points.csv").open(
            encoding="utf-8", newline="")))
        raw = [json.loads(line) for line in Path(
            bundle["directory"], "raw.jsonl").read_text(
                encoding="utf-8").splitlines()]
        second = rows[1]
        c.check("recovered point is fully qualified after settling",
                second["accessibility"] == "recovered"
                and second["actual_qualified_samples"] == "3"
                and second["settled"] == "True"
                and int(second["recovery_count"]) >= 1, str(second))
        second_phases = [row["phase"] for row in raw if row.get("point_index") == 2]
        c.check("raw stream preserves pulse, recovery, and stability intervals",
                "reignite" in second_phases and "recovery" in second_phases
                and "settle" in second_phases)

    c.section("retry budget and establishment settling are independent")
    async with VirtualReactor() as vr:
        resolved = resolve_plan(plan(
            vr.sup.cfg, recovery=0.07, stability_window=0.05,
            stability_wait=0.25), vr.sup.cfg)
        pulse_count = 0
        prior_relay = False
        first_restored_at = None

        async def flicker_then_recover():
            nonlocal pulse_count, prior_relay, first_restored_at
            loop = asyncio.get_running_loop()
            while True:
                relay = vr.sup.valve_state["plasma_ground"]
                if relay and not prior_relay:
                    pulse_count += 1
                if prior_relay and not relay and pulse_count == 1:
                    first_restored_at = loop.time()
                prior_relay = relay
                if relay or pulse_count == 0:
                    current = 0.0
                elif pulse_count == 1:
                    current = (0.001 if first_restored_at is not None
                               and loop.time() - first_restored_at < 0.025 else 0.0)
                else:
                    current = 0.001
                vr.instruments["ammeter"].value = current
                await vr.tick()
                await asyncio.sleep(0.005)

        ticker = asyncio.create_task(flicker_then_recover())
        try:
            await vr.sup.start_hcpes(
                resolved, "independent-recovery-timers", polarity_confirmed=True)
            complete = await wait_for(lambda: not vr.sup.hcpes_running, timeout=4)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        bundle = vr.sup.recording.status()["hcpes"]
        row = next(csv.DictReader(Path(bundle["directory"], "points.csv").open(
            encoding="utf-8", newline="")))
        manifest = yaml.safe_load(
            Path(bundle["directory"], "manifest.yaml").read_text(
                encoding="utf-8"))
        gates = manifest["initial_plasma"]["stability_gates"]
        c.check("dropout during establishment returns to another reignition pulse",
                complete and pulse_count >= 2
                and row["accessibility"] == "accessible",
                f"pulses={pulse_count}, row={row}")
        c.check("successful relight receives a full gate outside retry time",
                gates[-1]["profile"] == "establishment"
                and gates[-1]["outcome"] == "settled"
                and gates[-1]["elapsed_s"] >= 0.05
                and manifest["initial_plasma"]["settled"] is True
                and row["settled"] == "True",
                str(gates))

    c.section("stability timeout is visible but does not discard the point")
    async with VirtualReactor() as vr:
        resolved = resolve_plan(plan(
            vr.sup.cfg, stability_window=0.05, stability_wait=0.2,
            max_drift=0.000001), vr.sup.cfg)

        async def drifting_ticks():
            current = 0.001
            while True:
                current += 0.000001
                vr.instruments["ammeter"].value = current
                await vr.tick()
                await asyncio.sleep(0.005)

        ticker = asyncio.create_task(drifting_ticks())
        try:
            await vr.sup.start_hcpes(
                resolved, "never-settled", polarity_confirmed=True)
            c.check("never-settled point still completes", await wait_for(
                lambda: not vr.sup.hcpes_running, timeout=3))
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        bundle = vr.sup.recording.status()["hcpes"]
        row = next(csv.DictReader(Path(bundle["directory"], "points.csv").open(
            encoding="utf-8", newline="")))
        manifest = yaml.safe_load(
            Path(bundle["directory"], "manifest.yaml").read_text(
                encoding="utf-8"))
        c.check("initial timeout is visible without mislabelling point one",
                manifest["initial_plasma"]["settled"] is False
                and row["settled"] == "True"
                and row["actual_qualified_samples"] == "3", str(row))
        c.check("operator event names the timeout",
                any("never settled" in event["message"] for event in vr.sup.events))

    c.section("operator stop retains bundle and completes cleanup")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 0.001
        resolved = resolve_plan(plan(
            vr.sup.cfg, settle=0.1, stability_window=0.2,
            stability_wait=0.4, qualified=20), vr.sup.cfg)
        ticker = await autotick(vr, period=0.005)
        try:
            await vr.sup.start_hcpes(
                resolved, "operator-stop", polarity_confirmed=True)
            c.check("pre-start reached hardware", await wait_for(
                lambda: bool(vr.supplies["stage_bias"].voltage_calls), timeout=1))
            await vr.sup.stop_hcpes()
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        bundle = vr.sup.recording.status()["hcpes"]
        manifest = Path(bundle["manifest"]).read_text(encoding="utf-8")
        c.check("stop is final and marks partial bundle aborted",
                vr.sup.hcpes["phase"] == "aborted" and "status: aborted" in manifest)
        c.check("stop performs the full physical cleanup",
                all(mfc.commanded_sccm == 0 for mfc in vr.mfcs.values())
                and all(not vr.supplies[supply].output_on for supply in (
                    "stage_bias", "grid_bias", "collimating", "steering"))
                and not vr.sup.valve_state["ar_pneumatic"])

    c.section("cancelled recording preparation cannot orphan ownership")
    async with VirtualReactor() as vr:
        resolved = resolve_plan(plan(vr.sup.cfg), vr.sup.cfg)
        entered, release = threading.Event(), threading.Event()
        original = vr.sup.recording._start_hcpes_session

        def slow_start(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release HCPES recording start")
            return original(*args)

        vr.sup.recording._start_hcpes_session = slow_start
        pending = asyncio.create_task(vr.sup.start_hcpes(
            resolved, "cancelled-start", polarity_confirmed=True))
        c.check("exclusive ownership begins before disk returns",
                await asyncio.to_thread(entered.wait, 1) and vr.sup.hcpes_running)
        pending.cancel()
        release.set()
        result = await asyncio.gather(pending, return_exceptions=True)
        c.check("cancel is propagated after closing accepted bundle",
                isinstance(result[0], asyncio.CancelledError)
                and not vr.sup.hcpes_running
                and not vr.sup.recording.status()["hcpes"]["active"])

    c.section("hardware-command failure still runs complete cleanup")
    async with VirtualReactor() as vr:
        resolved = resolve_plan(plan(vr.sup.cfg), vr.sup.cfg)
        vr.instruments["ammeter"].value = 0.001

        async def fail_grid(_volts):
            raise OSError("injected grid write failure")

        vr.supplies["grid_bias"].set_voltage = fail_grid
        ticker = await autotick(vr, period=0.005)
        try:
            await vr.sup.start_hcpes(
                resolved, "failed-command", polarity_confirmed=True)
            c.check("failure reaches a terminal state", await wait_for(
                lambda: not vr.sup.hcpes_running, timeout=3))
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        c.check("failure is visible in state and manifest",
                vr.sup.hcpes["phase"] == "failed"
                and "injected grid write failure" in vr.sup.hcpes["error"]
                and vr.sup.recording.status()["hcpes"]["status"] == "failed")
        c.check("failed pre-start leaves no owned output active",
                all(mfc.commanded_sccm == 0 for mfc in vr.mfcs.values())
                and not vr.sup.valve_state["ar_pneumatic"]
                and not vr.sup.valve_state["plasma_ground"]
                and all(not vr.supplies[supply].output_on for supply in (
                    "stage_bias", "grid_bias", "collimating", "steering"))
                and vr.supplies["hv"].hv_off_calls == 1)

    c.section("server shutdown stops HCPES before disconnecting devices")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 0.001
        resolved = resolve_plan(plan(
            vr.sup.cfg, settle=0.1, stability_window=0.2,
            stability_wait=0.4, qualified=20), vr.sup.cfg)
        ticker = await autotick(vr, period=0.005)
        try:
            await vr.sup.start_hcpes(
                resolved, "shutdown-stop", polarity_confirmed=True)
            c.check("shutdown test reaches pre-start", await wait_for(
                lambda: bool(vr.supplies["stage_bias"].voltage_calls), timeout=1))
            receipt = await vr.sup.stop()
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        c.check("shutdown reports HCPES teardown",
                any(step["what"] == "stop HCPES characterization"
                    and step["ok"] for step in receipt["steps"]))
        c.check("shutdown bundle is aborted before recorder closes",
                vr.sup.hcpes["phase"] == "aborted"
                and vr.sup.recording.status()["hcpes"]["status"] == "aborted")
        c.check("shutdown commanded cleanup before disconnection",
                all(mfc.commanded_sccm == 0 for mfc in vr.mfcs.values())
                and all(not vr.supplies[supply].output_on for supply in (
                    "stage_bias", "grid_bias", "collimating", "steering")))

    c.section("polarity reminder is mandatory")
    async with VirtualReactor() as vr:
        resolved = resolve_plan(plan(vr.sup.cfg), vr.sup.cfg)
        c.check("unconfirmed start commands no hardware", await rejects(
            vr.sup.start_hcpes(
                resolved, "unconfirmed", polarity_confirmed=False),
            "confirm the physical stage leads") and not vr.daq.do_writes)
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
