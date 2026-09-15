"""Declarative dosing sequences and the engine that runs them.

Recipes are YAML, not code, so changing a dose time is not a programming task.

Timing runs on its own cooperative asyncio task. Timed waits use the event
loop, while exposure/cycle bookkeeping uses monotonic measurements. The original
program timed steps with a 1 s Express-VI delay inside
the same loop that redrew the front panel, so step length drifted whenever the UI
got busy. Worker-based recording and analysis keep their disk waits off this event loop;
other blocking work and scheduler jitter can still delay it.

Software timing is cooperative, with no verified upper bound on scheduling
lateness. Duration accounting uses an injected monotonic clock; experiment
labels use wall time. Physical deadlines require measured sensor/driver/actuator
latency and suitable hardware, independently of this software model.
"""

from __future__ import annotations

from .contracts import RecipeHost
from .clock import Clock

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Literal



# --------------------------------------------------------------------------- #
#  Recipe schema
# --------------------------------------------------------------------------- #


# Re-export these names for existing callers and file recipes.
from .recipe_model import (
    Step, GasSchedule, Recipe, build_ald_recipe, build_cvd_recipe,
    BEAM_TICK_S, GAS_SCHEDULE_MFCS, gas_display_name,
)

# --------------------------------------------------------------------------- #
#  Runner
# --------------------------------------------------------------------------- #


@dataclass
class RecipeProgress:
    state: Literal["idle", "running", "paused", "aborting", "done", "error"] = "idle"
    recipe: str = ""
    phase: str = ""
    cycle: int = 0
    cycles_total: int = 0
    step_index: int = 0
    step_total: int = 0
    step_op: str = ""
    step_desc: str = ""
    step_started: float | None = None  # wall timestamp for compatibility
    step_elapsed_started: float | None = None
    clock: Clock = field(default_factory=Clock, repr=False, compare=False)
    step_duration: float | None = None
    started_at: float | None = None
    message: str = ""
    error: str = ""
    #: live beam status while an electron_beam step runs, else None
    beam: dict | None = None
    #: set by a lit_gated step, whose remaining time is not a wall-clock
    #: countdown and so cannot be derived from step_started/step_duration
    step_remaining_hint: float | None = None
    #: Operator-paused time inside the CURRENT step, so the step countdown the
    #: UI shows is run time and not wall time. Without these, `pause` froze the
    #: step's action and the cycle clock but the "Step remaining" readout kept
    #: falling - half of what "in the purge step the timer keeps moving" was
    #: (2026-09-01). Reset per step by _run_steps.
    step_paused_accum: float = 0.0
    step_pause_start: float | None = None
    #: fractional cycle number for property-vs-cycle plotting, set by the runner
    #: each telemetry tick: whole part = completed cycles, fraction = progress
    #: through the current cycle's predicted length, FROZEN during a reignite or
    #: operator pause. None outside the cycling phase. `paused` marks a sample
    #: taken while that progress was frozen - those are kept in the raw run log
    #: but left out of the plot-ready by-cycle file. See RecipeRunner.cycle_fraction.
    cycle_fraction: float | None = None
    paused: bool = False
    #: WHY the cycle clock is frozen right now: "operator", "reignite", or "".
    #: Set by the supervisor each telemetry tick, alongside `paused`.
    pause_reason: str = ""

    def log_step(self) -> str:
        """What the run log's `recipe_step` column says for a sample taken now.

        A reignite is an event in its own right, not part of the electron-beam
        step it interrupts, and an operator pause is not a recipe step at all.
        Both therefore get their own label here, instead of a separate 0/1
        `paused` column sitting beside whichever step they happened to freeze
        (operator request, 2026-08-21): one descriptive column rather than two,
        and narrowing a raw run file to just the deposition is
        `recipe_step not in ("reignite", "pause")`.
        """
        if self.pause_reason == "operator":
            return "pause"
        if self.pause_reason == "reignite":
            return "reignite"
        return self.step_desc

    def as_dict(self) -> dict:
        remaining = None
        if self.step_remaining_hint is not None:
            remaining = max(0.0, self.step_remaining_hint)
        elif self.step_elapsed_started is not None and self.step_duration is not None:
            held = self.step_paused_accum
            if self.step_pause_start is not None:
                held += self.clock.elapsed() - self.step_pause_start
            remaining = max(0.0, self.step_duration
                            - (self.clock.elapsed() - self.step_elapsed_started - held))
        return {
            "state": self.state,
            "recipe": self.recipe,
            "phase": self.phase,
            "cycle": self.cycle,
            "cycles_total": self.cycles_total,
            "step_index": self.step_index,
            "step_total": self.step_total,
            "step_op": self.step_op,
            "step_desc": self.step_desc,
            "step_remaining_s": remaining,
            "step_duration_s": self.step_duration,
            "started_at": self.started_at,
            "message": self.message,
            "error": self.error,
            "beam": self.beam,
            "cycle_number": self.cycle_fraction,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
        }


