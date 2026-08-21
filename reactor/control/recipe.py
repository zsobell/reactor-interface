"""Declarative dosing sequences and the engine that runs them.

Recipes are YAML, not code, so changing a dose time is not a programming task.

Timing runs on its own asyncio task using **absolute deadlines** off the event
loop clock. The original program timed steps with a 1 s Express-VI delay inside
the same loop that redrew the front panel, so step length drifted whenever the UI
got busy. Here, a slow UI cannot stretch a dose.

Honest limit: software timing on Windows has roughly 1-15 ms of jitter. Fine for
doses of tens of milliseconds and up, and better than the original, but not
deterministic. If you need tighter than ~10 ms, the answer is a hardware-timed
DAQmx digital output task, which this structure can accommodate without changing
the recipe format.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
#  Recipe schema
# --------------------------------------------------------------------------- #


class Step(BaseModel):
    op: Literal[
        "dose", "wait", "valve", "set_flow", "wait_for_pressure", "message",
        "electron_beam", "beam_start", "beam_stop", "start_fill", "stop_fill",
    ]
    # dose / valve
    valve: str | None = None
    state: bool | None = None
    # timing
    seconds: float | None = Field(default=None, ge=0)
    #: `wait` only: count down while the plasma is lit, freezing during a
    #: reignite. Used for EE-CVD's pump A so it stays locked to the gas clock.
    #: Never set this on a `dose` - a frozen precursor pulse holds its valve
    #: open and dumps precursor into the chamber.
    lit_gated: bool = False
    # flow
    mfc: str | None = None
    sccm: float | None = None
    # pressure gate
    below_torr: float | None = None
    above_torr: float | None = None
    timeout_s: float = 3600.0
    # annotation
    text: str = ""

    # --- pressure-regulated fill (start_fill) ---
    # Pulse `valve` to hold `gauge` at `target_torr`; flag if it drifts more than
    # tolerance_frac off. Runs in the background until stop_fill.
    gauge: str | None = None
    target_torr: float | None = None
    pulse_on_s: float = 0.1
    pulse_off_s: float = 0.3
    tolerance_frac: float = 0.2

    # --- electron beam / plasma (electron_beam) ---
    # Beam ON = the plasma-ground `switch` OFF. Hold `seconds` of exposure while
    # |current on `ammeter`| >= min_current; if it drops, pulse the switch to
    # reignite and only count exposure while current is present.
    #
    # A full retry attempt is the 0.2s current-check poll (below) plus
    # reignite_pulse_s + reignite_settle_s; requested by the operator to run at
    # least 2x/sec, so the pair must sum to <=0.3s. These are guessed defaults
    # (0.10/0.15s -> ~0.45s/attempt, ~2.2/s) - untested against how long the
    # relay/plasma physically needs to restrike; tune from the ALD run panel if
    # a real run shows they need to be longer.
    switch: str | None = None
    ammeter: str = "inst.ammeter"
    min_current: float = 5.0e-4          # 500 uA absolute
    reignite_pulse_s: float = 0.10
    reignite_settle_s: float = 0.15

    def describe(self) -> str:
        if self.op == "dose":
            return f"dose {self.valve} for {self.seconds:g} s"
        if self.op == "wait":
            return f"wait {self.seconds:g} s"
        if self.op == "valve":
            return f"valve {self.valve} -> {'open' if self.state else 'closed'}"
        if self.op == "set_flow":
            return f"set {self.mfc} to {self.sccm:g} sccm"
        if self.op == "wait_for_pressure":
            if self.below_torr is not None:
                return f"wait for pressure below {self.below_torr:g} Torr"
            return f"wait for pressure above {self.above_torr:g} Torr"
        if self.op == "electron_beam":
            return f"electron beam {self.seconds:g} s (|I|>{self.min_current*1e6:g} uA)"
        if self.op == "beam_start":
            return f"beam on, held for the run (|I|>{self.min_current*1e6:g} uA)"
        if self.op == "beam_stop":
            return "beam off"
        if self.op == "start_fill":
            return f"regulate {self.gauge} to {self.target_torr:g} Torr via {self.valve}"
        if self.op == "stop_fill":
            return "stop fill regulation"
        return self.text or self.op


class GasSchedule(BaseModel):
    """Turns one MFC on/off automatically once per cycle.

    Each enabled gas covers `pct` of a window, in the order given, and the
    handoff between them is governed by a single `Recipe.gas_overlap_s`: the
    incoming gas starts that many seconds before the outgoing one stops. The
    same number applies in both directions, first->second and second->first
    (which for EE-CVD wraps into the next cycle).

    What the window is depends on the run mode:

    - EE-ALD (mode="ald"): the cycle's electron_beam exposure. Requested by the
      operator - H2/N2 should only flow proximal to the beam exposure, not for
      the whole cycle. Boundaries are measured in *exposure* seconds, the same
      accumulator the reignite logic freezes, so the schedule freezes with it.
      The "first" gas additionally leads the beam step's start by the overlap,
      scheduled off wall clock during dose/pump A (nothing is lit yet, so there
      is no exposure clock to measure against).
    - EE-CVD (mode="cvd"): the whole cycle (dose + pump A), since the beam is on
      for the entire run and there is no beam step to anchor to. Measured on the
      per-cycle clock in RecipeRunner._beam_watch, which advances in lockstep
      with the cycle's own steps - see build_cvd_recipe.

    At most one schedule may have order="first" and at most one "second"
    (validated by _build_gas_schedules before a run starts).
    """

    mfc: str
    order: Literal["first", "second"]
    #: percent (0-100) of the window (beam exposure, or cycle) this gas covers
    pct: float = Field(ge=0, le=100)
    #: setpoint while on; separate from any setpoint set by hand on the tile
    flow_sccm: float = Field(ge=0)


class Recipe(BaseModel):
    name: str = "untitled"
    notes: str = ""
    #: "ald" = beam fires for a fixed exposure inside each cycle; "cvd" = beam
    #: is held on for the whole run and the cycle is just dose + pump A. Drives
    #: which clock the gas schedules are measured against (see GasSchedule).
    mode: Literal["ald", "cvd"] = "ald"
    setup: list[Step] = Field(default_factory=list)
    cycles: int = Field(default=1, ge=1)
    steps: list[Step] = Field(default_factory=list)
    teardown: list[Step] = Field(default_factory=list)
    #: Per-gas on/off windows, once per cycle. Empty for plain file recipes.
    gas_schedules: list[GasSchedule] = Field(default_factory=list)
    #: Seconds the incoming gas starts before the outgoing one stops, applied at
    #: every handoff. See GasSchedule.
    gas_overlap_s: float = Field(default=0.0, ge=0)

    @staticmethod
    def load(path: Path | str) -> "Recipe":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return Recipe.model_validate(data)

    def cycle_seconds(self) -> float:
        return sum(s.seconds or 0.0 for s in self.steps)


#: Poll period for the beam's current check and the EE-CVD reignite watchdog.
#: The operator asked for a check at least twice a second; the last tick of a
#: timed beam step is clamped short of this so the step cannot overrun.
BEAM_TICK_S = 0.2

#: MFCs eligible for beam-proximal gas scheduling. Ar is excluded - it's
#: regulated separately (see the isolation-valve interlock), not gated to the
#: beam exposure.
GAS_SCHEDULE_MFCS = ("h2", "n2")


def _build_gas_schedules(p: dict) -> list[GasSchedule]:
    schedules = []
    for mfc in GAS_SCHEDULE_MFCS:
        if not p.get(f"{mfc}_gas_enable"):
            continue
        order = p.get(f"{mfc}_gas_order", "first")
        if order not in ("first", "second"):
            raise ValueError(f"{mfc}: gas order must be 'first' or 'second'")
        schedules.append(GasSchedule(
            mfc=mfc, order=order,
            pct=float(p.get(f"{mfc}_gas_pct", 100.0)),
            flow_sccm=float(p.get(f"{mfc}_gas_flow_sccm", 0.0)),
        ))

    for order in ("first", "second"):
        n = sum(1 for s in schedules if s.order == order)
        if n > 1:
            raise ValueError(
                f"at most one gas can be order='{order}' - "
                f"{n} are currently set to it"
            )
    return schedules


def build_ald_recipe(p: dict) -> Recipe:
    """Build an e-beam ALD recipe from UI parameters (no YAML file).

    Keys (all optional, with defaults): cycles, dose_s, pump_a_s, beam_s,
    pump_b_s, dose_pressure_torr, min_current_a, fill_pulse_on_s,
    fill_pulse_off_s, tolerance_frac, fill_valve, dose_valve, plasma_switch,
    gauge, ammeter, reignite_pulse_s, reignite_settle_s, name. Plus, per gas in
    GAS_SCHEDULE_MFCS: {gas}_gas_enable, {gas}_gas_order, {gas}_gas_pct,
    {gas}_gas_flow_sccm, plus the shared gas_overlap_s - see GasSchedule.
    """
    g = lambda k, d: p.get(k, d)  # noqa: E731
    fill_valve = g("fill_valve", "rpm_top")
    dose_valve = g("dose_valve", "prec1")
    plasma = g("plasma_switch", "plasma_ground")
    gauge = g("gauge", "gauge.prec1_dose")
    ammeter = g("ammeter", "inst.ammeter")
    gas_schedules = _build_gas_schedules(p)

    return Recipe(
        name=g("name", "ALD + e-beam (precursor 1)"),
        cycles=int(g("cycles", 100)),
        setup=[
            Step(op="valve", valve=plasma, state=True),   # beam off to start
            Step(op="start_fill", valve=fill_valve, gauge=gauge,
                 target_torr=float(g("dose_pressure_torr", 0.02)),
                 pulse_on_s=float(g("fill_pulse_on_s", 0.10)),
                 pulse_off_s=float(g("fill_pulse_off_s", 0.30)),
                 tolerance_frac=float(g("tolerance_frac", 0.20))),
            # Scheduled gases start OFF - they only turn on proximal to the
            # beam exposure, never for the whole run.
            *[Step(op="set_flow", mfc=gs.mfc, sccm=0.0) for gs in gas_schedules],
        ],
        steps=[
            Step(op="dose", valve=dose_valve, seconds=float(g("dose_s", 0.05))),
            Step(op="wait", seconds=float(g("pump_a_s", 10.0))),
            Step(op="electron_beam", switch=plasma, ammeter=ammeter,
                 min_current=float(g("min_current_a", 5.0e-4)),
                 seconds=float(g("beam_s", 5.0)),
                 reignite_pulse_s=float(g("reignite_pulse_s", 0.10)),
                 reignite_settle_s=float(g("reignite_settle_s", 0.15))),
            Step(op="wait", seconds=float(g("pump_b_s", 10.0))),
        ],
        teardown=_end_of_run(g, plasma),
        gas_schedules=gas_schedules,
        gas_overlap_s=float(g("gas_overlap_s", 0.0)),
    )


def _end_of_run(g, plasma: str) -> list[Step]:
    """The operator's end-of-run sequence, shared by EE-ALD and EE-CVD.

    Requested behaviour, in this order: zero the Ar setpoint, hold the Ar
    isolation valve open a further `ar_close_delay_s` (timed from the end of the
    last cycle, i.e. the start of teardown) so the line keeps purging, then close
    it, and finally leave the plasma ground OFF - which is beam-ON mode, the
    state the operator wants the tool parked in.

    The Ar setpoint is zeroed *before* the valve closes because the MFC refuses a
    nonzero setpoint while its isolation valve is shut, so closing first would
    strand a live setpoint behind a closed valve.

    Note Supervisor.finish_run runs after this and zeroes every MFC and closes
    the fill valve; it does not touch the plasma switch, so the beam-on state set
    here survives. `_run`'s beam-off safety only fires if a beam_stop has not
    already cleared `_beam_switch`, which the EE-CVD teardown does first.
    """
    return [
        Step(op="set_flow", mfc=g("ar_mfc", "ar"), sccm=0.0),
        Step(op="wait", seconds=float(g("ar_close_delay_s", 10.0))),
        Step(op="valve", valve=g("ar_valve", "ar_pneumatic"), state=False),
        Step(op="valve", valve=plasma, state=False),   # beam ON mode
        Step(op="message", text="run complete"),
    ]


def build_cvd_recipe(p: dict) -> Recipe:
    """Build an electron-enhanced CVD run from UI parameters (no YAML file).

    Same parameters as build_ald_recipe minus beam_s and pump_b_s, which do not
    exist here: the beam is turned on once at the start of the run and held on
    until the end, and a cycle is just dose + pump A.

    Everything that can freeze, freezes together, so the gas windows stay locked
    to the cycle no matter how often the plasma drops out:

    - Pump A is `lit_gated`: it only counts down while current is present, so a
      reignite pauses it.
    - The per-cycle gas clock advances on exactly the same condition (see
      RecipeRunner._beam_watch), so pump A ends precisely when that clock
      reaches the cycle length. No drift.
    - The dose is deliberately NOT gated. Freezing a precursor pulse because the
      beam went out would hold the dose valve open and dump precursor into the
      chamber; the pulse runs on wall clock and closes its valve on the way out
      whatever else is happening.
    """
    g = lambda k, d: p.get(k, d)  # noqa: E731
    fill_valve = g("fill_valve", "rpm_top")
    dose_valve = g("dose_valve", "prec1")
    plasma = g("plasma_switch", "plasma_ground")
    gauge = g("gauge", "gauge.prec1_dose")
    ammeter = g("ammeter", "inst.ammeter")
    gas_schedules = _build_gas_schedules(p)

    beam = dict(
        switch=plasma, ammeter=ammeter,
        min_current=float(g("min_current_a", 5.0e-4)),
        reignite_pulse_s=float(g("reignite_pulse_s", 0.10)),
        reignite_settle_s=float(g("reignite_settle_s", 0.15)),
    )
    return Recipe(
        name=g("name", "EE-CVD (precursor 1)"),
        mode="cvd",
        cycles=int(g("cycles", 100)),
        setup=[
            Step(op="valve", valve=plasma, state=True),   # beam off to start
            Step(op="start_fill", valve=fill_valve, gauge=gauge,
                 target_torr=float(g("dose_pressure_torr", 0.02)),
                 pulse_on_s=float(g("fill_pulse_on_s", 0.10)),
                 pulse_off_s=float(g("fill_pulse_off_s", 0.30)),
                 tolerance_frac=float(g("tolerance_frac", 0.20))),
            *[Step(op="set_flow", mfc=gs.mfc, sccm=0.0) for gs in gas_schedules],
            # Beam on for the rest of the run, with the reignite watchdog.
            Step(op="beam_start", **beam),
        ],
        steps=[
            # Dose on wall clock - never gated (see the docstring).
            Step(op="dose", valve=dose_valve, seconds=float(g("dose_s", 0.05))),
            # Pump A freezes with the plasma, in lockstep with the gas clock.
            Step(op="wait", seconds=float(g("pump_a_s", 10.0)), lit_gated=True),
        ],
        # beam_stop first: it kills the watchdog (and grounds the beam) so the
        # shared end-of-run sequence can leave the switch where the operator
        # wants it without the watchdog fighting it.
        teardown=[Step(op="beam_stop", switch=plasma), *_end_of_run(g, plasma)],
        gas_schedules=gas_schedules,
        gas_overlap_s=float(g("gas_overlap_s", 0.0)),
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
    step_started: float | None = None
    step_duration: float | None = None
    started_at: float | None = None
    message: str = ""
    error: str = ""
    #: live beam status while an electron_beam step runs, else None
    beam: dict | None = None
    #: set by a lit_gated step, whose remaining time is not a wall-clock
    #: countdown and so cannot be derived from step_started/step_duration
    step_remaining_hint: float | None = None
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
        elif self.step_started and self.step_duration:
            remaining = max(0.0, self.step_duration - (time.time() - self.step_started))
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

    def __init__(self, supervisor) -> None:
        self.sup = supervisor
        self.progress = RecipeProgress()
        self._task: asyncio.Task | None = None
        self._pause = asyncio.Event()
        self._pause.set()               # set == not paused
        self._abort = asyncio.Event()
        self._gas_schedules: list[GasSchedule] = []
        self._gas_lead_tasks: list[asyncio.Task] = []
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
        self._gas_on: dict[str, bool | None] = {"first": None, "second": None}
        self._gas_overlap_s = 0.0
        # Fractional-cycle bookkeeping for property-vs-cycle plotting. Progress
        # through a cycle is wall time since the cycle began MINUS time spent
        # frozen (reignite or operator pause) - computable at any log instant,
        # and correct for both ALD (beam exposure freezes on a dead plasma) and
        # CVD (the lit-gated pump A freezes). Pauses are reference-counted so
        # overlapping reasons nest cleanly.
        self._cycle_start_wall: float | None = None
        self._cycle_paused_accum = 0.0
        self._pause_start: float | None = None
        self._pause_reasons: set[str] = set()

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
            self._pause_start = time.time()
        elif was and not now_active and self._pause_start is not None:
            self._cycle_paused_accum += time.time() - self._pause_start
            self._pause_start = None

    def _begin_cycle_clock(self) -> None:
        """Reset the cycle-progress clock at the start of a cycle."""
        self._cycle_start_wall = time.time()
        self._cycle_paused_accum = 0.0
        self._pause_start = self._cycle_start_wall if self._pause_reasons else None

    @property
    def cycle_paused(self) -> bool:
        return bool(self._pause_reasons) and self._cycle_start_wall is not None

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
        """Fractional cycle number at `now`, or None outside the cycling phase.
        Whole part = completed cycles; fraction = frozen-adjusted progress
        through the current cycle's predicted length (Recipe.cycle_seconds)."""
        if (self.progress.phase != "cycling" or self.progress.cycle <= 0
                or self._cycle_len <= 0 or self._cycle_start_wall is None):
            return None
        now = time.time() if now is None else now
        paused = self._cycle_paused_accum
        if self._pause_start is not None:
            paused += now - self._pause_start
        prog = max(0.0, (now - self._cycle_start_wall) - paused)
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

    async def start(self, recipe: Recipe) -> None:
        if self.busy:
            raise RuntimeError("a recipe is already running")
        self._pause.set()
        self._abort.clear()
        self._gas_schedules = []
        self._gas_lead_tasks = []
        self._beam_task = None
        self._beam_switch = None
        self._lit_s = 0.0
        self._cycle_clock = 0.0
        self._clock_gated = False
        self._cycle_len = recipe.cycle_seconds()
        self._gas_plan = None
        self._gas_on = {"first": None, "second": None}
        self._gas_overlap_s = recipe.gas_overlap_s
        self._cycle_start_wall = None
        self._cycle_paused_accum = 0.0
        self._pause_start = None
        self._pause_reasons = set()
        self.progress = RecipeProgress(
            state="running", recipe=recipe.name,
            cycles_total=recipe.cycles, started_at=time.time(),
        )
        self._task = asyncio.create_task(self._run(recipe), name="recipe")

    def pause(self) -> None:
        if self.busy and self.progress.state == "running":
            self._pause.clear()
            self.progress.state = "paused"
            self._cycle_pause("operator", True)   # freeze cycle progress too

    def resume(self) -> None:
        if self.busy and self.progress.state == "paused":
            self._pause.set()
            self.progress.state = "running"
            self._cycle_pause("operator", False)

    async def abort(self) -> None:
        if not self.busy:
            return
        self.progress.state = "aborting"
        self._abort.set()
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
            first_gas = next((g for g in self._gas_schedules if g.order == "first"), None)

            self.progress.phase = "cycling"
            for cycle in range(1, recipe.cycles + 1):
                if self._abort.is_set():
                    break
                self.progress.cycle = cycle
                self._begin_cycle_clock()
                if recipe.mode == "cvd":
                    # Restart the cycle clock, and evaluate the windows here at
                    # 0 rather than leaving it to the watchdog's next tick: that
                    # tick can land past a boundary and strand a gas on into the
                    # next cycle.
                    self._cycle_clock = 0.0
                    await self._apply_cvd_gas(0.0)
                elif first_gas is not None:
                    delay = max(0.0, beam_runway_s - self._gas_overlap_s)
                    self._gas_lead_tasks.append(asyncio.create_task(
                        self._fire_gas_lead(first_gas, delay), name="gas-lead-in"))
                await self._run_steps(recipe.steps)

            self._cycle_start_wall = None       # cycle progress stops after cycling
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
            self._gas_lead_tasks = []
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
            self.progress.step_desc = step.describe()
            self.progress.step_duration = step.seconds
            self.progress.step_started = time.time()
            self.progress.step_remaining_hint = None
            await self._exec(step)
        self.progress.step_duration = None
        self.progress.step_started = None

    async def _exec(self, step: Step) -> None:
        sup = self.sup

        if step.op == "dose":
            if not step.valve or step.seconds is None:
                raise ValueError("dose needs 'valve' and 'seconds'")
            # A dose is a pulse: open, hold, close. The close in `finally` is part
            # of the operation, so an aborted dose still ends with its own valve
            # returned to where it started.
            await sup.set_valve(step.valve, True, reason="recipe dose")
            try:
                await self._sleep(step.seconds)
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
            await self.sup.set_mfc_setpoint(schedule.mfc, schedule.flow_sccm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.sup._event("error", f"gas lead-in ({schedule.mfc}): {exc}")

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
        # Handoff point: where "first" ends and "second" starts. The single
        # overlap moves the incoming gas earlier, same as in EE-CVD.
        handoff_s = (first.pct / 100.0 * total) if first else 0.0
        second_on_s = max(0.0, handoff_s - self._gas_overlap_s)
        second_off_s = min(total, handoff_s + (second.pct / 100.0 * total if second else 0.0))
        first_on = False
        second_on = False

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
        # it counts as the exposure it is, so `seconds` is honest wall time.
        strike_at = time.time()
        try:
            if first is not None:
                # Defensive re-assert: the lead-in task should already have
                # turned this on before this step started, but this step is
                # the one place that actually needs it on, so make sure.
                await self._gas_set(first, first.flow_sccm)
                first_on = True

            remaining = total
            while remaining > 0 and not self._abort.is_set():
                await self._pause.wait()
                t0 = time.time()
                # Clamp the last tick to what is actually left. A fixed 0.2 s
                # tick overshot the step by up to a full tick every time it ran
                # (0.1 s on average), which across 150 cycles is minutes.
                await asyncio.sleep(min(BEAM_TICK_S, remaining))
                dt = time.time() - t0            # unaffected by pausing
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
                    sup._event("flag", "plasma extinguished during beam - reigniting")
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
            # Beam OFF between phases = plasma ground ON.
            await sup.set_valve(step.switch, True, reason="beam off")

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
            self.sup._event("error", f"gas schedule ({schedule.mfc}): {exc}")

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
        first, second = plan["first"], plan["second"]
        if first is not None:
            want = in_cycle < plan["handoff"] or in_cycle >= plan["first_rearm"]
            if want != self._gas_on["first"]:
                await self._gas_set(first, first.flow_sccm if want else 0.0)
                self._gas_on["first"] = want
        if second is not None:
            want = plan["second_on"] <= in_cycle < plan["second_off"]
            if want != self._gas_on["second"]:
                await self._gas_set(second, second.flow_sccm if want else 0.0)
                self._gas_on["second"] = want

    async def _beam_start(self, step: Step) -> None:
        """Turn the beam on and hand it to the background watchdog."""
        if not step.switch:
            raise ValueError("beam_start needs 'switch'")
        await self._kill_beam_watch()          # never run two watchdogs
        self._gas_plan = self._build_gas_plan()
        self._gas_on = {"first": None, "second": None}
        self._beam_switch = step.switch
        await self.sup.set_valve(step.switch, False, reason="beam on (EE-CVD)")
        # Same as _electron_beam: this flip is the strike, so let the current
        # settle before the watchdog starts judging - otherwise the watchdog's
        # first tick reignites a plasma that was still coming up.
        await asyncio.sleep(step.reignite_settle_s)
        self._beam_task = asyncio.create_task(
            self._beam_watch(step), name="beam-watch")

    async def _beam_stop(self, step: Step) -> None:
        """Stop the watchdog and ground the beam."""
        await self._kill_beam_watch()
        switch = step.switch or self._beam_switch
        if switch:
            await self.sup.set_valve(switch, True, reason="beam off (EE-CVD)")
        self._beam_switch = None
        self.progress.beam = None

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
        try:
            while True:
                await self._pause.wait()
                t0 = time.time()
                await asyncio.sleep(BEAM_TICK_S)
                dt = time.time() - t0
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
                    sup._event("flag", "plasma extinguished during EE-CVD - reigniting")

                # The cycle clock tracks whatever the running step's own clock
                # is doing: always during an ungated step (the dose), only while
                # lit during a gated one (pump A). Pump A therefore ends exactly
                # when this reaches the cycle length - the gas windows and the
                # valves cannot drift apart.
                if lit or not self._clock_gated:
                    self._cycle_clock += dt
                    await self._apply_cvd_gas(self._cycle_clock)

                if not lit:
                    await self._reignite(step)
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
        deadline = time.time() + step.timeout_s
        while time.time() < deadline:
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

    async def _sleep(self, seconds: float) -> None:
        """Sleep `seconds`, returning early if the run is aborted.

        One timed wait, not a poll loop: the scheduler's lateness is never
        re-added per iteration, so a busy UI cannot stretch a dose.
        """
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._abort.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return       # slept the full duration
