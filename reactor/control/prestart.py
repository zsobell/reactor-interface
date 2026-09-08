"""Operator-requested pre-start sequence and its stop/abort lifecycle.

All hardware commands go through the owning Supervisor. This controller owns
only its task, parameters and progress; it does not own device connections.
"""
from __future__ import annotations

import asyncio
import contextlib
import time


class PrestartController:
    def __init__(self, supervisor):
        self.sup = supervisor
        self.state = {"running": False}
        self.params = {}
        self.task = None
        self.stop_event = asyncio.Event()

    async def start(self, params: dict) -> None:
        """Bring the tool up to a struck, primed, beam-off state.

        Exactly the sequence the operator specified (2026-08-05):
        open the Ar isolation valve, wait, flow Ar, start the precursor fill
        pulse, run the reignite protocol until sample current appears, hold
        that current, then energise plasma ground so the beam ends OFF.

        The strike retries indefinitely - by explicit instruction there is no
        timeout and no attempt limit. Stopping is the operator's call, via
        stop_prestart. However this ends - finished, stopped, or crashed - the
        beam is grounded on the way out; Ar and the fill regulation are left
        running, which is the same state a completed sequence leaves behind.
        """
        if self.state.get("running"):
            raise RuntimeError("pre-start is already running")
        if self.sup.recipes.busy or self.sup._run_start_lock.locked():
            raise RuntimeError("a run is in progress - abort it first")

        self.stop_event.clear()
        # Kept so abort_prestart can undo exactly what this sequence turned on,
        # rather than guessing at the default valve/MFC ids.
        self.params = dict(params)
        self.state = {
            "running": True, "phase": "starting", "lit": False,
            "current": None, "held_s": 0.0, "hold_target_s": 0.0, "strikes": 0,
        }
        self.task = asyncio.create_task(
            self._run(params), name="prestart")
        self.sup._event("recipe", "pre-start sequence started")

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.stop_event.set()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(self.task, timeout=10.0)
            if not self.task.done():
                self.task.cancel()
        self.task = None
        if self.state.get("running"):
            self.sup._event("recipe", "pre-start stopped by operator")
        # _run_prestart's own finally already set running=False/done/phase/
        # strikes in place by the time the wait above returns - merge, don't
        # replace, or an operator-initiated stop always reports back as bare
        # "idle" and throws away the phase/strike-count info the UI shows.
        self.state["running"] = False

    async def abort(self) -> None:
        """One-click undo of the pre-start: Ar off, fill off, beam relay at rest,
        HV off.

        `stop_prestart` only ends the *sequence*, and leaves the tool primed -
        Ar flowing, fill pulsing, beam grounded - because that is the state a
        successful pre-start is supposed to hand over to Start run. Once the
        plasma had struck there was no button that undid any of it (the Stop
        button greys out the moment the sequence finishes), so backing out
        meant closing Ar, stopping the regulator and clearing the relay by
        hand. This is that, on one click (operator request, 2026-08-21).

        Ordering matters: the sequence is stopped first, and its own teardown
        grounds the beam on the way out - so the relay is cleared AFTER that,
        or it would be re-energised behind us.

        The relay ends DE-ENERGISED, which is the "beam on" sense. That is
        deliberate: the relay box runs off a 9 V battery that only drains while
        the relay is energised, so at rest it belongs off. With HV commanded off
        in the same click there is nothing for an ungrounded beam to do.
        """
        p = dict(self.params)
        g = lambda k, d: p.get(k, d)  # noqa: E731

        await self.stop()

        errors: list[str] = []

        async def attempt(what: str, coro) -> None:
            try:
                await coro
            except Exception as exc:
                errors.append(f"{what}: {type(exc).__name__}: {exc}")

        # Ar: flow to zero before the isolation valve closes, so the MFC is not
        # left commanding gas into a closed valve.
        await attempt("Ar flow", self.sup.set_mfc_setpoint(g("ar_mfc", "ar"), 0.0))
        await attempt("Ar isolation valve",
                      self.sup.set_valve(g("ar_valve", "ar_pneumatic"), False,
                                     reason="pre-start abort"))
        # Precursor fill: stop the pulsing, then close the valve it was pulsing.
        await attempt("fill regulation", self.sup.stop_fill_regulation())
        await attempt("fill valve",
                      self.sup.set_valve(g("fill_valve", "rpm_top"), False,
                                     reason="pre-start abort"))
        await attempt("beam relay",
                      self.sup.set_valve(g("plasma_switch", "plasma_ground"), False,
                                     reason="pre-start abort - relay at rest"))
        await attempt("HV off", self.sup.hv_off(reason="pre-start abort"))
        await attempt("DC supply outputs",
                      self.sup.supplies_output_off(reason="pre-start abort"))

        # The tool is no longer primed, so the UI must stop offering the abort
        # (and stop claiming pre-start is complete).
        self.state["running"] = False
        self.state["done"] = False
        self.state["phase"] = ("aborted - Ar and fill off, relay at rest, "
                                  "HV off, DC supplies off")
        if errors:
            self.sup._event("error", "pre-start abort: " + "; ".join(errors))
        else:
            self.sup._event("recipe", "pre-start aborted: Ar off, fill off, "
                                  "beam relay de-energised, HV off, "
                                  "DC supply outputs off")

    async def _run(self, p: dict) -> None:
        g = lambda k, d: p.get(k, d)  # noqa: E731
        ar_valve = g("ar_valve", "ar_pneumatic")
        ar_mfc = g("ar_mfc", "ar")
        ar_sccm = float(g("ar_sccm", 4.0))
        valve_delay_s = float(g("valve_delay_s", 1.0))
        hold_s = float(g("hold_s", 5.0))
        switch = g("plasma_switch", "plasma_ground")
        ammeter = g("ammeter", "inst.ammeter")
        min_current = float(g("min_current_a", 5.0e-4))
        pulse_s = float(g("reignite_pulse_s", 0.10))
        settle_s = float(g("reignite_settle_s", 0.15))

        async def nap(dur: float) -> bool:      # True if asked to stop
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=dur)
                return True
            except asyncio.TimeoutError:
                return False

        def phase(name: str) -> None:
            self.state["phase"] = name

        struck = False
        try:
            self.state["hold_target_s"] = hold_s

            # First, before any gas: the operator wants the DC supplies live
            # early so he can see how the tool is behaving before a run starts.
            # These stay on for the whole run and are never cycled with the
            # beam - see supplies_output_on.
            phase("switching on DC supplies")
            await self.sup.supplies_output_on(
                sample_bias_v=float(g("sample_bias_v", 0.0)),
                polarity=int(g("sample_bias_polarity", 1)),
                reason="pre-start")

            phase("opening Ar isolation valve")
            await self.sup.set_valve(ar_valve, True, reason="pre-start")
            if await nap(valve_delay_s):
                return

            phase(f"Ar to {ar_sccm:g} sccm")
            await self.sup.set_mfc_setpoint(ar_mfc, ar_sccm)

            phase("starting precursor fill pulse")
            await self.sup.start_fill_regulation(
                valve=g("fill_valve", "rpm_top"),
                gauge=g("gauge", "gauge.prec1_dose"),
                target_torr=float(g("dose_pressure_torr", 0.02)),
                pulse_on_s=float(g("fill_pulse_on_s", 0.10)),
                pulse_off_s=float(g("fill_pulse_off_s", 0.30)),
                tolerance_frac=float(g("tolerance_frac", 0.20)),
            )

            # Strike, then hold. A drop-out during the hold sends it straight
            # back to striking, and the held time restarts - the point of the
            # hold is a continuous stretch of current, not a total.
            phase("striking plasma")
            await self.sup.set_valve(switch, False, reason="pre-start - beam on")
            if await nap(settle_s):
                return

            held = 0.0
            while not self.stop_event.is_set():
                t0 = time.time()
                if await nap(0.2):
                    return
                dt = time.time() - t0
                cur = self.sup.snapshot.get(ammeter)
                lit = isinstance(cur, (int, float)) and abs(cur) >= min_current
                self.state["current"] = (
                    float(cur) if isinstance(cur, (int, float)) else None)
                self.state["lit"] = lit

                if not lit:
                    if held > 0.0:
                        self.sup._event("flag", "pre-start: plasma dropped out, restriking")
                    held = 0.0
                    self.state["held_s"] = 0.0
                    phase("striking plasma")
                    self.state["strikes"] = self.state.get("strikes", 0) + 1
                    # Same restrike protocol the run uses.
                    await self.sup.set_valve(switch, True, reason="pre-start reignite pulse")
                    if await nap(pulse_s):
                        return
                    await self.sup.set_valve(switch, False, reason="pre-start reignite - beam on")
                    if await nap(settle_s):
                        return
                    continue

                held += dt
                self.state["held_s"] = held
                phase(f"holding current ({held:.1f}/{hold_s:g} s)")
                if held >= hold_s:
                    struck = True
                    break

            if struck:
                phase("done - beam grounded, Ar and fill running")
                self.sup._event("recipe",
                            "pre-start complete: plasma struck and held "
                            f"{hold_s:g} s, beam grounded")
        except Exception as exc:
            self.state["phase"] = f"error: {exc}"
            self.sup._event("error", f"pre-start failed: {exc}")
        finally:
            # The sequence's declared end state is beam OFF, and that applies
            # however it ends - including an operator stop mid-strike.
            with contextlib.suppress(Exception):
                await self.sup.set_valve(switch, True, reason="pre-start end - beam off")
            self.state["running"] = False
            self.state["done"] = struck