class RecipeRunner:
    """Executes a Recipe against a supervisor.

    Invariant: however this exits - finished, aborted, or crashed mid-dose -
    every dose valve is commanded closed on the way out.
    """

    def __init__(self, supervisor: RecipeHost, *, clock: Clock | None = None) -> None:
        self.clock = clock or Clock()
        self.sup = supervisor
        self.progress = RecipeProgress(clock=self.clock)
        self._task: asyncio.Task | None = None
        self.recipe = None
        self._pause = asyncio.Event()
        self._pause.set()               # set == not paused
        #: The same condition the other way up, so a timed wait can be woken BY
        #: a pause. An asyncio.Event can only be awaited for "set", and _pause
        #: is set while running - so without this, a step sleeping through its
        #: duration had no way to hear a pause arrive and could only notice at
        #: the next step boundary. That is exactly what "the timer keeps moving
        #: in the purge step" was (2026-09-01).
        self._paused = asyncio.Event()
        self._abort = asyncio.Event()
        self._gas_schedules: list[GasSchedule] = []
        self._gas_lead_tasks: list[asyncio.Task] = []
        #: One-shot "shut this gas off" commands from a mid-run parameter edit.
        #: Held only so the loop keeps a reference; they discard themselves.
        self._gas_off_tasks: set[asyncio.Task] = set()
        # EE-CVD continuous beam. Two clocks, both advanced by _beam_watch:
        #   _lit_s      total seconds with current present; what a lit_gated
        #               wait counts down against.
        #   _cycle_clock seconds into the current cycle for the gas schedule.
        #               Advances whenever the running step's own clock advances,
        #               i.e. always during an ungated step (the dose) and only
        #               while lit during a gated one (pump A). That is what
        #               keeps the gas windows locked to the cycle.
        self._beam_task: asyncio.Task | None = None
        self._beam_switch: str | None = None
        self._lit_s = 0.0
        self._cycle_clock = 0.0
        self._clock_gated = False
        self._cycle_len = 0.0
        self._gas_plan: dict | None = None
        #: Last state written per MFC id (None = never written). Keyed by MFC
        #: rather than by order because two gases can share the order
        #: "simultaneous"; keying by order would have them overwrite each other.
        self._gas_on: dict[str, bool | None] = {}
        self._gas_overlap_s = 0.0
        # Fractional-cycle bookkeeping for property-vs-cycle plotting. Progress
        # through a cycle is elapsed time since the cycle began MINUS time spent
        # frozen (reignite or operator pause) - computable at any log instant,
        # and correct for both ALD (beam exposure freezes on a dead plasma) and
        # CVD (the lit-gated pump A freezes). Pauses are reference-counted so
        # overlapping reasons nest cleanly.
        self._cycle_start_elapsed: float | None = None
        self._cycle_paused_accum = 0.0
        self._pause_start: float | None = None
        self._pause_reasons: set[str] = set()
        # Sample bias bracketing the beam (Step.bias_v & co). One pending flip
        # at a time: scheduling a new one cancels whatever was queued, which is
        # what makes a pump B shorter than the trail time do the right thing -
        # the next cycle's ON cancels the pending OFF and the bias simply stays
        # up rather than blinking. `_bias_armed` is the once-per-run voltage
        # write; after that only the OUTPUT is switched, so adjusting the level
        # by hand on the Hardware tab mid-run is not fought every cycle.
        self._bias_pending: list[tuple[float, asyncio.Task]] = []
        self._bias_step: Step | None = None
        self._bias_armed = False
        self._bias_on = False

    def gas_names(self):
        return getattr(self.sup, "gas_names", lambda: {})()

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    # -- fractional cycle number (for property-vs-cycle plots) -------------- #

    def _cycle_pause(self, reason: str, active: bool) -> None:
        """Reference-counted freeze of the cycle-progress clock. The clock is
        frozen while any reason is active; nesting reasons (e.g. a reignite that
        overlaps an operator pause) accrue paused time only once."""
        was = bool(self._pause_reasons)
        if active:
            self._pause_reasons.add(reason)
        else:
            self._pause_reasons.discard(reason)
        now_active = bool(self._pause_reasons)
        if now_active and not was:
            self._pause_start = self.clock.elapsed()
        elif was and not now_active and self._pause_start is not None:
            self._cycle_paused_accum += self.clock.elapsed() - self._pause_start
            self._pause_start = None

    def _begin_cycle_clock(self) -> None:
        """Reset the cycle-progress clock at the start of a cycle."""
        self._cycle_start_elapsed = self.clock.elapsed()
        self._cycle_paused_accum = 0.0
        self._pause_start = self._cycle_start_elapsed if self._pause_reasons else None

    @property
    def cycle_paused(self) -> bool:
        return bool(self._pause_reasons) and self._cycle_start_elapsed is not None

    @property
    def pause_reason(self) -> str:
        """Which freeze is active, operator first: an operator pause that
        overlaps a reignite is the one worth reporting.

        Deliberately NOT gated on the cycling phase the way `cycle_paused` is -
        if the operator pauses during setup or teardown the log should still say
        so. Those rows carry no cycle number and are dropped from the by-cycle
        and merged files regardless, so the two cannot disagree where it counts.
        """
        if "operator" in self._pause_reasons:
            return "operator"
        if "reignite" in self._pause_reasons:
            return "reignite"
        return ""

    def cycle_fraction(self, now: float | None = None) -> float | None:
        """Fractional cycle number at elapsed-clock `now`, or None outside the cycling phase.
        Whole part = completed cycles; fraction = frozen-adjusted progress
        through the current cycle's predicted length (Recipe.cycle_seconds)."""
        if (self.progress.phase != "cycling" or self.progress.cycle <= 0
                or self._cycle_len <= 0 or self._cycle_start_elapsed is None):
            return None
        now = self.clock.elapsed() if now is None else now
        paused = self._cycle_paused_accum
        if self._pause_start is not None:
            paused += now - self._pause_start
        prog = max(0.0, (now - self._cycle_start_elapsed) - paused)
        # The actual cycle can run a little longer than its predicted length
        # (per-step overhead, reignites already removed above). Cap progress at
        # the predicted length so cycle N's samples stay in [N-1, N] and the
        # number never steps backwards at the boundary where cycle increments.
        prog = min(prog, self._cycle_len)
        return (self.progress.cycle - 1) + prog / self._cycle_len

    # -- deterministic run clock (for the operator's "est. remaining") ----- #

    def run_total_s(self) -> float | None:
        """Nominal length of the cycling phase: one cycle's step durations times
        the cycle count. Setup and teardown are excluded - they are operator
        preamble, not the run the countdown is about."""
        if not self.progress.cycles_total or self._cycle_len <= 0:
            return None
        return self.progress.cycles_total * self._cycle_len

    def run_remaining_s(self) -> float | None:
        """Seconds left in the cycling phase, from the recipe - NOT from measured
        pace.

        This used to be extrapolated in the browser from the run's own average
        cycle time, which meant the number moved every few seconds for reasons
        the operator could not see, and never agreed with the arithmetic they
        had done from the parameters they typed. The requirement is the plain
        one: start at (cycle length x cycles) and tick down in real time,
        holding still only when the cycle itself is held still.

        That falls straight out of `cycle_fraction`, which is already exactly
        "how far through the run are we, in cycles, with reignites and operator
        pauses subtracted". Remaining is the rest of it, in seconds. So the
        countdown freezes during a reignite or a pause for the same reason and
        by the same mechanism as the cycle number does - there is no second
        clock to keep in step.
        """
        total = self.run_total_s()
        if total is None or self.progress.state not in ("running", "paused"):
            return None
        if self.progress.phase == "teardown":
            return 0.0                      # the cycles are done
        if self.progress.phase != "cycling":
            return total                    # "" (just started) or setup
        frac = self.cycle_fraction()
        if frac is None:
            return total
        return max(0.0, (self.progress.cycles_total - frac) * self._cycle_len)

    async def start(self, recipe: Recipe, *, started_at: float | None = None) -> None:
        if self.busy:
            raise RuntimeError("a recipe is already running")
        self._pause.set()
        self._paused.clear()
        self._abort.clear()
        self._gas_schedules = []
        self._gas_lead_tasks = []
        self._gas_off_tasks = set()
        self._beam_task = None
        self._beam_switch = None
        self._lit_s = 0.0
        self._cycle_clock = 0.0
        self._clock_gated = False
        self._cycle_len = recipe.cycle_seconds()
        self._gas_plan = None
        self._gas_on = {}
        self._gas_overlap_s = recipe.gas_overlap_s
        self._cycle_start_elapsed = None
        self._cycle_paused_accum = 0.0
        self._pause_start = None
        self._pause_reasons = set()
        self._bias_step = None
        self._bias_armed = False
        self._bias_on = False
        #: The recipe object actually running, so a mid-run parameter change has
        #: something to write into (Supervisor.update_run_params).
        self.recipe = recipe
        self.progress = RecipeProgress(
            state="running", recipe=recipe.name,
            cycles_total=recipe.cycles, started_at=self.clock.wall() if started_at is None else started_at,
            clock=self.clock,
        )
        self._task = asyncio.create_task(self._run(recipe), name="recipe")

    #: Step fields a mid-run parameter change is allowed to move. Everything
    #: else about a step - which valve, which gauge, which MFC - is plumbing,
    #: and changing it under a running recipe would mean the log no longer
    #: describes what ran.
    LIVE_STEP_FIELDS = (
        "seconds", "target_torr", "pulse_on_s", "pulse_off_s", "tolerance_frac",
        "min_current", "reignite_pulse_s", "reignite_settle_s",
        "bias_v", "bias_polarity", "bias_lead_s", "bias_trail_s", "sccm",
    )

    def apply_params(self, recipe_now, fresh) -> None:
        """Push a freshly built recipe's numbers into the one that is RUNNING.

        Operator, 2026-09-01: "I need to be able to change parameters mid run."
        Until now a recipe was a snapshot taken at Start, so the N2 flow that
        was wrong on Mo-017 stayed wrong for the whole run - the field accepted
        a new number and nothing happened with it.

        Values are copied INTO the existing Step objects rather than swapping
        the lists, which is what makes the change land on the cycle in progress:
        the beam step re-reads `min_current` every tick, the watchdog re-reads
        its reignite timings, and `_gas_set` re-reads a schedule's flow at the
        next window boundary. A step's `seconds` is read once when the step
        starts, so a duration change takes effect the next time that step runs -
        which is the only sane reading of "make the pump 2 s longer" while a
        pump is already counting down.

        Only LIVE_STEP_FIELDS move, and only when the fresh recipe has the same
        shape (same ops in the same order). A change that restructures the
        recipe - enabling a gas that was off, so the setup grows a step - is
        applied where it can be and left where it cannot; the caller's own diff
        is what gets reported to the operator either way.
        """
        for old_list, new_list in ((recipe_now.setup, fresh.setup),
                                   (recipe_now.steps, fresh.steps),
                                   (recipe_now.teardown, fresh.teardown)):
            if len(old_list) != len(new_list):
                continue
            for o, n in zip(old_list, new_list):
                if o.op != n.op:
                    continue
                for f in self.LIVE_STEP_FIELDS:
                    new_val = getattr(n, f, None)
                    if new_val is not None and getattr(o, f, None) != new_val:
                        setattr(o, f, new_val)

        recipe_now.cycles = fresh.cycles
        self.progress.cycles_total = fresh.cycles

        # Gas: mutate the live schedule objects so the flows in flight follow,
        # and keep the runner's own copy pointing at the same objects.
        #
        # Switching a gas OFF mid-run has to be honoured too, and used not to
        # be: an unticked gas simply vanishes from `fresh.gas_schedules`, the
        # lookup below missed it, and the ORIGINAL schedule object stayed in
        # `self._gas_schedules` cycling its old flow on and off for the rest of
        # the run - the operator sees a line they switched off being commanded
        # to 0.8 sccm every cycle, with 0 in every field (Zach, 2026-09-09).
        # So a gas that is gone gets shut off and dropped, and one that appears
        # is picked up. `_gas_on` is cleared for both so the next window
        # boundary re-asserts rather than trusting a stale "already on".
        by_mfc = {g.mfc: g for g in fresh.gas_schedules}
        dropped = [g for g in self._gas_schedules if g.mfc not in by_mfc]
        for g in dropped:
            self._gas_schedules.remove(g)
            self._gas_on.pop(g.mfc, None)
            task = asyncio.create_task(self._gas_set(g, 0.0),
                                       name=f"gas-off-{g.mfc}")
            self._gas_off_tasks.add(task)
            task.add_done_callback(self._gas_off_tasks.discard)
        live = {g.mfc for g in self._gas_schedules}
        for mfc_id, n in by_mfc.items():
            if mfc_id not in live:
                self._gas_schedules.append(n)
                self._gas_on.pop(mfc_id, None)
        for g in self._gas_schedules:
            n = by_mfc.get(g.mfc)
            if n is None:
                continue
            g.order, g.pct, g.flow_sccm = n.order, n.pct, n.flow_sccm
        self._gas_overlap_s = fresh.gas_overlap_s
        recipe_now.gas_overlap_s = fresh.gas_overlap_s

        # The countdown is cycle length x cycles, so both halves have to be
        # re-derived or "est. remaining" would keep quoting the old run.
        self._cycle_len = recipe_now.cycle_seconds()
        # _run aliases the recipe's own list, so the add/remove above is already
        # visible to the recipe (and to its teardown). Kept explicit here so a
        # future change that copies the list does not silently lose that.
        recipe_now.gas_schedules = self._gas_schedules
        if recipe_now.mode == "cvd":
            self._gas_plan = self._build_gas_plan()

    def pause(self) -> None:
        """Freeze the run: the clock stops AND the step's action stops.

        Operator, 2026-09-01: "It needs to stop the current action (e-beam or
        dose) and stop the timer. The step should resume with the correct
        timing on resume." Before this, pause only took effect at the next step
        BOUNDARY - a pump kept counting down, and a beam step froze its
        exposure budget while leaving the plasma on the sample.

        What each step does about it is the step's own business (see _sleep's
        on_pause/on_resume, _electron_beam and _beam_watch): the dose valve
        closes, the plasma ground goes back on, and both are undone on resume
        with the step's remaining time intact. The sample bias and the
        scheduled gases are deliberately NOT touched - operator's call, same
        day, when asked.
        """
        if self.busy and self.progress.state == "running":
            self._pause.clear()
            self._paused.set()
            self.progress.state = "paused"
            self.progress.step_pause_start = self.clock.elapsed()
            self._cycle_pause("operator", True)   # freeze cycle progress too

    def resume(self) -> None:
        if self.busy and self.progress.state == "paused":
            self._paused.clear()
            self._pause.set()
            self.progress.state = "running"
            if self.progress.step_pause_start is not None:
                self.progress.step_paused_accum += self.clock.elapsed() - self.progress.step_pause_start
                self.progress.step_pause_start = None
            self._cycle_pause("operator", False)

    async def abort(self) -> None:
        if not self.busy:
            return
        self.progress.state = "aborting"
        self._abort.set()
        self._paused.clear()
        self._pause.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if self._task:
                self._task.cancel()

    # -- execution ---------------------------------------------------------- #

    async def _run(self, recipe: Recipe) -> None:
        self._gas_schedules = recipe.gas_schedules
        try:
            self.progress.phase = "setup"
            await self._run_steps(recipe.setup)

            # Runway to the electron_beam step, from the start of a cycle - used
            # to schedule the "first" gas's lead-in before that step's start.
            # dose/pump-A durations are fixed inputs, never reignite-affected,
            # so this is exact (not just an estimate).
            beam_runway_s = 0.0
            for s in recipe.steps:
                if s.op == "electron_beam":
                    break
                beam_runway_s += s.seconds or 0.0
            # The same runway carries the sample bias's lead-in (EE-ALD). The
            # bias comes up bias_lead_s before the beam step starts, which is
            # inside dose/pump A, and goes down bias_trail_s after it ends,
            # which is inside pump B - so the stage is only energised around
            # the beam and the stage thermocouple reads clean everywhere else.
            beam_step = next((s for s in recipe.steps if s.op == "electron_beam"), None)

            self.progress.phase = "cycling"
            # A while loop, not `for cycle in range(recipe.cycles)`: the count
            # is re-read every pass so it can be changed mid-run (2026-09-01).
            # Raised, the run simply keeps going; LOWERED below the cycle in
            # progress, that cycle finishes and the run ends there with its
            # full teardown - the operator's call when asked, so the data never
            # contains a half cycle.
            cycle = 0
            while True:
                if self._abort.is_set():
                    break
                cycle += 1
                if cycle > recipe.cycles:
                    break
                beam_runway_s = 0.0
                for step in recipe.steps:
                    if step.op == "electron_beam":
                        break
                    beam_runway_s = beam_runway_s + (step.seconds or 0.0)
                self.progress.cycle = cycle
                self._begin_cycle_clock()
                if recipe.mode == "cvd":
                    # Restart the cycle clock, and evaluate the windows here at
                    # 0 rather than leaving it to the watchdog's next tick: that
                    # tick can land past a boundary and strand a gas on into the
                    # next cycle.
                    self._cycle_clock = 0.0
                    await self._apply_cvd_gas(0.0)
                else:
                    # Gases already flowing when the beam step starts, so they
                    # need leading in during dose/pump A: the "first" gas, or -
                    # when the window is not divided at all - every simultaneous
                    # one. Re-read EVERY cycle, not captured before the loop: a
                    # gas can be switched off (or on) mid-run, and a stale list
                    # here would keep leading in a gas the operator had just
                    # unticked - the same class of bug as the one fixed in
                    # apply_params on 2026-09-09.
                    lead_gases = [g for g in self._gas_schedules
                                  if g.order in ("first", "simultaneous")]
                    delay = max(0.0, beam_runway_s - self._gas_overlap_s)
                    for gas in lead_gases:
                        self._gas_lead_tasks.append(asyncio.create_task(
                            self._fire_gas_lead(gas, delay),
                            name=f"gas-lead-in-{gas.mfc}"))
                if recipe.mode != "cvd" and beam_step is not None:
                    self._schedule_bias(
                        beam_step, True,
                        max(0.0, beam_runway_s - beam_step.bias_lead_s))
                await self._run_steps(recipe.steps)

            self._cycle_start_elapsed = None       # cycle progress stops after cycling
            if not self._abort.is_set():
                self.progress.phase = "teardown"
                await self._run_steps(recipe.teardown)

            aborted = self._abort.is_set()
            self.progress.state = "idle" if aborted else "done"
            self.progress.message = (
                f"aborted after {self.progress.cycle} cycles" if aborted
                else f"completed {self.progress.cycle} of {recipe.cycles} cycles"
            )
        except Exception as exc:
            self.progress.state = "error"
            self.progress.error = f"{type(exc).__name__}: {exc}"
        finally:
            # A pending lead-in timer (queued for a cycle that never finished)
            # must not fire after the run has ended.
            for t in self._gas_lead_tasks:
                if not t.done():
                    t.cancel()
            if self._gas_lead_tasks:
                await asyncio.gather(*self._gas_lead_tasks, return_exceptions=True)
            self._gas_lead_tasks = []
            for task in list(self._gas_off_tasks):
                task.cancel()
            if self._gas_off_tasks:
                await asyncio.gather(*self._gas_off_tasks, return_exceptions=True)
            self._gas_off_tasks.clear()
            # EE-CVD holds the beam on across the whole run, so an abort or a
            # crash must not leave the watchdog running or the beam energised.
            # The teardown's beam_stop covers the happy path; this covers the
            # rest. Same invariant as _electron_beam's own finally.
            await self._kill_beam_watch()
            if self._beam_switch:
                with contextlib.suppress(Exception):
                    await self.sup.set_valve(
                        self._beam_switch, True, reason="run end - beam off")
                self._beam_switch = None
            # Same invariant for the sample bias: a queued flip must never fire
            # after the run, and the stage must never be left energised by an
            # abort or a crash. (finish_run switches every DC supply output off
            # below as well - this is the one that also kills the timers.)
            with contextlib.suppress(Exception):
                await self._bias_off_now()
            # However the run ends (finished, aborted, or crashed), hand off to
            # the supervisor's end-of-run cleanup: it always stops the background
            # fill regulation, and for an ALD run also zeroes the MFCs and closes
            # the fill valve.
            with contextlib.suppress(Exception):
                await self.sup.finish_run()

    async def _run_steps(self, steps: list[Step]) -> None:
        self.progress.step_total = len(steps)
        for i, step in enumerate(steps, start=1):
            if self._abort.is_set():
                return
            await self._pause.wait()
            self.progress.step_index = i
            self.progress.step_op = step.op
            self.progress.step_desc = step.describe(self.gas_names())
            self.progress.step_duration = step.seconds
            self.progress.step_started = self.clock.wall()
            self.progress.step_elapsed_started = self.clock.elapsed()
            self.progress.step_remaining_hint = None
            self.progress.step_paused_accum = 0.0
            self.progress.step_pause_start = self.clock.elapsed() if self.progress.state == "paused" else None
            await self._exec(step)
        self.progress.step_duration = None
        self.progress.step_started = None
        self.progress.step_elapsed_started = None

    async def _exec(self, step: Step) -> None:
        sup = self.sup

        if step.op == "dose":
            if not step.valve or step.seconds is None:
                raise ValueError("dose needs 'valve' and 'seconds'")
            # A dose is a pulse: open, hold, close. The close in `finally` is part
            # of the operation, so an aborted dose still ends with its own valve
            # returned to where it started.
            #
            # A pause closes the valve too and reopens it on resume, with the
            # rest of the pulse still to run (2026-09-01). A paused dose used to
            # hold the valve open for as long as the operator was away, dumping
            # precursor - the one place where "pause only takes effect at the
            # next step" was actively harmful rather than merely wrong.
            await sup.set_valve(step.valve, True, reason="recipe dose")
            try:
                await self._sleep(
                    step.seconds,
                    on_pause=lambda: sup.set_valve(step.valve, False,
                                                   reason="dose paused"),
                    on_resume=lambda: sup.set_valve(step.valve, True,
                                                    reason="dose resumed"))
            finally:
                await sup.set_valve(step.valve, False, reason="recipe dose end")

        elif step.op == "wait":
            if step.lit_gated:
                await self._sleep_lit(step.seconds or 0.0)
            else:
                await self._sleep(step.seconds or 0.0)

        elif step.op == "valve":
            if not step.valve or step.state is None:
                raise ValueError("valve needs 'valve' and 'state'")
            await sup.set_valve(step.valve, step.state, reason="recipe")

        elif step.op == "set_flow":
            if not step.mfc or step.sccm is None:
                raise ValueError("set_flow needs 'mfc' and 'sccm'")
            await sup.set_mfc_setpoint(step.mfc, step.sccm)

        elif step.op == "wait_for_pressure":
            await self._wait_for_pressure(step)

        elif step.op == "start_fill":
            if not step.valve or not step.gauge or step.target_torr is None:
                raise ValueError("start_fill needs 'valve', 'gauge', 'target_torr'")
            await sup.start_fill_regulation(
                valve=step.valve, gauge=step.gauge, target_torr=step.target_torr,
                pulse_on_s=step.pulse_on_s, pulse_off_s=step.pulse_off_s,
                tolerance_frac=step.tolerance_frac,
            )

        elif step.op == "stop_fill":
            await sup.stop_fill_regulation()

        elif step.op == "electron_beam":
            await self._electron_beam(step)

        elif step.op == "beam_start":
            await self._beam_start(step)

        elif step.op == "beam_stop":
            await self._beam_stop(step)

        elif step.op == "message":
            self.progress.message = step.text

    async def _fire_gas_lead(self, schedule: GasSchedule, delay_s: float) -> None:
        """Turn on the "first" gas `delay_s` wall-clock seconds from now.

        A separate task so it can count down while dose/pump-A are still
        running, ahead of the beam step it leads into. Cancelled by `_run`'s
        finally if the run ends before it fires.
        """
        try:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            if self._abort.is_set():
                return
            await self._pause.wait()
            # It has been counting down for a whole dose+pump-A; the gas may
            # have been switched off in the meantime.
            if schedule not in self._gas_schedules:
                return
            await self.sup.set_mfc_setpoint(schedule.mfc, schedule.flow_sccm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.sup.report_event("error", f"gas lead-in ({gas_display_name(schedule.mfc, self.gas_names())}): {exc}")

    # -- sample bias bracketing the beam ------------------------------------ #

    async def _set_bias(self, on: bool, step: Step | None = None) -> None:
        """Switch the sample-bias output, arming the level on the first ON.

        Never raises: a bias that will not respond is an event, not a reason to
        drop the beam step around it. The operator sees it in the event log and
        on the Hardware tab's supply card.
        """
        step = step or self._bias_step
        if step is None or not step.bias_v:
            return
        try:
            if on and not self._bias_armed:
                await self.sup.set_sample_bias_output(
                    True, volts=step.bias_v, polarity=step.bias_polarity,
                    reason="beam bracket")
                self._bias_armed = True
            else:
                await self.sup.set_sample_bias_output(
                    on, reason="beam bracket")
            self._bias_on = on
        except Exception as exc:
            self.sup.report_event("error", f"sample bias {'on' if on else 'off'}: {exc}")

    def _schedule_bias(self, step: Step, on: bool, delay_s: float) -> None:
        """Flip the sample bias `delay_s` from now.

        A separate task, like the gas lead-in, so the lead counts down inside
        the step that precedes the beam and the trail inside the one that
        follows it. Neither is ever awaited by a step, so bracketing the beam
        cannot stretch a cycle past the sum of its step durations.

        The newest instruction wins from its own deadline onward: scheduling a
        flip cancels every pending flip due at or after it, and leaves earlier
        ones alone. That is what makes the awkward settings behave. With a
        trail longer than pump B + dose + pump A, the next cycle's ON falls
        before the pending OFF, supersedes it, and the bias simply stays up
        across the boundary instead of blinking off just as the beam returns.
        """
        if not step.bias_v:
            return
        self._bias_step = step
        deadline = self.clock.elapsed() + max(0.0, delay_s)
        keep: list[tuple[float, asyncio.Task]] = []
        for due, task in self._bias_pending:
            if task.done():
                continue
            if due >= deadline:
                task.cancel()           # superseded by this flip
            else:
                keep.append((due, task))
        self._bias_pending = keep

        async def _flip() -> None:
            try:
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
                if on:
                    if self._abort.is_set():
                        return      # an abort never energises the stage
                    # Nor does an operator pause: the beam this is leading is
                    # held too, so energising now would sit a bias on the stage
                    # for the length of the pause with no beam to justify it.
                    # (Turning OFF is never gated - that direction is always
                    # safe to take immediately.) Same rule as _fire_gas_lead.
                    await self._pause.wait()
                await self._set_bias(on, step)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.sup.report_event("error", f"sample bias schedule: {exc}")

        self._bias_pending.append((deadline, asyncio.create_task(
            _flip(), name=f"bias-{'on' if on else 'off'}")))

    async def _bias_off_now(self) -> None:
        """Cancel every pending flip and drop the bias immediately. Run-end
        teardown: the stage must never be left energised by a crash or an
        abort, whatever a queued task was about to do."""
        pending, self._bias_pending = self._bias_pending, []
        for _due, task in pending:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._bias_on:
            await self._set_bias(False)

    async def _electron_beam(self, step: Step) -> None:
        """Beam ON (plasma ground OFF), hold `seconds` of exposure with current.

        Exposure only accumulates while |current| >= min_current. If the plasma
        goes out, pause the exposure clock, pulse the switch to reignite, and
        resume once current comes back.

        If gas_schedules are set (see RecipeRunner._run), this also runs the
        H2/N2 on/off handoff: at most one "first" and one "second" gas divide
        up the exposure by percentage, in the order given. Both boundaries are
        evaluated against the *accumulated exposure* (seconds - remaining), the
        same clock the reignite logic above already freezes during a dead
        plasma or an operator pause - so the gas handoff freezes right along
        with it, with no separate bookkeeping needed.
        """
        sup = self.sup
        if not step.switch or step.seconds is None:
            raise ValueError("electron_beam needs 'switch' and 'seconds'")

        total = float(step.seconds)
        first = next((g for g in self._gas_schedules if g.order == "first"), None)
        second = next((g for g in self._gas_schedules if g.order == "second"), None)
        # Simultaneous gases do not divide the exposure - they all cover the
        # whole of it, each at its own flow (operator, 2026-08-26). Validation
        # guarantees this is all-or-nothing, so when `simul` is non-empty
        # `first` and `second` are both None and the handoff arithmetic below
        # is inert rather than fighting it.
        simul = [g for g in self._gas_schedules if g.order == "simultaneous"]
        # Handoff point: where "first" ends and "second" starts. The single
        # overlap moves the incoming gas earlier, same as in EE-CVD.
        handoff_s = (first.pct / 100.0 * total) if first else 0.0
        second_on_s = max(0.0, handoff_s - self._gas_overlap_s)
        second_off_s = min(total, handoff_s + (second.pct / 100.0 * total if second else 0.0))
        first_on = False
        second_on = False
        simul_on = False

        await sup.set_valve(step.switch, False, reason="beam on")   # plasma ground off
        # That flip IS the strike - one is always required to start the beam.
        # The current needs the same settle time a reignite gets before it is
        # fair to judge whether the strike took; without it a healthy strike
        # that needs longer than one tick reads as "extinguished" and gets
        # pulsed back off, which both interrupts a good plasma and flags a
        # reignite every cycle.
        #
        # That settle used to be a blind sleep BEFORE the exposure loop, which
        # made the step reignite_settle_s longer than the exposure it was asked
        # for - 10.2 s of beam for a 10 s step, every cycle. It is now a grace
        # WINDOW inside the loop (the `t0 - strike_at` branch below): the same
        # protection from a premature reignite, but current that appears during
        # it counts as exposure, so `seconds` includes that elapsed interval.
        strike_at = self.clock.elapsed()
        try:
            # Defensive re-assert, same reasoning as the gas below: the lead-in
            # task scheduled at the start of the cycle should already have
            # raised the bias, but this step is the one that actually needs it
            # up, so an unbiased deposition is not left to a missed timer.
            if step.bias_v and not self._bias_on:
                await self._set_bias(True, step)
            if first is not None:
                # Defensive re-assert: the lead-in task should already have
                # turned this on before this step started, but this step is
                # the one place that actually needs it on, so make sure.
                await self._gas_set(first, first.flow_sccm)
                first_on = True
            if simul:
                # Same re-assert, and the only place these are switched on -
                # they have no window boundary to cross, so the tick loop below
                # never touches them.
                for gas in simul:
                    await self._gas_set(gas, gas.flow_sccm)
                simul_on = True

            remaining = total
            while remaining > 0 and not self._abort.is_set():
                if not self._pause.is_set():
                    # An operator pause takes the beam OFF THE SAMPLE, not just
                    # off the clock (2026-09-01). It used to freeze the exposure
                    # budget and leave the plasma running on the wafer for as
                    # long as the operator was away.
                    await sup.set_valve(step.switch, True, reason="paused - beam off")
                    self.progress.message = "paused - beam off"
                    await self._pause.wait()
                    if self._abort.is_set():
                        break
                    await sup.set_valve(step.switch, False, reason="resumed - beam on")
                    # The re-strike gets the same grace a first strike does, or
                    # the tick after it reads as "extinguished" and reignites.
                    strike_at = self.clock.elapsed()
                    continue
                t0 = self.clock.elapsed()
                # Clamp the last tick to what is actually left. A fixed 0.2 s
                # tick overshot the step by up to a full tick every time it ran
                # (0.1 s on average), which across 150 cycles is minutes.
                await self._tick(min(BEAM_TICK_S, remaining))
                dt = self.clock.elapsed() - t0            # unaffected by pausing
                cur = sup.snapshot.get(step.ammeter)
                lit = isinstance(cur, (int, float)) and abs(cur) >= step.min_current
                cur_val = float(cur) if isinstance(cur, (int, float)) else None
                # A dead plasma here freezes the exposure clock, so it freezes
                # the cycle-progress clock too (points logged now are "paused").
                self._cycle_pause("reignite", not lit)
                self.progress.beam = {
                    "remaining": max(0.0, remaining), "current": cur_val, "lit": lit,
                }
                if lit:
                    remaining -= dt
                    self.progress.message = (
                        f"beam on: |I|={abs(cur)*1e3:.2f} mA, "
                        f"{remaining:.1f}s exposure left"
                    )
                elif t0 - strike_at < step.reignite_settle_s:
                    # Still inside the strike's settle window: a plasma that is
                    # simply taking its time to come up is not out yet, so do
                    # not pulse the switch at it.
                    self.progress.message = "beam on - waiting for current"
                    continue
                else:
                    self.progress.message = "plasma out - reigniting"
                    sup.report_event("flag", "plasma extinguished during beam - reigniting")
                    await self._reignite(step)
                    continue    # consumed (below) is unchanged - gas state can't have crossed

                consumed = min(total, max(0.0, total - remaining))
                if second is not None and not second_on and consumed >= second_on_s:
                    await self._gas_set(second, second.flow_sccm)
                    second_on = True
                if first_on and consumed >= handoff_s:
                    await self._gas_set(first, 0.0)
                    first_on = False
                if second_on and consumed >= second_off_s:
                    await self._gas_set(second, 0.0)
                    second_on = False
        finally:
            self.progress.beam = None
            self._cycle_pause("reignite", False)   # exposure clock resumes/ends
            # However this step exits, no scheduled gas is left flowing.
            if first_on and first is not None:
                await self._gas_set(first, 0.0)
            if second_on and second is not None:
                await self._gas_set(second, 0.0)
            if simul_on:
                for gas in simul:
                    await self._gas_set(gas, 0.0)
            # Beam OFF between phases = plasma ground ON.
            await sup.set_valve(step.switch, True, reason="beam off")
            # ...and the bias follows it down bias_trail_s later, inside pump B.
            # Scheduled, not awaited: pump B starts on time either way.
            self._schedule_bias(step, False, step.bias_trail_s)

    # -- EE-CVD: beam held on for the whole run ----------------------------- #

    def _build_gas_plan(self) -> dict | None:
        """Per-cycle gas window boundaries for EE-CVD, in cycle-clock seconds.

        A cycle of length L divides into first (0 -> handoff) and second
        (handoff -> second_off). The single overlap moves each *incoming* gas
        earlier by that much, in both directions:

            second starts at handoff - overlap        (first -> second)
            first re-arms at second_off - overlap     (second -> first, which
                                                       wraps into the next cycle)

        There is no separate lead-in for the first gas: at a cycle boundary the
        thing it would lead is the previous cycle's second gas, which is exactly
        what the re-arm above expresses.
        """
        simul = [g for g in self._gas_schedules if g.order == "simultaneous"]
        if simul:
            # No windows to compute: every simultaneous gas covers the whole
            # cycle, which in EE-CVD (beam on all run) means it simply flows
            # from the first cycle to the end of the run. Supervisor.finish_run
            # zeroes it, the same as any other scheduled gas.
            return {"simultaneous": simul}
        first = next((g for g in self._gas_schedules if g.order == "first"), None)
        second = next((g for g in self._gas_schedules if g.order == "second"), None)
        if first is None and second is None:
            return None
        cyc = max(1e-6, self._cycle_len)
        ov = self._gas_overlap_s
        handoff = (first.pct / 100.0 * cyc) if first else 0.0
        second_off = min(
            cyc, handoff + (second.pct / 100.0 * cyc if second else 0.0))
        return {
            "first": first, "second": second, "cycle": cyc, "handoff": handoff,
            "second_on": max(0.0, handoff - ov),
            "second_off": second_off,
            "first_rearm": max(0.0, second_off - ov),
        }

    async def _sleep_lit(self, seconds: float) -> None:
        """Wait `seconds` of *lit* time - frozen whenever the plasma is out.

        Used for EE-CVD's pump A. Falls back to a wall-clock sleep if no beam
        watchdog is running, so a lit_gated step can never hang waiting for a
        clock nothing is advancing.
        """
        if seconds <= 0:
            return
        if self._beam_task is None or self._beam_task.done():
            await self._sleep(seconds)
            return
        target = self._lit_s + seconds
        self._clock_gated = True
        try:
            while self._lit_s < target and not self._abort.is_set():
                await self._pause.wait()
                self.progress.step_remaining_hint = max(0.0, target - self._lit_s)
                await asyncio.sleep(0.05)
        finally:
            self._clock_gated = False
            self.progress.step_remaining_hint = None

    async def _gas_set(self, schedule: GasSchedule, sccm: float) -> None:
        """Drive one scheduled gas, used by both modes' schedulers. A refused or
        failed setpoint is logged and swallowed: a gas that cannot be set must
        not take down the beam step or the watchdog around it."""
        try:
            await self.sup.set_mfc_setpoint(schedule.mfc, sccm)
        except Exception as exc:
            self.sup.report_event("error", f"gas schedule ({gas_display_name(schedule.mfc, self.gas_names())}): {exc}")

    async def _apply_cvd_gas(self, in_cycle: float) -> None:
        """Drive both scheduled gases to the state `in_cycle` calls for.

        Idempotent - it compares against the last state written, so calling it
        from both the watchdog tick and the cycle boundary is safe, and a
        skipped tick (a long reignite) cannot leave a gas stranded.

        Honest limit: windows are still only resolved to the watchdog's 0.2 s
        poll, so a window shorter than that inside a cycle may be missed. The
        cycle boundaries themselves are exact because _run calls this directly.
        """
        plan = self._gas_plan
        if not plan:
            return
        if plan.get("simultaneous"):
            # On once, then left alone - there is no boundary to cross.
            for gas in plan["simultaneous"]:
                if self._gas_on.get(gas.mfc) is not True:
                    await self._gas_set(gas, gas.flow_sccm)
                    self._gas_on[gas.mfc] = True
            return
        first, second = plan["first"], plan["second"]
        if first is not None:
            want = in_cycle < plan["handoff"] or in_cycle >= plan["first_rearm"]
            if want != self._gas_on.get(first.mfc):
                await self._gas_set(first, first.flow_sccm if want else 0.0)
                self._gas_on[first.mfc] = want
        if second is not None:
            want = plan["second_on"] <= in_cycle < plan["second_off"]
            if want != self._gas_on.get(second.mfc):
                await self._gas_set(second, second.flow_sccm if want else 0.0)
                self._gas_on[second.mfc] = want

    async def _beam_start(self, step: Step) -> None:
        """Turn the beam on and hand it to the background watchdog."""
        if not step.switch:
            raise ValueError("beam_start needs 'switch'")
        await self._kill_beam_watch()          # never run two watchdogs
        self._gas_plan = self._build_gas_plan()
        self._gas_on = {}
        self._beam_switch = step.switch
        # The bias leads the strike. Awaited rather than scheduled: this runs in
        # SETUP, before the cycling phase the run clock counts, so the lead
        # cannot push a cycle out - and there is no preceding step to hide it
        # in the way EE-ALD's pump A hides it.
        if step.bias_v:
            await self._set_bias(True, step)
            await asyncio.sleep(step.bias_lead_s)
        await self.sup.set_valve(step.switch, False, reason="beam on (EE-CVD)")
        # Same as _electron_beam: this flip is the strike, so let the current
        # settle before the watchdog starts judging - otherwise the watchdog's
        # first tick reignites a plasma that was still coming up.
        await asyncio.sleep(step.reignite_settle_s)
        self._beam_task = asyncio.create_task(
            self._beam_watch(step), name="beam-watch")

    async def _beam_stop(self, step: Step) -> None:
        """Stop the watchdog, ground the beam, and drop the bias behind it."""
        await self._kill_beam_watch()
        switch = step.switch or self._beam_switch
        if switch:
            await self.sup.set_valve(switch, True, reason="beam off (EE-CVD)")
        self._beam_switch = None
        self.progress.beam = None
        # Teardown, so the trail is awaited: nothing is waiting on this step's
        # length. On an ABORT the teardown is skipped entirely and _run's
        # finally drops the bias at once - safety over the trailing 0.2 s.
        if step.bias_v and self._bias_on:
            await asyncio.sleep(step.bias_trail_s)
            await self._set_bias(False, step)

    async def _kill_beam_watch(self) -> None:
        task, self._beam_task = self._beam_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _beam_watch(self, step: Step) -> None:
        """Hold the beam lit for an entire EE-CVD run, and drive the gas schedule.

        Two jobs, both on the same 0.2 s tick:

        1. Reignite. If |current| drops below min_current the plasma is out, so
           pulse the switch to restrike - the same protocol _electron_beam uses,
           just without an exposure budget to spend.
        2. The two clocks. `_lit_s` advances only while current is present, and
           is what a lit_gated pump A counts down against. `_cycle_clock`
           advances whenever the running step's own clock does - always during
           an ungated step, only while lit during a gated one - so pump A ends
           exactly as the cycle clock reaches the cycle length and the gas
           windows stay locked to the valves.

        The window boundaries live in _build_gas_plan/_apply_cvd_gas, which _run
        also calls at each cycle boundary so a boundary is never missed between
        ticks.
        """
        sup = self.sup
        strike_at = self.clock.elapsed()
        try:
            while True:
                if not self._pause.is_set():
                    # Same as EE-ALD: a pause grounds the beam rather than just
                    # freezing the clocks (2026-09-01). Skipping the rest of the
                    # tick also stops the watchdog reading its own grounded beam
                    # as a dead plasma and reigniting into a pause.
                    await sup.set_valve(step.switch, True, reason="paused - beam off")
                    self.progress.message = "paused - beam off"
                    await self._pause.wait()
                    await sup.set_valve(step.switch, False, reason="resumed - beam on")
                    strike_at = self.clock.elapsed()
                    continue
                t0 = self.clock.elapsed()
                await self._tick(BEAM_TICK_S)
                dt = self.clock.elapsed() - t0
                cur = sup.snapshot.get(step.ammeter)
                lit = isinstance(cur, (int, float)) and abs(cur) >= step.min_current
                self.progress.beam = {
                    "remaining": None,          # no exposure budget in EE-CVD
                    "current": float(cur) if isinstance(cur, (int, float)) else None,
                    "lit": lit,
                    "lit_s": self._lit_s,
                }
                # The cycle clock (and thus cycle progress) freezes exactly when
                # it would below: during a gated step (pump A) with no plasma.
                self._cycle_pause("reignite", self._clock_gated and not lit)
                if lit:
                    self._lit_s += dt
                else:
                    self.progress.message = "plasma out - reigniting"
                    sup.report_event("flag", "plasma extinguished during EE-CVD - reigniting")

                # The cycle clock tracks whatever the running step's own clock
                # is doing: always during an ungated step (the dose), only while
                # lit during a gated one (pump A). Pump A therefore ends exactly
                # when this reaches the cycle length - the gas windows and the
                # valves cannot drift apart.
                if lit or not self._clock_gated:
                    self._cycle_clock += dt
                    await self._apply_cvd_gas(self._cycle_clock)

                if not lit and self.clock.elapsed() - strike_at >= step.reignite_settle_s:
                    await self._reignite(step)
                    strike_at = self.clock.elapsed()
        finally:
            self.progress.beam = None
            self._cycle_pause("reignite", False)
            # Deliberately no MFC writes here: this task is normally stopped by
            # cancellation, and awaiting during cancellation is not reliable.
            # Every scheduled gas is zeroed by Supervisor.finish_run instead,
            # which runs for CVD as well as ALD.

    async def _reignite(self, step: Step) -> None:
        """Pulse the plasma-ground switch ON then OFF to restrike the plasma."""
        sup = self.sup
        await sup.set_valve(step.switch, True, reason="reignite pulse")   # beam off
        await asyncio.sleep(step.reignite_pulse_s)
        await sup.set_valve(step.switch, False, reason="reignite - beam on")
        await asyncio.sleep(step.reignite_settle_s)   # let current re-establish

    async def _wait_for_pressure(self, step: Step) -> None:
        if step.below_torr is None and step.above_torr is None:
            raise ValueError("wait_for_pressure needs 'below_torr' or 'above_torr'")
        deadline = self.clock.elapsed() + step.timeout_s
        while self.clock.elapsed() < deadline:
            if self._abort.is_set():
                return
            await self._pause.wait()
            p = self.sup.snapshot.get("pressure")
            if isinstance(p, (int, float)):
                if step.below_torr is not None and p <= step.below_torr:
                    return
                if step.above_torr is not None and p >= step.above_torr:
                    return
                self.progress.message = f"pressure {p:.3e} Torr, waiting"
            else:
                self.progress.message = "no pressure reading"
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"pressure did not reach the target within {step.timeout_s:g} s"
        )

    async def _tick(self, seconds: float) -> None:
        """Sleep up to `seconds`, cut short by an abort or an operator pause.

        The beam loops tick on this rather than a bare asyncio.sleep so that
        pressing Pause grounds the relay within milliseconds instead of at the
        end of the tick in flight - and so the exposure clock is charged only
        for time the beam was really on the sample (2026-09-01).
        """
        if seconds <= 0:
            return
        waiters = [asyncio.ensure_future(self._abort.wait()),
                   asyncio.ensure_future(self._paused.wait())]
        try:
            await asyncio.wait(waiters, timeout=seconds,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    async def _sleep(self, seconds: float, *, on_pause=None, on_resume=None) -> None:
        """Sleep `seconds` of RUN time, returning early if the run is aborted.

        Run time, not wall time: an operator pause stops the clock and the step
        resumes with exactly what was left of it. `on_pause` / `on_resume` are
        awaited around the held time, which is how a dose hands its valve back
        and takes it again (see _exec).

        Still one timed wait per stretch, not a poll loop: the scheduler's
        lateness is never re-added per iteration, so a busy UI cannot stretch a
        dose. A pause splits the sleep into stretches, and only the un-slept
        remainder is carried into the next one.
        """
        remaining = float(seconds)
        while remaining > 0:
            if self._abort.is_set():
                return
            t0 = self.clock.elapsed()
            # Wake on whichever comes first: the duration, an abort, or a pause.
            waiters = [asyncio.ensure_future(self._abort.wait()),
                       asyncio.ensure_future(self._paused.wait())]
            try:
                done, _ = await asyncio.wait(waiters, timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
            remaining -= self.clock.elapsed() - t0
            if self._abort.is_set():
                return
            if not done:
                return                      # slept the whole duration
            # Paused. Stop doing whatever this step does, wait it out, resume.
            if on_pause is not None:
                await on_pause()
            await self._pause.wait()
            if self._abort.is_set():
                return
            if on_resume is not None:
                await on_resume()
