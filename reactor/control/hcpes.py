"""Exclusive HCPES characterization execution.

The controller owns sequencing and an immutable resolved plan.  The Supervisor
remains the only hardware-command boundary; the controller token lets those
public methods distinguish its writes from manual requests while HCPES owns the
controls.
"""
from __future__ import annotations

import asyncio
from collections import Counter, deque
import math
import statistics
from typing import Any, Literal

from .clock import Clock
from .hcpes_model import (
    AR_TARGET,
    ResolvedHcpesPlan,
    StabilityProfile,
    STAGE_TARGET,
    SweepPoint,
)
from ..hcpes_recording import (
    HcpesObservation,
    HcpesPointSummary,
    HcpesStabilityGate,
    NumericChannelSummary,
)
from ..recording import RecordingService


AMMETER_KEY = "inst.ammeter"
AR_VALVE = "ar_pneumatic"
PLASMA_RELAY = "plasma_ground"
HCPES_SUPPLIES = ("stage_bias", "grid_bias", "collimating", "steering")


class HcpesController:
    def __init__(self, host, recording: RecordingService, *, clock: Clock | None = None):
        self.host = host
        self.recording = recording
        self.clock = clock or Clock()
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._started_elapsed = 0.0
        self._last_current_seq = 0
        self._live_drift_samples: deque[tuple[float, float]] = deque()
        self._applied_setpoints: dict[str, float] = {}
        self.resolved: ResolvedHcpesPlan | None = None
        self.state: dict[str, Any] = {
            "running": False, "phase": "idle", "cleanup_receipts": [],
        }

    @property
    def running(self) -> bool:
        return bool(self.state.get("running"))

    async def start(
        self,
        resolved: ResolvedHcpesPlan,
        session_id: str,
        *,
        polarity_confirmed: bool,
    ) -> None:
        async with self._start_lock:
            if self.running or (self.task is not None and not self.task.done()):
                raise RuntimeError("HCPES characterization is already running")
            if not polarity_confirmed:
                sign = "+" if resolved.plan.stage_polarity > 0 else "-"
                raise RuntimeError(
                    f"confirm the physical stage leads are in the {sign} orientation")
            conflict = self.host.hcpes_admission_conflict()
            if conflict:
                raise RuntimeError(conflict)
            started_at = self.clock.wall()
            self.resolved = resolved
            self.stop_event.clear()
            self._started_elapsed = self.clock.elapsed()
            self._last_current_seq = int(
                self.host.snapshot_seq.get(AMMETER_KEY, 0) or 0)
            self.state = {
                "running": True,
                "phase": "preparing recording",
                "session_id": session_id,
                "plan_id": resolved.plan.id,
                "plan_name": resolved.plan.name,
                "plan_signature": resolved.compatibility_signature,
                "polarity": resolved.plan.stage_polarity,
                "point_index": 0,
                "point_total": resolved.point_count,
                "points_completed": 0,
                "points_collected": 0,
                "points_rejected": 0,
                "progress_fraction": 0.0,
                "elapsed_s": 0.0,
                "estimated_remaining_s": None,
                "qualified_samples": 0,
                "qualified_target": resolved.plan.settings.qualified_samples,
                "settled": None,
                "observed_drift_a_per_min": None,
                "drift_window_s": (
                    resolved.plan.settings.establishment.trend_window_s),
                "drift_threshold_a_per_min": (
                    resolved.plan.settings.establishment.max_drift_a_per_min),
                "current_a": None,
                "setpoints": {},
                "applied_setpoints": {},
                "changed_target": None,
                "requested_value": None,
                "settle_remaining_s": None,
                "recovery_remaining_s": None,
                "active_stability_profile": None,
                "stability_window_elapsed_s": None,
                "stability_window_required_s": None,
                "stability_wait_elapsed_s": None,
                "stability_max_wait_s": None,
                "recovery_count": 0,
                "inaccessible_points": 0,
                "initial_settled": None,
                "initial_stability_gates": [],
                "cleanup_receipts": [],
                "error": "",
            }
            prepare = asyncio.create_task(self.recording.start_hcpes_session(
                resolved, session_id, started_at))
            try:
                await asyncio.shield(prepare)
            except BaseException as exc:
                # The recording worker may have accepted the open even if an
                # HTTP caller disappears. Collect it and close any bundle it
                # created before releasing exclusive ownership.
                try:
                    await prepare
                    await self.recording.finish_hcpes_session(
                        "aborted" if isinstance(exc, asyncio.CancelledError)
                        else "failed",
                        self.clock.wall(),
                        f"start did not complete: {type(exc).__name__}: {exc}")
                finally:
                    self.state.update(
                        running=False, phase="aborted" if isinstance(
                            exc, asyncio.CancelledError) else "failed",
                        error=str(exc))
                raise
            self.state["phase"] = "starting"
            self._live_drift_samples.clear()
            self._applied_setpoints = {}
            self.task = asyncio.create_task(
                self._run(), name=f"hcpes-{session_id}")
            self.host.report_event(
                "hcpes", f"HCPES characterization started: {resolved.plan.name}")

    async def stop(self) -> None:
        self.stop_event.set()
        task = self.task
        if task is not None and not task.done():
            await asyncio.shield(task)

    async def shutdown(self) -> None:
        await self.stop()

    async def _run(self) -> None:
        resolved = self.resolved
        if resolved is None:
            return
        outcome: Literal["complete", "aborted", "failed"] = "complete"
        error = ""
        previous: dict[str, float] = {}
        try:
            previous = await self._restricted_prestart()
            await self._settle_initial_plasma()
            for point in resolved.iter_points():
                if self.stop_event.is_set():
                    outcome = "aborted"
                    break
                self.state.update(
                    phase="setting condition", point_index=point.index,
                    setpoints=point.setpoints, qualified_samples=0,
                    settled=None, observed_drift_a_per_min=None,
                    recovery_count=0, changed_target=None,
                    requested_value=None, settle_remaining_s=None,
                    recovery_remaining_s=None, active_stability_profile=None,
                    stability_window_elapsed_s=None,
                    stability_window_required_s=None,
                    stability_wait_elapsed_s=None, stability_max_wait_s=None,
                )
                previous, changed = await self._apply_point(point, previous)
                if self.stop_event.is_set():
                    outcome = "aborted"
                    break
                stability_profile: Literal[
                    "establishment", "parameter_change",
                ] | None = None
                if (changed
                        and resolved.plan.settings.condition_settle_mode == "current"):
                    stability_profile = "parameter_change"
                await self._acquire_point(
                    point, stability_profile=stability_profile)
            if self.stop_event.is_set() and outcome == "complete":
                outcome = "aborted"
        except asyncio.CancelledError:
            outcome = "aborted"
            error = "controller task cancelled"
        except Exception as exc:
            outcome = "failed"
            error = f"{type(exc).__name__}: {exc}"
            self.state["error"] = error
            self.host.report_event("error", f"HCPES failed: {error}")
        finally:
            self.state["phase"] = "cleanup"
            cleanup_errors = await self._cleanup()
            if cleanup_errors and outcome == "complete":
                outcome = "failed"
            if cleanup_errors:
                tail = "; ".join(cleanup_errors)
                error = "; ".join(part for part in (error, tail) if part)
            try:
                await self.recording.finish_hcpes_session(
                    outcome, self.clock.wall(), error)
            except Exception as exc:
                self.host.report_event(
                    "error", f"HCPES recording close failed: {type(exc).__name__}: {exc}")
                outcome = "failed"
                error = "; ".join(
                    part for part in (error, f"recording close: {exc}") if part)
            self.state.update(
                running=False, phase=outcome, error=error,
                ended_at=self.clock.wall())
            self.host.report_event(
                "hcpes", f"HCPES characterization {outcome}"
                + (f": {error}" if error else ""))

    async def _restricted_prestart(self) -> dict[str, float]:
        """No precursor fill: zero gases, configure supplies, open only Ar."""
        assert self.resolved is not None
        self.state["phase"] = "HCPES pre-start: zeroing all MFCs"
        for mfc_id in self.host.mfcs:
            await self.host.set_mfc_setpoint(mfc_id, 0.0, _owner=self)
            self._event_observation("setup", f"zeroed mfc:{mfc_id}")
        self._raise_if_stopped()
        await self.host.set_valve(
            AR_VALVE, True, reason="HCPES pre-start", _owner=self)
        self._event_observation("setup", "opened Ar isolation valve")
        self.state["phase"] = "HCPES pre-start: programming initial condition"
        initial = self.resolved.initial_setpoints
        # Program the dedicated plasma-establishment condition before
        # energising outputs.  There is no
        # per-axis delay here: plasma does not exist yet, so waiting after each
        # command only makes startup look hung.  The full current stability
        # gate begins immediately after the supplies and beam are enabled.
        for axis in self.resolved.plan.axes:
            self._raise_if_stopped()
            if axis.locked:
                continue
            value = initial[axis.target]
            await self._set_target(axis.target, value, initial=True)
            self._event_observation(
                "initial_parameter", f"set initial {axis.target} to {value:g}",
                changed_target=axis.target, requested_value=value,
                actual_value=value)
            self._applied_setpoints[axis.target] = value
            self.state.update(
                applied_setpoints=dict(self._applied_setpoints),
                changed_target=axis.target, requested_value=value)
        self._raise_if_stopped()
        self.state["phase"] = "HCPES pre-start: enabling support supplies"
        for supply_id in HCPES_SUPPLIES:
            await self.host.set_supply_output(supply_id, True, _owner=self)
            self._event_observation("setup", f"enabled supply:{supply_id}")
        self._raise_if_stopped()
        self.state["phase"] = "HCPES pre-start: establishing plasma"
        await self.host.set_valve(
            PLASMA_RELAY, False, reason="HCPES pre-start - beam on", _owner=self)
        self._event_observation("setup", "parked plasma relay in beam-on state")
        self._applied_setpoints = dict(initial)
        self.state.update(
            phase="initial plasma stability", changed_target=None,
            requested_value=None,
            setpoints=dict(initial),
            applied_setpoints=dict(self._applied_setpoints))
        return dict(initial)

    async def _settle_initial_plasma(self) -> None:
        """Establish plasma at the plan's non-acquisition startup condition."""
        exclusions: Counter[str] = Counter()
        gates: list[HcpesStabilityGate] = []
        result = await self._wait_stability(None, "establishment")
        gates.append(result)
        settled = result.outcome == "settled"
        if result.outcome == "plasma_lost":
            recovered, settled, _slope, _attempts = await self._recover(
                None, exclusions, gates)
            if not recovered:
                self.state.update(
                    initial_settled=False,
                    initial_stability_gates=[
                        gate.model_dump(mode="json") for gate in gates],
                )
                raise RuntimeError(
                    "initial plasma could not be established within the "
                    "recovery retry window")
        self.state.update(
            initial_settled=settled,
            initial_stability_gates=[
                gate.model_dump(mode="json") for gate in gates],
        )
        self._event_observation(
            "setup",
            "initial plasma settled" if settled
            else "initial plasma stability timed out; continuing flagged",
            flags={
                "initial_settled": settled,
                "stability_gates": [
                    gate.model_dump(mode="json") for gate in gates],
            },
        )

    def _raise_if_stopped(self) -> None:
        if self.stop_event.is_set():
            raise asyncio.CancelledError

    async def _apply_point(
        self, point: SweepPoint, previous: dict[str, float],
    ) -> tuple[dict[str, float], bool]:
        assert self.resolved is not None
        current = point.setpoints
        changed = False
        for axis in self.resolved.plan.axes:
            if axis.locked:
                continue
            value = current[axis.target]
            if previous.get(axis.target) == value:
                continue
            changed = True
            self.state.update(
                phase=f"setting {axis.target}", changed_target=axis.target,
                requested_value=value, settle_remaining_s=None)
            await self._set_target(axis.target, value)
            self._applied_setpoints[axis.target] = value
            self.state["applied_setpoints"] = dict(self._applied_setpoints)
            self._reset_live_drift()
            parameter_profile = self.resolved.plan.settings.parameter_change
            self.state.update(
                drift_window_s=parameter_profile.trend_window_s,
                drift_threshold_a_per_min=(
                    parameter_profile.max_drift_a_per_min),
            )
            self._event_observation(
                "parameter_change", f"set {axis.target} to {value:g}", point,
                changed_target=axis.target, requested_value=value,
                actual_value=value)
            if self.resolved.plan.settings.condition_settle_mode == "time":
                self.state["phase"] = f"timed settle after {axis.target}"
                self.state["active_stability_profile"] = "timed_parameter_change"
                await self._observe_for(
                    self.resolved.plan.settings.parameter_settle_s,
                    "settle", f"timed settle after {axis.target} change", point,
                    exclusion="parameter_settle", track_remaining=True)
            if self.stop_event.is_set():
                break
        self.state.update(changed_target=None, requested_value=None,
                          settle_remaining_s=None,
                          active_stability_profile=None)
        return current, changed

    async def _set_target(self, target: str, value: float, *, initial: bool = False) -> None:
        kind, device_id = target.split(":", 1)
        if kind == "mfc":
            await self.host.set_mfc_setpoint(device_id, value, _owner=self)
        elif target == STAGE_TARGET:
            if initial:
                await self.host.arm_sample_bias(
                    device_id, volts=value,
                    polarity=self.resolved.plan.stage_polarity,
                    reason="HCPES pre-start", _owner=self)
            else:
                await self.host.set_supply_voltage(device_id, value, _owner=self)
        elif device_id == "grid_bias":
            await self.host.set_supply_voltage(device_id, value, _owner=self)
        else:
            await self.host.set_supply_current(device_id, value, _owner=self)

    async def _acquire_point(
        self,
        point: SweepPoint,
        *,
        stability_profile: Literal["establishment", "parameter_change"] | None,
    ) -> None:
        assert self.resolved is not None
        settings = self.resolved.plan.settings
        point_started_elapsed_s = max(
            0.0, self.clock.elapsed() - self._started_elapsed)
        values: list[float] = []
        qualified_measurements: list[dict[str, Any]] = []
        exclusions: Counter[str] = Counter()
        recovered = False
        all_settled = True
        latest_slope: float | None = None
        recoveries = 0
        stability_gates: list[HcpesStabilityGate] = []

        if stability_profile is not None:
            result = await self._wait_stability(point, stability_profile)
            stability_gates.append(result)
            latest_slope = result.observed_drift_a_per_min
            if result.outcome == "plasma_lost":
                recovered_now, settled, slope, attempts = await self._recover(
                    point, exclusions, stability_gates)
                recoveries += attempts
                recovered |= recovered_now
                all_settled &= settled
                latest_slope = slope
                if not recovered_now:
                    await self._finish_point(
                        point, point_started_elapsed_s, values,
                        qualified_measurements, exclusions, all_settled,
                        latest_slope, recoveries, "inaccessible", stability_gates)
                    return
            else:
                # A plasma loss is not a stability timeout. Only a gate that
                # reaches its deadline while plasma remains present sets the
                # analysis-visible never-settled flag.
                all_settled &= result.outcome == "settled"
        else:
            self.state.update(phase="collecting qualified samples", settled=True)
            latest_slope = self.state.get("observed_drift_a_per_min")

        self.state["phase"] = "collecting qualified samples"
        self._clear_live_timers()
        while len(values) < settings.qualified_samples and not self.stop_event.is_set():
            current = await self._next_current()
            if current is None:
                continue
            if abs(current) < settings.plasma_min_current_a:
                exclusions["plasma_loss"] += 1
                self._observation(
                    current, "plasma_loss", "current below HCPES plasma minimum",
                    point, exclusion="plasma_loss")
                ok, settled, slope, attempts = await self._recover(
                    point, exclusions, stability_gates)
                recoveries += attempts
                recovered |= ok
                all_settled &= settled
                latest_slope = slope
                if not ok:
                    accessibility = "partial" if values else "inaccessible"
                    await self._finish_point(
                        point, point_started_elapsed_s, values,
                        qualified_measurements, exclusions, all_settled,
                        latest_slope, recoveries, accessibility, stability_gates)
                    return
                self._clear_live_timers()
                continue
            values.append(current)
            index = len(values)
            qualified_measurements.append(self._observation(
                current, "acquire", "qualified stage-current sample", point,
                qualified=True, qualified_index=index))
            self.state["qualified_samples"] = index

        accessibility = "recovered" if recovered else "accessible"
        if len(values) < settings.qualified_samples:
            accessibility = "partial"
        # The live trend keeps updating while the qualified readings arrive.
        # Preserve the trend through the actual collection interval rather than
        # the stale value captured immediately before collection began.
        collection_slope = self.state.get("observed_drift_a_per_min")
        if _number(collection_slope) is not None:
            latest_slope = float(collection_slope)
        await self._finish_point(
            point, point_started_elapsed_s, values, qualified_measurements,
            exclusions, all_settled, latest_slope, recoveries, accessibility,
            stability_gates)

    def _clear_live_timers(self) -> None:
        self.state.update(
            active_stability_profile=None,
            settle_remaining_s=None, recovery_remaining_s=None,
            stability_window_elapsed_s=None,
            stability_window_required_s=None,
            stability_wait_elapsed_s=None, stability_max_wait_s=None)

    async def _wait_stability(
        self,
        point: SweepPoint | None,
        profile_name: Literal["establishment", "parameter_change"],
    ) -> HcpesStabilityGate:
        assert self.resolved is not None
        settings = self.resolved.plan.settings
        profile: StabilityProfile = (
            settings.establishment
            if profile_name == "establishment"
            else settings.parameter_change
        )
        started = self.clock.elapsed()
        end = started + profile.maximum_wait_s
        samples: deque[tuple[float, float]] = deque()
        stable_since: float | None = None
        stable_elapsed = 0.0
        latest_slope = None
        latest_current = _number(self.host.snapshot.get(AMMETER_KEY))
        label = (
            "plasma establishment stability"
            if profile_name == "establishment"
            else "parameter-change stability"
        )
        self._reset_live_drift()
        self.state.update(
            phase=label, settled=None,
            active_stability_profile=profile_name,
            drift_threshold_a_per_min=profile.max_drift_a_per_min,
            drift_window_s=profile.trend_window_s,
            stability_window_elapsed_s=0.0,
            stability_window_required_s=profile.stable_window_s,
            stability_wait_elapsed_s=0.0,
            stability_max_wait_s=profile.maximum_wait_s,
            settle_remaining_s=profile.maximum_wait_s,
        )
        while not self.stop_event.is_set() and self.clock.elapsed() < end:
            current = await self._next_current(deadline=end)
            if current is None:
                continue
            latest_current = current
            now = self.clock.elapsed()
            waited = max(0.0, now - started)
            lit = abs(current) >= settings.plasma_min_current_a
            if lit:
                samples.append((now, current))
                while (len(samples) > 1
                       and now - samples[1][0] >= profile.trend_window_s):
                    samples.popleft()
            span = now - samples[0][0] if samples else 0.0
            if lit and len(samples) >= 2 and span >= profile.trend_window_s:
                latest_slope = _robust_slope_a_per_min(samples)
                self.state["observed_drift_a_per_min"] = latest_slope
                if abs(latest_slope) < profile.max_drift_a_per_min:
                    if stable_since is None:
                        # The trend window establishes that the rate is now
                        # acceptable.  Required stable time begins here; its
                        # history is not back-credited, so the operator sees the
                        # timer advance from zero for the full requested hold.
                        stable_since = now
                    stable_elapsed = max(0.0, now - stable_since)
                else:
                    stable_since = None
                    stable_elapsed = 0.0
            self.state.update(
                stability_window_elapsed_s=min(
                    stable_elapsed, profile.stable_window_s),
                stability_wait_elapsed_s=waited,
                settle_remaining_s=max(0.0, end - now),
            )
            flags = {
                "plasma_present": lit,
                "stability_profile": profile_name,
                "settle_remaining_s": max(0.0, end - now),
                "trend_window_s": profile.trend_window_s,
                "stable_window_elapsed_s": min(
                    stable_elapsed, profile.stable_window_s),
                "stable_window_required_s": profile.stable_window_s,
                "max_drift_a_per_min": profile.max_drift_a_per_min,
            }
            if self.state.get("recovery_remaining_s") is not None:
                flags["retry_remaining_s"] = self.state["recovery_remaining_s"]
            self._observation(
                current, "settle", label, point,
                exclusion="stability_window", flags=flags)
            if not lit:
                self.state.update(settled=False, settle_remaining_s=None)
                return HcpesStabilityGate(
                    profile=profile_name, outcome="plasma_lost", elapsed_s=waited,
                    trend_window_s=profile.trend_window_s,
                    stable_window_s=profile.stable_window_s,
                    maximum_wait_s=profile.maximum_wait_s,
                    max_drift_a_per_min=profile.max_drift_a_per_min,
                    observed_drift_a_per_min=latest_slope,
                )
            if stable_elapsed >= profile.stable_window_s:
                if latest_slope is not None:
                    elapsed = max(0.0, now - started)
                    self.state.update(settled=True, settle_remaining_s=0.0)
                    return HcpesStabilityGate(
                        profile=profile_name, outcome="settled", elapsed_s=elapsed,
                        trend_window_s=profile.trend_window_s,
                        stable_window_s=profile.stable_window_s,
                        maximum_wait_s=profile.maximum_wait_s,
                        max_drift_a_per_min=profile.max_drift_a_per_min,
                        observed_drift_a_per_min=latest_slope,
                    )
        elapsed = max(0.0, self.clock.elapsed() - started)
        self.state.update(
            settled=False, observed_drift_a_per_min=latest_slope,
            settle_remaining_s=0.0)
        subject = f"point {point.index}" if point else "initial plasma"
        self.host.report_event(
            "flag", f"HCPES {subject}: {profile_name.replace('_', ' ')} "
            "current never settled; continuing")
        lit = (latest_current is not None
               and abs(latest_current) >= settings.plasma_min_current_a)
        return HcpesStabilityGate(
            profile=profile_name,
            outcome="timeout" if lit else "plasma_lost",
            elapsed_s=elapsed,
            trend_window_s=profile.trend_window_s,
            stable_window_s=profile.stable_window_s,
            maximum_wait_s=profile.maximum_wait_s,
            max_drift_a_per_min=profile.max_drift_a_per_min,
            observed_drift_a_per_min=latest_slope,
        )

    async def _recover(
        self,
        point: SweepPoint | None,
        exclusions: Counter[str],
        stability_gates: list[HcpesStabilityGate],
    ) -> tuple[bool, bool, float | None, int]:
        assert self.resolved is not None
        settings = self.resolved.plan.settings
        remaining = settings.recovery_window_s
        attempts = 0
        self.state.update(
            phase="recovering plasma", active_stability_profile=None,
            recovery_remaining_s=remaining, settle_remaining_s=None)
        while not self.stop_event.is_set() and remaining > 0:
            attempts += 1
            self.state["recovery_count"] = int(
                self.state.get("recovery_count", 0)) + 1
            retry_deadline = self.clock.elapsed() + remaining
            await self.host.set_valve(
                PLASMA_RELAY, True, reason="HCPES reignite pulse", _owner=self)
            self._event_observation(
                "reignite", f"reignite attempt {attempts}: plasma-ground pulse",
                point, exclusion_reason="reignite",
                flags={"retry_remaining_s": remaining})
            exclusions["reignite"] += await self._observe_for(
                settings.reignite_pulse_s, "reignite", "plasma-ground pulse",
                point, exclusion="reignite", countdown_key="recovery_remaining_s",
                limit_deadline=retry_deadline)
            await self.host.set_valve(
                PLASMA_RELAY, False, reason="HCPES recovery - beam on", _owner=self)
            remaining = max(0.0, retry_deadline - self.clock.elapsed())
            self._event_observation(
                "recovery", f"reignite attempt {attempts}: beam restored",
                point, exclusion_reason="reignite_settle",
                flags={"retry_remaining_s": remaining})
            exclusions["reignite_settle"] += await self._observe_for(
                settings.reignite_settle_s, "recovery", "post-pulse settle",
                point, exclusion="reignite_settle",
                countdown_key="recovery_remaining_s",
                limit_deadline=retry_deadline)
            remaining = max(0.0, retry_deadline - self.clock.elapsed())
            self.state["recovery_remaining_s"] = remaining
            current = _number(self.host.snapshot.get(AMMETER_KEY))
            if current is None or abs(current) < settings.plasma_min_current_a:
                continue
            result = await self._wait_stability(point, "establishment")
            stability_gates.append(result)
            if result.outcome != "plasma_lost":
                self.state["recovery_remaining_s"] = None
                return (
                    True,
                    result.outcome == "settled",
                    result.observed_drift_a_per_min,
                    attempts,
                )
            self.state.update(
                phase="recovering plasma", active_stability_profile=None,
                recovery_remaining_s=remaining, settle_remaining_s=None)
        if point is not None:
            self.state["inaccessible_points"] += 1
        self.state["recovery_remaining_s"] = 0.0
        self._event_observation(
            "inaccessible",
            "condition did not recover within retry window" if point
            else "initial plasma did not recover within retry window",
            point,
            exclusion_reason="recovery_exhausted")
        if point is not None:
            self.host.report_event(
                "flag", f"HCPES point {point.index} inaccessible; continuing sweep")
        else:
            self.host.report_event(
                "error", "HCPES initial plasma inaccessible; ending characterization")
        return False, False, None, attempts

    async def _finish_point(
        self,
        point: SweepPoint,
        started_elapsed_s: float,
        values: list[float],
        qualified_measurements: list[dict[str, Any]],
        exclusions: Counter[str],
        settled: bool,
        slope: float | None,
        recoveries: int,
        accessibility: Literal["accessible", "recovered", "inaccessible", "partial"],
        stability_gates: list[HcpesStabilityGate],
    ) -> None:
        assert self.resolved is not None
        if values:
            mean = statistics.fmean(values)
            stddev = statistics.stdev(values) if len(values) > 1 else 0.0
            minimum, maximum = min(values), max(values)
        else:
            mean = stddev = minimum = maximum = None
        summary = HcpesPointSummary(
            point_index=point.index,
            started_elapsed_s=started_elapsed_s,
            ended_elapsed_s=max(
                started_elapsed_s,
                self.clock.elapsed() - self._started_elapsed),
            setpoints=point.setpoints,
            signed_stage_bias_v=point.signed_stage_bias_v,
            requested_qualified_samples=self.resolved.plan.settings.qualified_samples,
            actual_qualified_samples=len(values),
            excluded_counts=dict(exclusions),
            stage_current_mean_a=mean, stage_current_stddev_a=stddev,
            stage_current_min_a=minimum, stage_current_max_a=maximum,
            settled=settled, observed_drift_a_per_min=slope,
            accessibility=accessibility, recovery_count=recoveries,
            stability_gates=stability_gates,
            measurement_stats=_numeric_channel_stats(qualified_measurements),
        )
        if not self.recording.submit_hcpes_point(summary):
            raise RuntimeError("recording backlog refused HCPES point summary")
        completed = int(self.state.get("points_completed", 0)) + 1
        rejected = int(self.state.get("points_rejected", 0))
        collected = int(self.state.get("points_collected", 0))
        if accessibility in {"inaccessible", "partial"}:
            rejected += 1
        else:
            collected += 1
        total = self.resolved.point_count
        elapsed = max(0.0, self.clock.elapsed() - self._started_elapsed)
        fraction = completed / total if total else 1.0
        remaining = (elapsed * (1.0 - fraction) / fraction) if fraction > 0 else None
        self.state.update(
            points_completed=completed, points_collected=collected,
            points_rejected=rejected, progress_fraction=fraction,
            elapsed_s=elapsed, estimated_remaining_s=remaining,
        )

    async def _observe_for(
        self,
        seconds: float,
        phase: str,
        reason: str,
        point: SweepPoint | None,
        *,
        exclusion: str,
        track_remaining: bool = False,
        countdown_key: str | None = None,
        limit_deadline: float | None = None,
    ) -> int:
        end = self.clock.elapsed() + seconds
        if limit_deadline is not None:
            end = min(end, limit_deadline)
        observed = 0
        key = countdown_key or ("settle_remaining_s" if track_remaining else None)
        countdown_end = limit_deadline if countdown_key and limit_deadline is not None else end
        if key:
            self.state[key] = max(0.0, countdown_end - self.clock.elapsed())
        while not self.stop_event.is_set() and self.clock.elapsed() < end:
            remaining = max(0.0, countdown_end - self.clock.elapsed())
            if key:
                self.state[key] = remaining
            current = await self._next_current(deadline=end)
            if current is not None:
                observed += 1
                flags = {key: remaining} if key else None
                self._observation(
                    current, phase, reason, point, exclusion=exclusion,
                    flags=flags)
        if key:
            self.state[key] = max(0.0, countdown_end - self.clock.elapsed())
        return observed

    async def _next_current(self, *, deadline: float | None = None) -> float | None:
        while not self.stop_event.is_set():
            seq = int(self.host.snapshot_seq.get(AMMETER_KEY, 0) or 0)
            if seq > self._last_current_seq:
                self._last_current_seq = seq
                current = _number(self.host.snapshot.get(AMMETER_KEY))
                if current is not None:
                    self._update_live_drift(current)
                return current
            if deadline is not None and self.clock.elapsed() >= deadline:
                return None
            timeout = 0.05
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - self.clock.elapsed()))
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return None

    def _reset_live_drift(self) -> None:
        self._live_drift_samples.clear()
        self.state["observed_drift_a_per_min"] = None

    def _update_live_drift(self, current: float) -> None:
        """Maintain a robust rolling trend for telemetry in every run phase."""
        assert self.resolved is not None
        now = self.clock.elapsed()
        elapsed = max(0.0, now - self._started_elapsed)
        self._live_drift_samples.append((now, current))
        window = float(
            self.state.get("drift_window_s")
            or self.resolved.plan.settings.establishment.trend_window_s)
        while (len(self._live_drift_samples) > 1
               and now - self._live_drift_samples[1][0] >= window):
            self._live_drift_samples.popleft()
        span = now - self._live_drift_samples[0][0]
        slope = self.state.get("observed_drift_a_per_min")
        if len(self._live_drift_samples) >= 2 and span >= window:
            slope = _robust_slope_a_per_min(self._live_drift_samples)
        self.state.update(
            current_a=current,
            observed_drift_a_per_min=slope,
            elapsed_s=elapsed,
        )

    def _event_observation(
        self,
        phase: str,
        reason: str,
        point: SweepPoint | None = None,
        **kwargs,
    ) -> None:
        self._write_observation(
            phase=phase, reason=reason, point=point,
            current=_number(self.host.snapshot.get(AMMETER_KEY)), **kwargs)

    def _observation(
        self,
        current: float,
        phase: str,
        reason: str,
        point: SweepPoint | None,
        *,
        qualified: bool = False,
        qualified_index: int | None = None,
        exclusion: str | None = None,
        flags: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        observation_flags = dict(flags or {})
        slope = _number(self.state.get("observed_drift_a_per_min"))
        if slope is not None:
            observation_flags.setdefault("observed_drift_a_per_min", slope)
        return self._write_observation(
            phase=phase, reason=reason, point=point, current=current,
            qualified=qualified, qualified_index=qualified_index,
            exclusion_reason=exclusion, flags=observation_flags)

    def _write_observation(
        self,
        *,
        phase: str,
        reason: str,
        point: SweepPoint | None,
        current: float | None,
        qualified: bool = False,
        qualified_index: int | None = None,
        exclusion_reason: str | None = None,
        flags: dict[str, Any] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        measurements = dict(self.host.snapshot)
        if current is not None:
            measurements[AMMETER_KEY] = current
        observation = HcpesObservation(
            point_index=point.index if point else None,
            captured_at=self.clock.wall(),
            elapsed_s=max(0.0, self.clock.elapsed() - self._started_elapsed),
            phase=phase, reason=reason,
            setpoints=point.setpoints if point else {},
            measurements=measurements,
            qualified=qualified, qualified_index=qualified_index,
            exclusion_reason=exclusion_reason,
            flags=flags or {}, **kwargs,
        )
        if not self.recording.submit_hcpes_observation(observation):
            raise RuntimeError("recording backlog refused HCPES observation")
        return measurements

    async def _cleanup(self) -> list[str]:
        receipts: list[dict[str, Any]] = []

        async def attempt(what: str, awaitable) -> None:
            try:
                result = await awaitable
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                receipts.append({"what": what, "ok": False, "detail": detail})
            else:
                failed = (
                    [row for row in result
                     if isinstance(row, dict) and not row.get("ok")]
                    if isinstance(result, list) else [])
                detail = "; ".join(
                    f"{row.get('id', '?')}: {row.get('detail', 'failed')}"
                    for row in failed)
                receipts.append({"what": what, "ok": not failed,
                                 "detail": detail, "result": result})
            try:
                row = receipts[-1]
                self._event_observation(
                    "cleanup", what, flags={"ok": row["ok"],
                                             "detail": row["detail"]})
            except Exception:
                # Disk trouble is already latched by RecordingService and must
                # never prevent the remaining physical cleanup commands.
                pass

        for mfc_id in self.host.mfcs:
            await attempt(
                f"zero MFC {mfc_id}",
                self.host.set_mfc_setpoint(mfc_id, 0.0, _owner=self))
            if receipts[-1]["ok"]:
                self._applied_setpoints[f"mfc:{mfc_id}"] = 0.0
                self.state["applied_setpoints"] = dict(self._applied_setpoints)
        await attempt(
            "close Ar isolation",
            self.host.set_valve(
                AR_VALVE, False, reason="HCPES cleanup", _owner=self))
        await attempt("command HV off", self.host.hv_off(reason="HCPES cleanup"))
        for supply_id in HCPES_SUPPLIES:
            await attempt(
                f"switch off {supply_id}",
                self.host.set_supply_output(supply_id, False, _owner=self))
            if receipts[-1]["ok"]:
                # Output-off deliberately preserves the supply's programmed
                # setpoint. Remove it from this run-owned display rather than
                # implying either an energized value or an uncommanded zero.
                self._applied_setpoints.pop(f"supply:{supply_id}", None)
                self.state["applied_setpoints"] = dict(self._applied_setpoints)
        await attempt(
            "park plasma relay",
            self.host.set_valve(
                PLASMA_RELAY, False, reason="HCPES cleanup - relay at rest",
                _owner=self))
        self.state["cleanup_receipts"] = receipts
        errors = [f"{row['what']}: {row['detail']}" for row in receipts if not row["ok"]]
        if errors:
            self.host.report_event("error", "HCPES cleanup: " + "; ".join(errors))
        return errors


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _numeric_channel_stats(
    snapshots: list[dict[str, Any]],
) -> dict[str, NumericChannelSummary]:
    """Summarize every finite numeric channel present in qualified snapshots."""
    values: dict[str, list[float]] = {}
    for snapshot in snapshots:
        for key, raw in snapshot.items():
            value = _number(raw)
            if value is not None:
                values.setdefault(key, []).append(value)
    return {
        key: NumericChannelSummary(
            count=len(channel_values),
            mean=statistics.fmean(channel_values),
            stddev=(statistics.stdev(channel_values)
                    if len(channel_values) > 1 else 0.0),
            minimum=min(channel_values),
            maximum=max(channel_values),
        )
        for key, channel_values in sorted(values.items())
    }


def _robust_slope_a_per_min(samples) -> float:
    """Return a long-baseline Theil-Sen current trend in A/min.

    The former endpoint-median calculation changed discontinuously whenever a
    noisy sample entered or left either endpoint group.  That produced the
    reported flat-looking rate followed by a spike.  The median of pairwise
    slopes is resistant to individual DMM excursions and evolves smoothly as
    readings arrive.  Ignoring pairs shorter than a quarter-window (or 0.5 s)
    prevents high-frequency measurement noise from being extrapolated to an
    enormous per-minute rate while retaining a genuine window-scale trend.
    """
    rows = list(samples)
    if len(rows) < 2:
        return 0.0
    span = rows[-1][0] - rows[0][0]
    if span <= 1e-9:
        return 0.0
    minimum_baseline = min(span, max(0.5, span / 4.0))
    slopes = [
        (later_current - earlier_current) / elapsed * 60.0
        for index, (earlier_time, earlier_current) in enumerate(rows)
        for later_time, later_current in rows[index + 1:]
        if (elapsed := later_time - earlier_time) >= minimum_baseline
    ]
    if not slopes:
        return (rows[-1][1] - rows[0][1]) / span * 60.0
    return statistics.median(slopes)
