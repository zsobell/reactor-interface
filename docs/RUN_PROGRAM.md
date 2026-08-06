# The run program: EE-ALD, EE-CVD, and pre-start

The electron-beam run modes, the recipe engine underneath them, and the GUI
around them. First built 2026-08-01 as a single ALD-only workflow; the run
panel now supports two modes and a pre-start sequence, added 2026-08-05.
Control logic is verified with fake-DAQ / fake-supervisor harnesses in the
scratchpad on every change; **the whole program has not yet been run on real
hardware** (tracked in `reactor-alz`).

## Two run modes, one panel

The **Run** tab's run panel has a mode selector: **EE-ALD** or **EE-CVD**.
Both share cycles, dose pressure, dose time, pump A, min current, the
fill-pulse/tolerance/reignite advanced-timing fields, and gas scheduling.
They differ in what happens to the electron beam.

### EE-ALD — pulsed beam, one exposure per cycle

Repeat N cycles, all durations set in the UI:

1. **Dose precursor 1** — open the micro-pulse valve (`prec1`, cDAQ1Mod3
   line0) for a set time, dosing into the chamber from the charged full
   volume.
2. **Pump A** — wait, wall clock.
3. **Electron beam** — plasma-ground relay **OFF** (= beam ON), hold a set
   *exposure* time while watching sample current.
4. **Pump B** — wait, wall clock.

### EE-CVD — continuous beam, dosing on top of it

No beam-exposure or pump-B field — the beam turns on once at run start and
stays on until the run ends (or is aborted). A cycle is just:

1. **Dose precursor 1** — same micro-pulse valve, same wall-clock timing,
   **never gated by the plasma**. A frozen precursor pulse would hold the
   dose valve open and dump precursor into the chamber, so this step ignores
   plasma state entirely and always closes its valve on the way out.
2. **Pump A** — waits, but *counts down only while the plasma is lit*
   (`Step.lit_gated=True`). A reignite pauses it, exactly like EE-ALD's
   exposure clock pauses during a reignite.

The reignite watchdog (`RecipeRunner._beam_watch`) runs for the whole EE-CVD
run, restriking on dropout the same way EE-ALD's `_electron_beam` does, and
grounds the beam on teardown, abort, or a crash — never left energized.

## What runs continuously alongside either mode

- **Full-volume pressure regulation** (the *whole* run, not per-cycle): a
  background loop pulses the fill valve (`rpm_top`, right manifold top —
  switch to `rpm_bottom` if reversed; unverified, see `reactor-zo0`) whenever
  the precursor-1 dose Baratron (`gauge.prec1_dose`, ai1 — labelling
  unverified, see `reactor-gkw`) reads below the setpoint, holding the full
  volume charged. Flags gently if it drifts more than the tolerance off
  setpoint; never stops anything.
- **Current monitoring during any beam-on period**: exposure/lit-time only
  accumulates while `|Keithley current|` ≥ `min_current`. If the plasma
  extinguishes, the switch is pulsed ON→OFF to reignite (default
  0.10 s pulse / 0.15 s settle, ≈2.2 attempts/s including the 0.2 s
  current-check poll — tune from the UI's Advanced timing panel, which shows
  the achieved rate live).

## Gas scheduling — a single overlap, not per-gas leads

H2/N2 can each be scheduled on/off around the run's beam-on period instead of
flowing the whole cycle. At most one gas is `first`, at most one is
`second`. There is **one** `Overlap (s)` field, not a lead time per gas: the
incoming gas starts that many seconds before the outgoing one stops, applied
at every handoff — first→second, and second→first (which for EE-CVD wraps
into the next cycle as the "first" gas re-arming near the end of this one).

What the percentage is measured against depends on the mode:

- **EE-ALD**: the beam step's exposure time. Boundaries are evaluated on the
  same accumulated-exposure clock the reignite logic freezes, so the gas
  schedule freezes with it during a reignite or an operator pause. `first`
  additionally leads the beam step's start by the overlap, scheduled on wall
  clock during dose/pump A (nothing is lit yet, so there is no exposure clock
  to measure against there).
- **EE-CVD**: the whole cycle (dose + pump A). Because pump A is
  `lit_gated`, this is exact, not driftable — pump A always ends the instant
  the cycle's lit-time clock reaches the cycle length, so the gas windows and
  the valves cannot come apart no matter how many reignites happen. Doses
  themselves are never gated (see above), so the gas schedule and the dose
  timing don't line up if the run has reignited recently — only pump A
  freezes.

