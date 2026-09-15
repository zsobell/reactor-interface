"""Recipe schema and pure builders for UI-driven ALD/CVD experiments."""
from __future__ import annotations

from pathlib import Path
from typing import Literal
import yaml
import re

from .parameters import GAS_SCHEDULE_MFCS, GasParameters, RunParameters
from pydantic import BaseModel, Field

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

    # --- sample bias bracketing the beam ---
    # Operator request 2026-08-26: a live stage bias corrupts the stage
    # thermocouple, so the bias is only energised around the beam - on
    # `bias_lead_s` BEFORE the beam comes on, off `bias_trail_s` AFTER it goes
    # off, leaving clean TC data everywhere else in the cycle.
    #
    # bias_v == 0 means this step does not touch the sample bias at all, which
    # is what every file recipe gets: only build_ald_recipe/build_cvd_recipe
    # ever set these, and only when the run asked for a bias.
    #
    # Both flips are scheduled, never awaited in line (see
    # RecipeRunner._schedule_bias): the lead runs down inside the preceding
    # pump A and the trail inside pump B, so bracketing the beam does not make
    # a cycle one second longer than the sum of its steps.
    #
    # A reignite deliberately does NOT cycle the bias. It brackets the beam
    # STEP, not every flip of the plasma-ground relay - a 0.1 s reignite pulse
    # is shorter than the lead time, so chasing it would leave the bias off for
    # most of the restrike.
    bias_v: float = 0.0
    bias_polarity: int = 1
    bias_lead_s: float = Field(default=0.0, ge=0)
    bias_trail_s: float = Field(default=0.0, ge=0)

    def _bias_note(self) -> str:
        """The sample-bias bracket, for `describe()`. Empty when unset - a step
        that does not touch the bias must not claim to."""
        if not self.bias_v:
            return ""
        sign = "-" if self.bias_polarity < 0 else "+"
        if self.op == "beam_stop":
            return f", sample bias off {self.bias_trail_s:g} s later"
        return (f", sample bias {sign}{abs(self.bias_v):g} V on "
                f"{self.bias_lead_s:g} s early"
                + (f" / off {self.bias_trail_s:g} s late"
                   if self.op == "electron_beam" else ""))

    def describe(self, gas_names=None) -> str:
        """One line of operator-facing prose for this step. Used by the run
        parameters report, the UI's step readout and the `recipe_step` column,
        so an MFC is named by the GAS it is flowing, not by its channel id."""
        if self.op == "dose":
            return f"dose {self.valve} for {self.seconds:g} s"
        if self.op == "wait":
            return f"wait {self.seconds:g} s"
        if self.op == "valve":
            return f"valve {self.valve} -> {'open' if self.state else 'closed'}"
        if self.op == "set_flow":
            return f"set {gas_display_name(self.mfc, gas_names)} to {self.sccm:g} sccm"
        if self.op == "wait_for_pressure":
            if self.below_torr is not None:
                return f"wait for pressure below {self.below_torr:g} Torr"
            return f"wait for pressure above {self.above_torr:g} Torr"
        if self.op == "electron_beam":
            return (f"electron beam {self.seconds:g} s "
                    f"(|I|>{self.min_current*1e6:g} uA)" + self._bias_note())
        if self.op == "beam_start":
            return (f"beam on, held for the run "
                    f"(|I|>{self.min_current*1e6:g} uA)" + self._bias_note())
        if self.op == "beam_stop":
            return "beam off" + self._bias_note()
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

    **order="simultaneous"** is the third option, requested 2026-08-26: the
    gases do not divide the window at all, they both cover the whole of it, each
    at its own flow. `pct` is unused and the UI greys it out. It is a PAIRING -
    "if more than one MFC selects simultaneous then they both run together";
    exactly one gas set to simultaneous is rejected before the run starts rather
    than silently treated as a 100% "first", because a lone simultaneous gas is
    far more likely to be a half-finished edit than an intent.
    """

    mfc: str
    order: Literal["first", "second", "simultaneous"]
    #: percent (0-100) of the window (beam exposure, or cycle) this gas covers.
    #: Unused when order="simultaneous", which covers the whole window.
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



def _build_gas_schedules(p: dict | RunParameters) -> list[GasSchedule]:
    gases = p.gases if isinstance(p, RunParameters) else GasParameters.enabled(p)
    schedules = [GasSchedule(mfc=g.mfc, order=g.order, pct=g.pct, flow_sccm=g.flow_sccm)
                 for g in gases]

    for order in ("first", "second"):
        n = sum(1 for s in schedules if s.order == order)
        if n > 1:
            raise ValueError(
                f"at most one gas can be order='{order}' - "
                f"{n} are currently set to it"
            )
    # Simultaneous is a pairing, not a solo mode (operator, 2026-08-26): two or
    # more gases share the whole window, so exactly one is a half-finished edit
    # and the run is refused rather than started on a guess.
    simul = [s for s in schedules if s.order == "simultaneous"]
    if len(simul) == 1:
        others = [m for m in GAS_SCHEDULE_MFCS if m != simul[0].mfc]
        lone = gas_display_name(simul[0].mfc)
        raise ValueError(
            f"{lone} is set to Simultaneous on its own. "
            f"Simultaneous means two or more gases flow together for the whole "
            f"window, so set {' or '.join(map(gas_display_name, others))} to "
            f"Simultaneous as well (and switch it on), or put "
            f"{lone} back to First or Second."
        )
    return schedules


def build_ald_recipe(p: dict | RunParameters) -> Recipe:
    """Build an e-beam ALD recipe from UI parameters (no YAML file).

    Keys (all optional, with defaults): cycles, dose_s, pump_a_s, beam_s,
    pump_b_s, dose_pressure_torr, min_current_a, fill_pulse_on_s,
    fill_pulse_off_s, tolerance_frac, fill_valve, dose_valve, plasma_switch,
    gauge, ammeter, reignite_pulse_s, reignite_settle_s, name. Plus, per gas in
    GAS_SCHEDULE_MFCS: {gas}_gas_enable, {gas}_gas_order, {gas}_gas_pct,
    {gas}_gas_flow_sccm, plus the shared gas_overlap_s - see GasSchedule.
    """
    p = RunParameters.normalize(p, mode="ald")
    fill_valve, dose_valve = p.fill.valve, p.dose_valve
    plasma, gauge, ammeter = p.beam.switch, p.fill.gauge, p.beam.ammeter
    gas_schedules = _build_gas_schedules(p)
    bias = _bias_bracket(p.raw.get)

    return Recipe(
        name=p.name,
        cycles=p.cycles,
        setup=[
            Step(op="valve", valve=plasma, state=True),   # beam off to start
            Step(op="start_fill", valve=fill_valve, gauge=gauge,
                 target_torr=p.fill.target_torr,
                 pulse_on_s=p.fill.pulse_on_s,
                 pulse_off_s=p.fill.pulse_off_s,
                 tolerance_frac=p.fill.tolerance_frac),
            # Scheduled gases start OFF - they only turn on proximal to the
            # beam exposure, never for the whole run.
            *[Step(op="set_flow", mfc=gs.mfc, sccm=0.0) for gs in gas_schedules],
        ],
        steps=[
            Step(op="dose", valve=dose_valve, seconds=p.dose_s),
            Step(op="wait", seconds=p.pump_a_s),
            Step(op="electron_beam", switch=plasma, ammeter=ammeter,
                 min_current=p.beam.min_current,
                 seconds=p.beam_s,
                 reignite_pulse_s=p.beam.pulse_s,
                 reignite_settle_s=p.beam.settle_s, **bias),
            Step(op="wait", seconds=p.pump_b_s),
        ],
        teardown=_end_of_run(p, plasma),
        gas_schedules=gas_schedules,
        gas_overlap_s=p.gas_overlap_s,
    )


def _end_of_run(p: RunParameters, plasma: str) -> list[Step]:
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
        Step(op="set_flow", mfc=p.ar_mfc, sccm=0.0),
        Step(op="wait", seconds=p.ar_close_delay_s),
        Step(op="valve", valve=p.ar_valve, state=False),
        Step(op="valve", valve=plasma, state=False),   # beam ON mode
        Step(op="message", text="run complete"),
    ]


def build_cvd_recipe(p: dict | RunParameters) -> Recipe:
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
    p = RunParameters.normalize(p, mode="cvd")
    fill_valve, dose_valve = p.fill.valve, p.dose_valve
    plasma, gauge, ammeter = p.beam.switch, p.fill.gauge, p.beam.ammeter
    gas_schedules = _build_gas_schedules(p)
    bias = _bias_bracket(p.raw.get)

    beam = dict(
        switch=plasma, ammeter=ammeter,
        min_current=p.beam.min_current,
        reignite_pulse_s=p.beam.pulse_s,
        reignite_settle_s=p.beam.settle_s, **bias,
    )
    return Recipe(
        name=p.name,
        mode="cvd",
        cycles=p.cycles,
        setup=[
            Step(op="valve", valve=plasma, state=True),   # beam off to start
            Step(op="start_fill", valve=fill_valve, gauge=gauge,
                 target_torr=p.fill.target_torr,
                 pulse_on_s=p.fill.pulse_on_s,
                 pulse_off_s=p.fill.pulse_off_s,
                 tolerance_frac=p.fill.tolerance_frac),
            *[Step(op="set_flow", mfc=gs.mfc, sccm=0.0) for gs in gas_schedules],
            # Beam on for the rest of the run, with the reignite watchdog.
            Step(op="beam_start", **beam),
        ],
        steps=[
            # Dose on wall clock - never gated (see the docstring).
            Step(op="dose", valve=dose_valve, seconds=p.dose_s),
            # Pump A freezes with the plasma, in lockstep with the gas clock.
            Step(op="wait", seconds=p.pump_a_s, lit_gated=True),
        ],
        # beam_stop first: it kills the watchdog (and grounds the beam) so the
        # shared end-of-run sequence can leave the switch where the operator
        # wants it without the watchdog fighting it.
        teardown=[Step(op="beam_stop", switch=plasma, **bias), *_end_of_run(p, plasma)],
        gas_schedules=gas_schedules,
        gas_overlap_s=p.gas_overlap_s,
    )




def gas_display_name(mfc_id: str, names=None) -> str:
    if name := (names or {}).get(mfc_id):
        return name
    match = re.fullmatch(r"mfc(\d+)", mfc_id or "")
    return f"MFC {match.group(1)}" if match else mfc_id


def _bias_bracket(g) -> dict:
    """The sample-bias bracket, from UI params, for a beam step.

    Empty when the run's Sample bias field is zero: no bias wanted means the
    beam steps leave the supply alone entirely, exactly as they did before
    2026-08-26. The magnitude is what reaches the instrument (the 2260B is
    single-quadrant); the polarity is lead-orientation bookkeeping and only
    signs the logged value - see Supervisor.set_sample_bias_output.
    """
    volts = abs(float(g("sample_bias_v", 0.0) or 0.0))
    if volts <= 0.0:
        return {}
    return dict(
        bias_v=volts,
        bias_polarity=-1 if int(g("sample_bias_polarity", 1) or 1) < 0 else 1,
        bias_lead_s=float(g("sample_bias_lead_s", 0.2)),
        bias_trail_s=float(g("sample_bias_trail_s", 0.2)),
    )

