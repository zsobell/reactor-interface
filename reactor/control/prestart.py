"""Capability-driven user-authored pre-start execution.

All hardware commands still go through the owning Supervisor. The controller
owns one immutable recipe snapshot, progress, cancellation, primed state, and
its explicit abort sequence.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from .clock import Clock
from .prestart_model import (
    ResolvedPrestart,
    ResolvedStep,
    target_device_id,
    target_read_key,
)
from .prestart_store import PrestartRecipeStore


Handler = Callable[[ResolvedStep], Awaitable[None]]


class PrestartController:
    def __init__(self, supervisor, *, store: PrestartRecipeStore,
                 clock: Clock | None = None):
        self.clock = clock or Clock()
        self.sup = supervisor
        self.store = store
        self.state: dict[str, Any] = {
            "state": "idle", "running": False, "done": False, "primed": False,
            "cleanup_available": False,
        }
        self.params: dict[str, Any] = {}
        self.task: asyncio.Task | None = None
        # Abort is a session-owned operation.  HTTP callers may disappear or
        # overlap, but one task keeps cleanup alive and all callers observe the
        # same terminal result.
        self.abort_task: asyncio.Task | None = None
        self._session_generation = 0
        self.stop_event = asyncio.Event()
        self.session: ResolvedPrestart | None = None
        self._grounded_plasma_switches: set[str] = set()
        self._receipts: list[dict[str, Any]] = []
        self._handlers: dict[str, Handler] = {
            "delay.wait": self._delay,
            "valve.open": self._valve_open,
            "valve.close": self._valve_close,
            "mfc.start_flow": self._mfc_start,
            "mfc.stop_flow": self._mfc_stop,
            "mfc.wait_flow": self._wait_until,
            "supply.output_on": self._supply_on,
            "supply.output_off": self._supply_off,
            "supply.set_voltage": self._supply_voltage,
            "supply.set_current": self._supply_current,
            "supply.arm_bias": self._arm_bias,
            "supply.wait_voltage": self._wait_until,
            "supply.wait_current": self._wait_until,
            "hv.off": self._hv_off,
            "fill.start": self._fill_start,
            "fill.stop": self._fill_stop,
            "plasma.strike_hold": self._strike_hold,
            "sensor.wait_until": self._wait_until,
        }

    async def start(self, params: dict, *, recipe_id: str | None = None,
                    expected_revision: int | None = None) -> None:
        if self.abort_task is not None and not self.abort_task.done():
            raise RuntimeError("pre-start cleanup is still in progress")
        if self.state.get("running"):
            raise RuntimeError("pre-start is already running")
        if self.state.get("cleanup_available"):
            raise RuntimeError(
                "a pre-start session is still active - start the run or abort it first")
        if self.sup.run_in_progress:
            raise RuntimeError("a run is in progress - abort it first")
        if getattr(self.sup, "hcpes_running", False):
            raise RuntimeError("HCPES characterization is running - stop it first")

        # Capture the recipe before scheduling the task. Validation and value
        # resolution happen inside _run before its first hardware command, so a
        # malformed request reports through normal pre-start state just like the
        # legacy controller did, without an unhandled task exception.
        recipe = self.store.get(recipe_id) if recipe_id else self.store.selected()
        self.stop_event.clear()
        self._session_generation += 1
        self.params = dict(params or {})
        self.session = None
        self._grounded_plasma_switches.clear()
        self._receipts = []
        self.state = {
            "state": "running", "running": True, "done": False, "primed": False,
            "cleanup_available": False,
            "recipe_id": recipe.id, "recipe_name": recipe.name,
            "revision": recipe.revision, "phase": "validating", "step_id": "",
            "step_index": 0, "step_total": len([s for s in recipe.start_steps if s.enabled]),
            "step_desc": "", "held_s": 0.0, "hold_target_s": 0.0,
            "step_action": "", "step_target": "", "step_elapsed_s": 0.0,
            "step_remaining_s": None, "current": None, "lit": False,
            "strikes": 0, "receipts": [],
        }
        self.task = asyncio.create_task(
            self._run(recipe, self.params, expected_revision), name="prestart")
        self.sup.report_event("recipe", f"pre-start sequence started: {recipe.name}")

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.stop_event.set()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.task), timeout=10.0)
            if not self.task.done():
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
        self.task = None
        if self.state.get("running"):
            self.sup.report_event("recipe", "pre-start stopped by operator")
        self.state["running"] = False

    async def abort(self) -> None:
        """Stop the active step, then execute the snapshotted cleanup recipe."""
        if self.abort_task is None or self.abort_task.done():
            # Publish ownership before the first await so concurrent requests
            # cannot both enter the physical cleanup sequence.
            generation = self._session_generation
            session = self.session
            self.abort_task = asyncio.create_task(
                self._abort_owned(generation, session), name="prestart-abort")
        await asyncio.shield(self.abort_task)

    async def _abort_owned(self, generation: int,
                           session: ResolvedPrestart | None) -> None:
        """Execute cleanup once for the captured session generation."""
        await self.stop()
        self.state.update(state="aborting", running=False, primed=False,
                          cleanup_available=True,
                          phase="running abort sequence")
        errors: list[str] = []
        if session is not None:
            self.stop_event.clear()
            errors = await self._run_steps(session.abort_steps, cleanup=True)
        # A new session cannot normally start while this task is live, but the
        # generation guard also prevents a late unwind from clearing it if a
        # future admission path changes that ordering.
        if generation == self._session_generation:
            self.session = None
        self.state.update(
            state="error" if errors else "aborted", running=False, done=False,
            primed=False, cleanup_available=False,
            phase=("abort completed with errors" if errors else "aborted"),
            cleanup_errors=errors,
        )
        if errors:
            self.sup.report_event("error", "pre-start abort: " + "; ".join(errors))
        else:
            self.sup.report_event("recipe", "pre-start abort sequence complete")

    @property
    def aborting(self) -> bool:
        return self.abort_task is not None and not self.abort_task.done()

    async def cancel_owned_abort(self) -> None:
        """Bound process shutdown by explicitly collecting owned cleanup."""
        task = self.abort_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.state.update(
            state="error", running=False, primed=False,
            cleanup_available=True,
            phase="abort cancelled during server shutdown",
            error="pre-start cleanup did not finish before shutdown",
        )

    def consume_primed(self) -> None:
        """A run has accepted ownership of the primed hardware state."""
        if self.state.get("primed"):
            self.state.update(state="handed_off", done=False, primed=False,
                              cleanup_available=False, phase="handed off to run")
            self.session = None

    async def _run(self, recipe, params: dict[str, Any],
                   expected_revision: int | None) -> None:
        try:
            if expected_revision is not None and recipe.revision != expected_revision:
                raise RuntimeError(
                    f"recipe changed since review: expected revision {expected_revision}, "
                    f"current revision is {recipe.revision}")
            from .prestart_model import resolve_recipe
            session = resolve_recipe(recipe, self.store.catalog, params)
            self.session = session
            self.state.update(recipe_id=session.recipe_id, recipe_name=session.name,
                              revision=session.revision,
                              step_total=len(session.start_steps), phase="starting",
                              cleanup_available=True)
            errors = await self._run_steps(session.start_steps)
            if errors:
                raise RuntimeError("; ".join(errors))
            if self.stop_event.is_set():
                self.state.update(state="stopped", done=False, primed=False,
                                  phase="stopped")
                return
            self.state.update(state="primed", done=True, primed=True,
                              phase="done - recipe primed")
            self.sup.report_event("recipe", f"pre-start complete: {session.name}")
        except Exception as exc:
            self.state.update(state="error", done=False, primed=False,
                              phase=f"error: {exc}", error=str(exc))
            self.sup.report_event("error", f"pre-start failed: {exc}")
        finally:
            # A configured plasma action owns its switch from validation onward,
            # even if an earlier step is stopped or fails before strike begins.
            # This preserves the established pre-start guarantee without
            # inventing inverse commands for unrelated recipe actions.
            if self.session is not None:
                for step in self.session.start_steps:
                    if step.action != "plasma.strike_hold":
                        continue
                    switch = target_device_id(step.args["switch"])
                    if switch in self._grounded_plasma_switches:
                        continue
                    try:
                        await self.sup.set_valve(
                            switch, True, reason="pre-start end - beam off")
                        self._grounded_plasma_switches.add(switch)
                    except Exception as exc:
                        self.sup.report_event(
                            "error", f"pre-start could not ground {switch}: {exc}")
            self.state["running"] = False

    async def _run_steps(self, steps: tuple[ResolvedStep, ...], *,
                         cleanup: bool = False) -> list[str]:
        errors: list[str] = []
        for index, step in enumerate(steps, start=1):
            if self.stop_event.is_set() and not cleanup:
                break
            self.state.update(
                step_id=step.id, step_index=index, step_total=len(steps),
                step_desc=step.summary, phase=step.summary,
                step_action=step.action, step_target=step.target,
                step_elapsed_s=0.0, step_remaining_s=None,
                step_started=self.clock.wall(), step_elapsed_started=self.clock.elapsed(),
            )
            handler = self._handlers.get(step.action)
            if handler is None:
                error = f"no executor for action {step.action!r}"
                if not cleanup and step.on_error == "stop":
                    raise RuntimeError(error)
                errors.append(error)
                continue
            try:
                if cleanup:
                    await asyncio.wait_for(handler(step), timeout=5.0)
                else:
                    await handler(step)
                self._receipt(step, "ok", cleanup=cleanup)
            except Exception as exc:
                error = f"{step.summary}: {type(exc).__name__}: {exc}"
                self.sup.report_event("error", f"pre-start step failed: {error}")
                self._receipt(step, "error", error=error, cleanup=cleanup)
                if not cleanup and step.on_error == "stop":
                    raise
                errors.append(error)
        return errors

    def _receipt(self, step: ResolvedStep, status: str, *, error: str = "",
                 cleanup: bool = False) -> None:
        self._receipts.append({
            "step_id": step.id, "summary": step.summary, "action": step.action,
            "target": step.target, "section": "cleanup" if cleanup else "start",
            "status": status, "error": error,
        })
        self.state["receipts"] = list(self._receipts)

    async def _nap(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    async def _delay(self, step: ResolvedStep) -> None:
        duration = step.args["seconds"]
        started = self.clock.elapsed()
        while not self.stop_event.is_set():
            elapsed = self.clock.elapsed() - started
            remaining = max(0.0, duration - elapsed)
            self.state.update(step_elapsed_s=elapsed, step_remaining_s=remaining)
            if remaining <= 0:
                return
            if await self._nap(min(0.2, remaining)):
                return

    async def _valve_open(self, step: ResolvedStep) -> None:
        await self.sup.set_valve(target_device_id(step.target), True, reason="pre-start recipe")

    async def _valve_close(self, step: ResolvedStep) -> None:
        await self.sup.set_valve(target_device_id(step.target), False, reason="pre-start recipe")

    async def _mfc_start(self, step: ResolvedStep) -> None:
        await self.sup.set_mfc_setpoint(target_device_id(step.target), step.args["sccm"])

    async def _mfc_stop(self, step: ResolvedStep) -> None:
        await self.sup.set_mfc_setpoint(target_device_id(step.target), 0.0)

    async def _supply_on(self, step: ResolvedStep) -> None:
        await self.sup.set_supply_output(target_device_id(step.target), True)

    async def _supply_off(self, step: ResolvedStep) -> None:
        await self.sup.set_supply_output(target_device_id(step.target), False)

    async def _supply_voltage(self, step: ResolvedStep) -> None:
        await self.sup.set_supply_voltage(target_device_id(step.target), step.args["volts"])

    async def _supply_current(self, step: ResolvedStep) -> None:
        await self.sup.set_supply_current(target_device_id(step.target), step.args["amps"])

    async def _arm_bias(self, step: ResolvedStep) -> None:
        await self.sup.arm_sample_bias(
            target_device_id(step.target), volts=step.args["volts"],
            polarity=step.args["polarity"], reason="pre-start recipe")

    async def _hv_off(self, step: ResolvedStep) -> None:
        await self.sup.hv_off(reason="pre-start recipe abort")

    async def _fill_start(self, step: ResolvedStep) -> None:
        await self.sup.start_fill_regulation(
            valve=target_device_id(step.args["valve"]),
            gauge=target_read_key(step.args["gauge"], self.store.catalog),
            target_torr=step.args["target_torr"],
            pulse_on_s=step.args["pulse_on_s"], pulse_off_s=step.args["pulse_off_s"],
            tolerance_frac=step.args["tolerance_frac"],
        )

    async def _fill_stop(self, step: ResolvedStep) -> None:
        await self.sup.stop_fill_regulation()

    async def _strike_hold(self, step: ResolvedStep) -> None:
        switch = target_device_id(step.args["switch"])
        ammeter = target_read_key(step.args["ammeter"], self.store.catalog)
        minimum = step.args["min_current_a"]
        pulse_s, settle_s, hold_target = (
            step.args["pulse_s"], step.args["settle_s"], step.args["hold_s"])
        self.state["hold_target_s"] = hold_target
        held = 0.0
        try:
            await self.sup.set_valve(switch, False, reason="pre-start - beam on")
            if await self._nap(settle_s):
                return
            while not self.stop_event.is_set():
                t0 = self.clock.elapsed()
                if await self._nap(0.2):
                    return
                dt = self.clock.elapsed() - t0
                current = self.sup.snapshot.get(ammeter)
                lit = isinstance(current, (int, float)) and abs(current) >= minimum
                self.state.update(current=(float(current)
                                           if isinstance(current, (int, float)) else None),
                                  lit=lit)
                if not lit:
                    if held > 0:
                        self.sup.report_event(
                            "flag", "pre-start: plasma dropped out, restriking")
                    held = 0.0
                    self.state.update(held_s=0.0, phase="striking plasma",
                                      strikes=self.state.get("strikes", 0) + 1)
                    await self.sup.set_valve(
                        switch, True, reason="pre-start reignite pulse")
                    if await self._nap(pulse_s):
                        return
                    await self.sup.set_valve(
                        switch, False, reason="pre-start reignite - beam on")
                    if await self._nap(settle_s):
                        return
                    continue
                held += dt
                self.state.update(held_s=held,
                                  phase=f"holding current ({held:.1f}/{hold_target:g} s)")
                if held >= hold_target:
                    return
        finally:
            try:
                await self.sup.set_valve(
                    switch, True, reason="pre-start end - beam off")
                self._grounded_plasma_switches.add(switch)
            except Exception:
                pass

    async def _wait_until(self, step: ResolvedStep) -> None:
        read_key = target_read_key(step.target, self.store.catalog, step.action)
        if not read_key:
            raise ValueError(f"{step.target} has no readable value")
        operator = step.args["operator"]
        value, upper = step.args["value"], step.args.get("upper")
        hold_target, timeout = step.args["hold_s"], step.args["timeout_s"]
        absolute = step.args["absolute"]
        started, held = self.clock.elapsed(), 0.0
        previous = started
        while not self.stop_event.is_set():
            now = self.clock.elapsed()
            elapsed = now - started
            current = self.sup.snapshot.get(read_key)
            compare = abs(current) if absolute and isinstance(current, (int, float)) else current
            matched = False
            if isinstance(compare, (int, float)):
                if operator == "above":
                    matched = compare >= value
                elif operator == "below":
                    matched = compare <= value
                else:
                    lo, hi = sorted((value, upper if upper is not None else value))
                    matched = lo <= compare <= hi
            held = held + (now - previous) if matched else 0.0
            previous = now
            self.state.update(
                current=current, held_s=held, hold_target_s=hold_target,
                step_elapsed_s=elapsed,
                step_remaining_s=max(0.0, timeout - elapsed) if timeout > 0 else None)
            if matched and held >= hold_target:
                return
            if timeout > 0 and now - started >= timeout:
                raise TimeoutError(f"{read_key} did not satisfy {operator} {value:g} "
                                   f"within {timeout:g} s")
            if await self._nap(0.2):
                return
