# The ALD + e-beam run program

The electron-beam ALD run, its recipe engine, and the GUI around it. Built
2026-08-01 (first iteration; expect many more). Verified with a fake-DAQ harness;
**not yet run on real hardware.**

## The cycle, as the operator described it

Repeat N cycles, all durations set in the UI:

1. **Dose precursor 1** — open the micro-pulse valve (`prec1`, cDAQ1Mod3 line0)
   for a set time, dosing into the chamber from the charged full volume.
2. **Pump A** — wait.
3. **Electron beam** — plasma-ground relay **OFF** (= beam ON), hold a set
   *exposure* time while watching sample current.
4. **Pump B** — wait.

Two things run continuously alongside the cycle:

- **Full-volume pressure regulation** (the *whole* run, not per-cycle): a
  background loop pulses the fill valve (`rpm_top`, right manifold top — switch to
  `rpm_bottom` if reversed) whenever the precursor-1 dose Baratron (`gauge.prec1_dose`,
  ai1) reads below the setpoint, holding the full volume charged. Flags gently if
  it drifts >20% off.
- **Current monitoring during the beam phase**: exposure only accumulates while
  `|Keithley current|` ≥ `min_current` (default 500 µA — plasma current is tens of
  mA negative). If the plasma extinguishes, exposure pauses, the switch is pulsed
  ON→OFF to reignite, and exposure resumes only once current returns.

## Where it lives

- **`reactor/control/recipe.py`** — the recipe engine.
  - `Step` (pydantic): ops `dose`, `wait`, `valve`, `set_flow`,
    `wait_for_pressure`, `message`, **`electron_beam`**, **`start_fill`**,
    **`stop_fill`**.
  - `RecipeRunner` — runs setup → N×steps → teardown on its own asyncio task,
    with absolute-deadline sleeps (UI load can't stretch a dose). Pausable,
    abortable.
  - `_electron_beam()` — the beam step: sets plasma-ground off, accumulates
    exposure only while current is present, reignites on dropout, restores
    plasma-ground on at the end. Publishes live `progress.beam = {remaining,
    current, lit}`.
  - **`build_ald_recipe(params)`** — builds the ALD Recipe from a dict of UI
    parameters (no YAML file). This is what the UI drives.
- **`reactor/supervisor.py`** — the fill regulator.
  - `start_fill_regulation()` / `_run_regulation()` / `stop_fill_regulation()` —
    background task pulsing the fill valve (via `_drive_valve_quiet`, which skips
    the event log so fast pulsing doesn't flood it). Publishes `self.regulator`
    status. Emits a `"flag"` event crossing out of ±tolerance, `"fill"` back in.
  - `start_ald_run(params)` — records the run's dose/plasma valves and launches
    the recipe.
  - `self.marks` — every `set_valve` flip is recorded `{t, id, state, reason}`
    for the current-trace plasma overlay; recent ones are exposed in `state()`.
  - Each trend sample carries `dosing` and `beam_on` derived from valve state.
- **`reactor/server/app.py`** — `POST /api/run/ald` (body = params dict) →
  `sup.start_ald_run`. Plus the existing `/api/recipe/*`, `/api/mfc/*`,
  `/api/valve/*` routes.
- **`reactor/server/static/index.html`** — the GUI (below).

## Run parameters (all editable in the UI, persisted to localStorage)

| UI field | param key | default | notes |
|---|---|---|---|
| Cycles | `cycles` | 100 | |
| Dose pressure (Torr) | `dose_pressure_torr` | 0.020 | full-volume setpoint (ai1 Baratron) |
| Dose time (s) | `dose_s` | 0.05 | micro-pulse valve open time |
| Pump A (s) | `pump_a_s` | 10 | |
| Beam exposure (s) | `beam_s` | 5 | counted only while current present |
| Pump B (s) | `pump_b_s` | 10 | |
| Min current (µA) | `min_current_a` | 500 µA | UI takes µA, sends amps |
| Fill pulse on/off (s) | `fill_pulse_on_s` / `fill_pulse_off_s` | 0.10 / 0.30 | fill valve pulse timing |
| Flag tolerance (%) | `tolerance_frac` | 20% | UI takes %, sends fraction |

Fixed defaults inside `build_ald_recipe` (change there or add UI fields later):
`fill_valve=rpm_top`, `dose_valve=prec1`, `plasma_switch=plasma_ground`,
`gauge=gauge.prec1_dose`, `ammeter=inst.ammeter`, reignite pulse/settle = 0.5/1.0 s.

## The GUI pieces

- **ALD + e-beam run** card: the parameter form, Start/Pause/Resume/Abort, a
  small dropdown to run the file-based recipes too, the **phase strip** (4 chips,
  active one highlighted with a live countdown; beam shows exposure remaining),
  and live readouts (cycle, step, beam exposure, sample current with lit/no-plasma,
  fill pressure vs setpoint with OUT OF BOUNDS).
- **Run monitor** card: two stacked canvas panels sharing a time axis — pressure
  (log: chamber + precursor dose) on top, sample current on the bottom. Plasma
  relay flips overlay the current panel (dashed grey = scheduled, solid red =
  reignite). Drag to pan, wheel to zoom, "Follow live" re-attaches, window presets
  2–30 min. All client-side from the `trend` buffer + `marks`.
- **Auto-download**: `recordRun()` accumulates a per-run buffer from the moment
  Start is pressed (keyed on `recipe.started_at`); on completion `downloadRun()`
  writes a CSV — columns `elapsed_s, stage_temp_c, sample_current_a,
  precursor_dosing, precursor_pressure_torr, chamber_pressure_torr`, time zeroed
  to the Start press. This is client-side (the browser must stay open during the
  run).

## To actually run it in the lab

1. Close LabVIEW. Start `python -m reactor`, open in a real browser.
2. Confirm live readings look right (pressure, Baratrons, current, MFCs).
3. Set the ALD parameters. Confirm the fill valve (`rpm_top`) and the current
   threshold (500 µA) are right for the process.
4. Press **Start run**. Watch the phase strip + Run monitor. The gentle flag will
   warn on pressure drift; the beam step will reignite if the plasma drops.
5. On completion the CSV downloads automatically.

## Likely next iterations

- Verify/tune everything on real hardware (dose pressure, durations, threshold).
- Expose the fixed valve/gauge IDs and reignite timing as UI fields if needed.
- Server-side run export (current export is client-side; a server copy would
  survive a closed browser).
- Per-cycle marks / cycle boundaries on the plots; temperature panel.