The UI computes the exact same numbers as `RecipeRunner` and shows them live
next to the Gas scheduling panel's title (e.g. `H2 on beam−0.50s · off
beam+2.00s`), including the same order-collision check the server enforces
with a 409.

## Pre-start — bring the tool up to a struck, beam-off state

A separate button and sequence (`Supervisor.start_prestart`), not part of
either run mode, for getting the tool ready before pressing Start:

1. Confirmation dialog: *"Set Ar Pneumatic, Plasma Ground, and Precursor Fill
   to Remote. Turn on output for power supplies."*
2. Open the Ar pneumatic isolation valve.
3. Wait (editable, default 1 s).
4. Set Ar flow (editable, default 4 sccm).
5. Start the precursor fill pulse (same target pressure / pulse params as
   the run panel).
6. Strike the plasma and hold: pulse plasma-ground to restrike on any
   dropout — **retries indefinitely, no timeout, no attempt limit**, by
   explicit instruction. The only way out is the **Stop pre-start** button.
7. Once current holds continuously for the configured duration (default
   5 s — a drop mid-hold restarts the count, it is not cumulative), stop
   watching and set plasma-ground **OPEN**, i.e. beam **OFF**.

However the sequence ends — success, an operator stop, or a crash — the beam
is grounded on the way out. Ar and the fill pulse are left running (that's
the point: the tool is primed for **Start run** next). A run and pre-start
both drive `plasma_ground`, so the server refuses to start one while the
other is active (409); the UI greys out the buttons accordingly.

## Where it lives

- **`reactor/control/recipe.py`** — the recipe engine.
  - `Step` (pydantic): ops `dose`, `wait` (with `lit_gated`), `valve`,
    `set_flow`, `wait_for_pressure`, `message`, **`electron_beam`**,
    **`beam_start`**, **`beam_stop`**, **`start_fill`**, **`stop_fill`**.
  - `Recipe.mode` is `"ald"` or `"cvd"`; `Recipe.gas_overlap_s` is the single
    shared handoff overlap described above.
  - `RecipeRunner` — runs setup → N×steps → teardown on its own asyncio
    task, with absolute-deadline sleeps (UI load can't stretch a dose).
    Pausable, abortable.
  - `_electron_beam()` — EE-ALD's beam step: sets plasma-ground off,
    accumulates exposure only while current is present, reignites on
    dropout, restores plasma-ground on at the end. Publishes live
    `progress.beam = {remaining, current, lit}`.
  - `_beam_watch()` — EE-CVD's continuous-beam watchdog, started by
    `beam_start` and stopped by `beam_stop` or the run's own cleanup. Tracks
    two clocks: `_lit_s` (total lit time, what a `lit_gated` wait counts
    down against) and `_cycle_clock` (advances whenever the running step's
    own clock does — always during the ungated dose, only while lit during
    gated pump A — which is what locks the gas windows to the cycle).
  - **`build_ald_recipe(params)`** / **`build_cvd_recipe(params)`** — build
    the two recipes from a dict of UI parameters (no YAML file). This is
    what the UI drives.
- **`reactor/supervisor.py`**
  - `start_fill_regulation()` / `_run_regulation()` / `stop_fill_regulation()`
    — background task pulsing the fill valve (via `_drive_valve_quiet`,
    which skips the event log so fast pulsing doesn't flood it). Publishes
    `self.regulator` status. Emits a `"flag"` event crossing out of
    ±tolerance, `"fill"` back in.
  - `start_ald_run(params)` / `start_cvd_run(params)` — both funnel through
    `_start_built_run`, which records the run's dose/plasma/fill valves and
    refuses to start while pre-start owns the plasma relay.
  - `start_prestart(params)` / `stop_prestart()` / `_run_prestart()` — the
    pre-start sequence above. Publishes `self.prestart` status.
  - `self.marks` — every `set_valve` flip is recorded `{t, id, state,
    reason}` for the current-trace plasma overlay; recent ones are exposed
    in `state()`.
  - Each trend sample carries `dosing` and `beam_on` derived from valve
    state.
- **`reactor/server/app.py`** — `POST /api/run/ald`, `POST /api/run/cvd`
  (body = params dict), `POST /api/prestart/start`, `POST
  /api/prestart/stop`. Plus the existing `/api/recipe/*`, `/api/mfc/*`,
  `/api/valve/*`, `/api/label` routes.
- **`reactor/server/static/index.html`** — the GUI (below).

## Run parameters (all editable in the UI, persisted to localStorage)

| UI field | param key | default | notes |
|---|---|---|---|
| Cycles | `cycles` | 100 | |
| Dose pressure (Torr) | `dose_pressure_torr` | 0.020 | full-volume setpoint (ai1 Baratron) |
| Dose time (s) | `dose_s` | 0.05 | micro-pulse valve open time |
| Pump A (s) | `pump_a_s` | 10 | EE-ALD: wall clock. EE-CVD: lit-time gated |
| Beam exposure (s) | `beam_s` | 5 | EE-ALD only; counted only while current present |
| Pump B (s) | `pump_b_s` | 10 | EE-ALD only |
| Min current (µA) | `min_current_ua` | 500 | UI takes µA, sends amps |
| Overlap (s) | `gas_overlap_s` | 0.5 | single handoff time shared by both gas transitions |
| Fill pulse on/off (s) | `fill_pulse_on_s` / `fill_pulse_off_s` | 0.10 / 0.30 | fill valve pulse timing |
| Flag tolerance (%) | `tolerance_frac` | 20% | UI takes %, sends fraction |
| Reignite pulse/settle (s) | `reignite_pulse_s` / `reignite_settle_s` | 0.10 / 0.15 | ≈2.2 attempts/s incl. the 0.2 s poll |
| Pre-start Ar flow (sccm) | `ar_sccm` | 4 | pre-start only |
| Pre-start valve settle (s) | `valve_delay_s` | 1 | pre-start only |
| Pre-start hold (s) | `hold_s` | 5 | pre-start only; a drop restarts the count |

Fixed defaults inside the builders (change there or add UI fields later):
`fill_valve=rpm_top`, `dose_valve=prec1`, `plasma_switch=plasma_ground`,
`gauge=gauge.prec1_dose`, `ammeter=inst.ammeter`.

## The GUI

Three tabs (`localStorage`-persisted): **Run**, **Hardware**, **Diagnostics**.

### Run tab

- **Hero readouts** across the top: chamber pressure, stage temperature,
  precursor fill pressure (turns amber + "OUT OF BOUNDS" when the flag
  trips), bubbler temperature, sample current (auto-ranges µA/mA; turns
  orange with a lit/no-plasma indicator whenever a beam or pre-start is
  active).
- **Run panel** (left column): mode selector (EE-ALD/EE-CVD), the shared
  parameter fields, a collapsible **Gas scheduling** panel (open by
  default) with a live plain-English timeline, a collapsible **Advanced
  timing** panel (the reignite/fill fields, with the achieved reignite rate
  in its summary), a collapsible **Pre-start** panel (its own params +
  live status in its summary), Start/Pause/Resume/Abort, Pre-start/Stop
  pre-start, and Initiate/Stop fill pulse. A **phase strip** highlights the
  active phase with a live countdown, sized to whichever phases the current
  mode has (4 for EE-ALD, 2 for EE-CVD).
- **Run monitor** (right column, top): two stacked canvas panels — chamber
  pressure (log, optionally smoothed via a collapsible panel with a
  centered moving average; the axis stays on the raw scale so smoothing
  can't make noise look like a bigger trend than it is) and sample current,
  with plasma-relay flips overlaid (dashed grey = scheduled, solid red =
  reignite).
- **MFC flow** (right column, bottom): actual flow per gas, not setpoint.
- **Temperatures** (below the fold, scrolls): stage and precursor bubbler,
  separate panels since they sit at different temperatures.

All three plots have **independent** time windows (2 min – 1 hr), their own
Follow-live button, hover crosshair with a readout box, and drag-to-zoom
(box-select a time range; arrow keys pan/zoom while hovering that chart). No
wheel-zoom — it would hijack page scroll.

**Auto-download**: `recordRun()` accumulates a per-run buffer from the
moment Start is pressed (keyed on `recipe.started_at`); on completion
`downloadRun()` writes a CSV — columns `elapsed_s, stage_temp_c,
sample_current_a, precursor_dosing, precursor_pressure_torr,
chamber_pressure_torr`, time zeroed to the Start press. This is client-side
(the browser must stay open during the run); a server-side copy is tracked
as `reactor-f3j`.

### Hardware tab

Valves (grouped by control box, each individually actuable, with rename)
and the MFC tiles (live flow + settable flow + rename) up top; other
pressure gauges, other inputs, instruments, and primary-sensor detail
below. Any valve, MFC, or Baratron can be renamed from its ✎ button —
persisted to `config/labels.json`, blank reverts to the `reactor.yaml`
default.

### Diagnostics tab

Valve-identification sweep, data logging controls, the connections table,
and the event log. A pinned header chip surfaces the newest
error/trip/safety/flag event for two minutes regardless of which tab is
open, so a fill-pressure flag during a run on the Run tab isn't missed just
because the log itself lives elsewhere.

## To actually run it in the lab

1. Close LabVIEW. Start `python -m reactor`, open in a real browser.
2. Confirm live readings look right (pressure, Baratrons, current, MFCs).
3. Set the run parameters. Confirm the fill valve (`rpm_top`), the
   precursor-Baratron labelling, and the current threshold (500 µA) are
   right for the process — both are still unverified assumptions
   (`reactor-zo0`, `reactor-gkw`).
4. Optionally press **Pre-start** first to strike the plasma and prime Ar +
   fill pressure ahead of time; confirm the dialog once the three valves are
   in REMOTE and supplies are on.
5. Press **Start run**. Watch the phase strip + Run monitor. The gentle flag
   will warn on pressure drift; the beam will reignite on its own if the
   plasma drops.
6. On completion the CSV downloads automatically.

## Likely next iterations

- Verify/tune everything on real hardware (dose pressure, durations,
  threshold, precursor Baratron labelling, fill valve) — `reactor-alz`,
  `reactor-2z1`, `reactor-gkw`, `reactor-zo0`.
- Server-side run export so a closed browser doesn't lose the CSV —
  `reactor-f3j`.
- Identify the NI 9265 current outputs — `reactor-5u2`.
