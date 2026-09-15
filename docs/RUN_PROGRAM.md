# The run program: EE-ALD, EE-CVD, and pre-start

The electron-beam run modes, the recipe engine underneath them, and the GUI
around them. First built 2026-08-01 as a single ALD-only workflow; the run
panel now supports two modes and a pre-start sequence, added 2026-08-05.
Control logic is verified on every change by `python -m tests.run_all`, which
runs the real `Supervisor` and recipe engine against the fake devices in
`reactor/testing/virtual_reactor.py` (see [tests/README.md](../tests/README.md)
for what that proves and what it flatly cannot), and **EE-ALD has now been run
and tuned on real hardware** (`reactor-alz`, `reactor-2z1`, confirmed
2026-08-06).

## The event log and the error log

The server keeps **200,000 events** in memory and the browser scrolls what it
has (`GET /api/events` seeds the newest 20,000 by default - shipping all
200,000 at once would be tens of MB over Tailscale; pass `limit` for more). That is not gratuitous: a run emits roughly ten events a cycle, so a
150-cycle run is ~1,500 on its own, and the old 250-event buffer pushed a whole
pre-start out of view before anyone could read it — which is what stalled the
stage-bias diagnosis on 2026-08-25. It was raised from 20,000 on 2026-09-09,
when the ask was for both logs to be "much much longer than I could ever need".

**The errors are their own log** (2026-09-09), on their own Diagnostics panel
above the event log, so a fault does not have to be found by scrolling. It is
the same entries — `Supervisor._event` appends to both — filtered to
`Supervisor.ERROR_KINDS`: `error` and `flag`. A flag is in deliberately; a fill
pressure that drifted off setpoint is exactly what gets hunted for afterwards.
`GET /api/errors` seeds it, the frame's `errors` tail feeds it, and the panel
shows a count.

How it gets there matters, because the naive version is expensive. The live
telemetry frame carries only the **last 200** events; shipping the whole buffer
at 5 Hz would be roughly a megabyte a second of pure repetition, which is the
last thing you want over Tailscale. The browser instead seeds its log once from
`GET /api/events` and appends whatever is new from each frame, de-duplicated on
timestamp + message.

The in-memory buffer starts empty after a restart. **The permanent record is
`server.log`** — `Supervisor._event()` writes every event to the Python logger
as well, and that file rotates rather than being trimmed.

## Where the run parameters live

**The server owns them.** They are stored in `config/run_params.json` and served
by `GET`/`POST /api/run_params`; each browser keeps a `localStorage` copy purely
as a cache, so the fields populate instantly on load and still work if the
server is unreachable.

This changed on 2026-08-25. They used to be localStorage-only, which meant every
machine had its own set: opening the UI over Tailscale from a laptop showed
default cycles/dose/bias rather than what the reactor PC was actually
configured with. One reactor should present one set of parameters — the same
reasoning that already made the run *name* server-owned.

Saves are debounced ~800 ms (the fields save on every keystroke) and are
fire-and-forget: a failed save leaves the local cache correct. Last write wins
if two browsers edit at once, which is fine for a single-operator tool.

The **run name** is separate and already server-owned (`config/last_run.json`),
so the suggested next name follows real runs rather than whichever browser you
happen to be sitting at.

## Sample bias

**Sample bias (V)** sits in the main parameter grid for both modes, where *Min
current (µA)* used to be — min current moved into **Advanced timing**, since it
is a reignite-detection threshold rather than something set per run.

It drives the `stage_bias` Keithley 2260B (2260B-250-4, 250 V / 4.5 A):

- **Enter a magnitude.** Zero means the stage bias output stays **off** for that
  run, and an event says so rather than leaving you to infer it from a dark
  supply.
- Non-zero **arms** the supply at that voltage during pre-start — output still
  off — and each beam then switches it: **on `Bias lead` seconds before the beam
  and off `Bias trail` seconds after** (both in Advanced timing, 0.2 s by
  default). Changed 2026-08-26, because a bias held on all run made the stage
  thermocouple unreadable; now the TC reads clean whenever the beam is off.
  In EE-CVD the beam is one long step, so the bias leads the strike at the
  start of the run and drops after the beam at the end of it.
- **A reignite does not cycle it**, and the bracket costs no run time: both
  flips are scheduled inside pump A and pump B, so a cycle still takes the sum
  of its step durations.
- The **`+`/`−` toggle records which way the leads were run onto the stage.**
  The supply is single-quadrant and cannot source a negative voltage, so the
  sign never reaches the instrument — it is applied to the **logged** voltage, so
  `psu_stage_bias_voltage` reads negative when the leads are reversed.
- The **current limit is not touched by pre-start**: it stays wherever the front
  panel or the Hardware tab's current field last put it.

The pre-start confirmation dialog states the bias explicitly — magnitude, sign,
and that pre-start only arms it — because it is the one output in the set that
puts a potential on the sample.

The other three supplies (steering, grid, collimating) have no run parameter:
their outputs simply come on at pre-start and go off at run end. See
[KEITHLEY_2260B.md](KEITHLEY_2260B.md).

## Two run modes, one panel

The **Run** tab's run panel has a mode selector: **EE-ALD** or **EE-CVD**.
Both share cycles, dose pressure, dose time, pump A, **sample bias**, the
fill-pulse/tolerance/reignite/min-current advanced-timing fields, and gas
scheduling. They differ in what happens to the electron beam.

### EE-ALD — pulsed beam, one exposure per cycle

Repeat N cycles, all durations set in the UI:

1. **Dose precursor 1** — open the micro-pulse valve (`prec1`, cDAQ1Mod3
   line0) for a set time, dosing into the chamber from the charged full
   volume.
2. **Pump A** — wait, monotonic elapsed time.
3. **Electron beam** — plasma-ground relay **OFF** (= beam ON), hold a set
   *exposure* time while watching sample current.
4. **Pump B** — wait, monotonic elapsed time.

### EE-CVD — continuous beam, dosing on top of it

No beam-exposure or pump-B field — the beam turns on once at run start and
stays on until the run ends (or is aborted). A cycle is just:

1. **Dose precursor 1** — same micro-pulse valve, same monotonic timing,
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
  confirmed) whenever the precursor-1 dose Baratron (`gauge.prec1_dose`, ai1
  — confirmed) reads below the setpoint, holding the full volume charged.
  Flags gently if it drifts more than the tolerance off setpoint; never
  stops anything.
- **Current monitoring during any beam-on period**: exposure/lit-time only
  accumulates while `|Keithley current|` ≥ `min_current`. If the plasma
  extinguishes, the switch is pulsed ON→OFF to reignite (default
  0.10 s pulse / 0.15 s settle, ≈2.2 attempts/s including the 0.2 s
  current-check poll — tune from the UI's Advanced timing panel, which shows
  the achieved rate live).

## Timing: how a run stays on schedule

The requirement is plain: a cycle should take as long as the numbers typed into
the UI say it takes — every cycle, for hundreds of cycles, whether or not anyone
is looking at the browser. This section is how that is arranged, and where it
stops being true.

### One process, one owner, four clocks

Everything runs in a single Python process on one asyncio event loop.
`Supervisor` is the only thing that touches hardware, so there is no lock
contention and no second writer to race. On that loop there are four long-lived
tasks:

| Task | Rate | What it does |
| --- | --- | --- |
| `_control_loop` | `site.loop_hz` 2 Hz | DAQ analog inputs (thermocouple-limited) + the four Keithleys + the Glassman |
| `_current_loop` | `site.current_hz` 5 Hz | DMM6500 sample current, telemetry publish, run-CSV row |
| `_mfc_loop` | `site.mfc_hz` 6 Hz | the three MKS G50s — a slow read, kept off the telemetry tick on purpose |
| `recipe` | event-driven | the run itself |

The three polling loops keep time with an **absolute deadline that accumulates**
(`next_at += period`, then sleep whatever is left of it). A loop that runs late
does not push the next tick out; it just gets a shorter sleep. Only if it falls
more than a whole period behind does it resynchronise — the one case where
catching up would mean firing several ticks back-to-back for no benefit.

**The recipe never waits on any of them.** It reads `sup.snapshot`, a plain dict
the loops update in place. So a slow MFC read, a stalled DMM, or five browsers
on Tailscale cannot stretch a dose; the worst they can do is let the beam step's
current check act on a value a fraction of a second old.

### Why a timed step takes as long as it says

Each timed step performs **one timed wait**, not a poll loop:

```python
await asyncio.wait_for(self._abort.wait(), timeout=seconds)   # RecipeRunner._sleep
```

That is the whole mechanism, and what matters is what it avoids. A loop of the
form "sleep 50 ms, check, repeat" re-adds the scheduler's lateness on *every*
iteration, so a 10 s step built from 200 naps inherits 200 doses of jitter. One
wait inherits one. It also gets abort for free — the wait ends early when
`_abort` is set, so Abort never has to sit out the rest of a step.

The old LabVIEW program timed steps with a 1 s Express-VI delay inside the same
loop that redrew the front panel, which is exactly the failure mode this avoids:
a busy UI lengthened the chemistry.

### The beam step is the exception, and how it stays honest

The beam step cannot be one long sleep — it has to check sample current at least
twice a second to notice a dropped plasma. It polls on `BEAM_TICK_S` (0.2 s),
and does two things so that poll does not inflate the step:

- **It subtracts measured time, not nominal time.** `dt = time.time() - t0` is
  the tick that actually happened; `remaining -= dt`. A tick that took 0.23 s
  spends 0.23 s of the exposure budget, so lateness is absorbed rather than
  accumulated.
- **The last tick is clamped**: `asyncio.sleep(min(BEAM_TICK_S, remaining))`.
  Without that the final tick overshoots by up to a full tick — 0.1 s on
  average, every cycle.

The strike settle is the third piece. Flipping plasma-ground off *is* the strike,
and current takes a moment to appear, so the step must not judge the plasma dead
on its first tick. That settle used to be a blind `sleep()` **before** the
exposure loop, which made every beam step `reignite_settle_s` longer than asked
for. It is now a grace **window inside** the loop (the `t0 - strike_at` branch):
the same protection from a premature reignite, but current that appears during it
counts as the exposure it is.

Those three faults together are why Mo-015 (150 cycles, 2026-08-21) finished
124 s past its nominal 16.00 s/cycle. `tests/test_run_timing.py` now pins the
behaviour to **0.1 s per cycle**, and checks the whole cycling phase, not one
cycle in isolation.

### Work that must not lengthen a cycle is scheduled beside it

Two things have to happen *before* a step starts or *after* it ends: the "first"
gas leading the beam in, and the sample bias bracketing it. Neither is awaited in
line. Both are separate asyncio tasks with their own delay, launched so they
count down **inside** the neighbouring steps — the lead inside dose/pump A, the
trail inside pump B. A cycle therefore still takes the sum of its step durations,
with the bracket hidden in the slack.

The bias schedule resolves conflicts by deadline: scheduling a flip cancels every
pending flip due at or after it and leaves earlier ones alone. That is what makes
the awkward settings behave — with a trail longer than pump B + dose + pump A,
the next cycle's ON falls before the pending OFF, supersedes it, and the bias
simply stays up across the boundary instead of blinking off just as the beam
returns.

### Things that always happen, however a run ends

Timing is only half of "it runs when it should"; the other half is that cleanup
is not conditional on the happy path. Each of these is a `finally`, so it runs on
completion, on abort, and on an unhandled exception alike:

- a **dose** closes its own valve — open/hold/close is one operation, and an
  aborted dose still ends with the valve where it started;
- an **`electron_beam` step** re-grounds the beam and zeroes any gas it turned on;
- **`_run`'s** own teardown cancels pending gas lead-ins and bias flips, kills the
  EE-CVD watchdog, grounds the beam if a `beam_stop` has not already, drops the
  sample bias, and calls `Supervisor.finish_run`;
- **`finish_run`** stops fill regulation, closes the run export, commands **HV
  off** and the DC supply outputs off, and for a UI-built run zeroes every MFC
  and closes the fill valve.

The invariant worth stating out loud: **no exit from a run leaves a dose valve
open, the beam energised, the stage biased, or the fill valve pulsing.**

### What is deliberately allowed to stop the clock

A run is deterministic in **exposure**, not in wall time — and that is the right
way round. The recipe promises the film a set number of beam-seconds per cycle,
not a set finish time. Three clocks freeze, and they freeze on one shared
mechanism (`RecipeRunner._cycle_pause`, reference-counted so overlapping reasons
nest without double-counting):

1. the beam **exposure** budget (EE-ALD), or the **lit-time** clock (EE-CVD);
2. the **cycle-progress** clock that produces the fractional cycle number in the
   logs;
3. the operator's **est. remaining** countdown — which, between runs, shows
   the length of the run the parameters currently describe instead
   ("est. duration" / "finish if started now", 2026-08-28). That number comes
   from `POST /api/run/estimate`, which builds the same recipe a run would and
   returns `cycle_seconds() x cycles`, so the idle estimate and the countdown's
   starting value are the same arithmetic and cannot drift apart. It refreshes
   as the cycle parameters or the mode change.

They freeze for a reignite and for an operator pause. Because it is one mechanism
and not three, they cannot disagree with each other. A run that reignites often
finishes late in wall time by exactly the time it spent dark — and the log says
where, since `recipe_step` names the freeze (`reignite`, `pause`) rather than
carrying a separate `paused` column.

One asymmetry is intentional: **an operator pause cannot freeze a dose.** The
dose's wait ignores the pause flag and the pause lands at the next step boundary
instead, because freezing mid-dose would hold the precursor valve open. Same
reasoning as EE-CVD's never-gated dose.

### The countdown is arithmetic, not extrapolation

`run_remaining_s()` starts at (cycle length × cycles) and falls one second per
second. It is computed from `cycle_fraction()` — "how far through the run are we,
in cycles, with freezes subtracted" — so it inherits the freeze behaviour above
for free. There is no second clock to keep in step.

It used to be extrapolated in the browser from the run's own measured pace, which
meant the number moved for reasons the operator could not see and never agreed
with the arithmetic they had already done from the parameters they typed in.

### Honest limits

- **This is software timing on Windows: 1–15 ms of jitter.** Fine for doses of
  tens of milliseconds and up, and far better than the program it replaced, but
  not deterministic in the real-time sense. Tighter than ~10 ms wants a
  hardware-timed DAQmx digital output task, which this structure can accommodate
  without changing the recipe format.
- **The nominal cycle length is the sum of the step durations and nothing else.**
  The DAQ writes between steps (a valve flip is a real DAQmx call) are not in that
  sum, so a real cycle is a few milliseconds longer. `cycle_fraction` caps
  progress at the nominal length so the cycle number never steps backwards at a
  boundary.
- **Gas windows inside an EE-CVD cycle resolve only to the 0.2 s watchdog tick**,
  so a window shorter than that can be missed. The cycle *boundaries* are exact,
  because `_run` applies them directly rather than leaving them to a tick.
- **The current check is only as fresh as the 5 Hz DMM loop.** The beam ticks at
  0.2 s and current updates at 0.2 s, so a reading can be up to one poll old.

## Gas scheduling — a single overlap, not per-gas leads

The two process-gas lines (MFC 1 / MFC 2 - named on screen by the gas they
report) can each be scheduled on/off around the run's beam-on period instead of
flowing the whole cycle. At most one gas is `first`, at most one is
`second`. There is **one** `Overlap (s)` field, not a lead time per gas: the
incoming gas starts that many seconds before the outgoing one stops, applied
at every handoff — first→second, and second→first (which for EE-CVD wraps
into the next cycle as the "first" gas re-arming near the end of this one).

### Simultaneous (2026-08-26)

`Simultaneous` is the third **Order**, and it means the window is not divided
at all: every gas set to it covers the **whole** window, at its own flow. The
`%` column is greyed out because there is nothing to divide.

It takes **two**. One gas set to Simultaneous while the other is First,
Second, or switched off is **refused when you press Start** — an alert in the
browser before the confirm dialog, and a 409 from the server if the request
gets there anyway (`_build_gas_schedules` raises). A lone Simultaneous gas is
functionally a 100% `first`, and is far more likely to be a half-finished edit
than an intent, so the run does not start on a guess.

The overlap still applies as a lead-in for EE-ALD — both gases come up that
long before the beam strikes, then go off when the exposure ends. For EE-CVD
the beam is on for the whole run, so the gases come on at the first cycle and
stay on to the end; the overlap does nothing there.

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

Gas names in the scheduler — the table rows, the prose under it, and the
Start-time errors — are the MFCs' own display labels, so renaming an MFC on the
Hardware tab (its ✎ button) renames it here in the same frame. The label's
`"<name> - <purpose>"` tail is trimmed for the table. The head is the gas the
unit reports (`NH3`), falling back to the channel (`MFC 1`) if it reports none -
nothing static names a gas, because the gas on a line changes.

The UI computes the exact same numbers as `RecipeRunner` and shows them live
next to the Gas scheduling panel's title (e.g. `NH3 on beam−0.50s · off
beam+2.00s`), including the same order-collision check the server enforces
with a 409, and the lone-Simultaneous refusal above.

## Pre-start — bring the tool up to a struck, beam-off state

A separate button and sequence (`Supervisor.start_prestart`), not part of
either run mode, for getting the tool ready before pressing Start:

1. Confirmation dialog: *"Set Ar Pneumatic, Plasma Ground, and Precursor Fill
   to Remote. Turn on HV at the Glassman front panel if you want plasma."* It
   also states the sample bias explicitly — magnitude, sign, and that pre-start
   only arms it. (The DC supply outputs are no longer a manual step: pre-start
   switches the three coils on itself, see below.)
2. Open the Ar pneumatic isolation valve — **soft-opened**: one 0.05 s bleed
   pulse, a 0.5 s wait, then open, so the Ar built up behind it does not dump
   into the reactor (2026-08-26; the numbers are in Advanced timing). This adds
   ~0.55 s at the default settings, before step 3's settle. Every other open of
   that valve is pulsed the same way, including a manual one from the Hardware
   tab.
3. Wait (editable, default 1 s).
4. Set Ar flow (editable, default 4 sccm).
5. Start the precursor fill pulse (same target pressure / pulse params as
   the run panel).
6. Strike the plasma and hold: pulse plasma-ground to restrike on any
   dropout — **retries indefinitely, no timeout, no attempt limit**, by
   explicit instruction. The only way out is the **Abort pre-start** button
   (there was a Stop button beside it until 2026-08-28; it ended the sequence
   and left the tool primed, so Abort had to be pressed after it anyway).
7. Once current holds continuously for the configured duration (default
   5 s — a drop mid-hold restarts the count, it is not cumulative), stop
   watching and set plasma-ground **OPEN**, i.e. beam **OFF**.

On success or an internal stop, the beam is grounded and Ar and the fill pulse
are left running: the tool is primed for **Start run** next. Operator Abort
performs the full cleanup described in CONTROL_MODEL.md, including Ar and fill.
A malformed opening field fails before commands; later failures ground the
beam in the controller's cleanup. A run and pre-start
both drive `plasma_ground`, so the server refuses to start one while the
other is active (409); the UI greys out the buttons accordingly.

## Where it lives

- **`reactor/control/recipe_model.py`** — pure schema and ALD/CVD builders,
  re-exported by `recipe.py` for compatibility.
  - `Step` (pydantic): ops `dose`, `wait` (with `lit_gated`), `valve`,
    `set_flow`, `wait_for_pressure`, `message`, **`electron_beam`**,
    **`beam_start`**, **`beam_stop`**, **`start_fill`**, **`stop_fill`**.
  - `Recipe.mode` is `"ald"` or `"cvd"`; `Recipe.gas_overlap_s` is the single
    shared handoff overlap described above.
- **`reactor/control/recipe.py`** — `RecipeRunner` runs setup → N×steps → teardown
    on its own asyncio task. Timed waits avoid accumulating polling drift;
    scheduler and device latency can still stretch a dose. See the timing section.
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
- **`reactor/control/parameters.py`** — typed parameter views, legacy gas-key
  migration and stage-specific pre-start conversions.
- **`reactor/control/run_coordinator.py`** — run admission, immutable cleanup
  identities, live edits and their history, recording preparation and drain.
- **`reactor/control/fill.py`** — fill-pressure regulation and live tuning.
- **`reactor/control/prestart.py`** — pre-start task, progress and cleanup.
- **`reactor/supervisor.py`**
  - `start_fill_regulation()` / `stop_fill_regulation()` delegate to the fill
    controller, pulsing the fill valve (via `drive_fill_valve`,
    which skips the event log so fast pulsing doesn't flood it). Publishes
    `self.regulator` status. Emits a `"flag"` event crossing out of
    ±tolerance, `"fill"` back in.
  - `start_ald_run(params)` / `start_cvd_run(params)` — both funnel through
    `_start_built_run`, which records the run's dose/plasma/fill valves and
    refuses to start while pre-start owns the plasma relay.
  - `start_prestart(params)` / `stop_prestart()` / `abort_prestart()` delegate
    to the pre-start controller. Publishes `self.prestart` status.
  - `self.marks` — every `set_valve` flip is recorded `{t, id, state,
    reason}` for the current-trace plasma overlay; recent ones are exposed
    in `state()`.
  - Each trend sample carries `dosing` and `beam_on` derived from valve
    state.
- **`reactor/server/app.py`** — `POST /api/run/ald`, `POST /api/run/cvd`
  (body = params dict), `POST /api/run/params` (same body, applied to the run
  ALREADY in progress — see "Parameters are editable mid-run" in
  CONTROL_MODEL.md; 409 if no run is running), `POST /api/run/estimate` (same
  body, starts nothing —
  returns the length of the run those parameters describe, for the Run tab's
  idle estimate), `POST /api/prestart/start`, `POST /api/prestart/abort`. Plus
  the existing `/api/recipe/*`, `/api/mfc/*`, `/api/valve/*`, `/api/label`
  routes.
- **`reactor/server/static/index.html`** — the GUI (below).

## Run parameters (all editable in the UI, persisted to localStorage)

| UI field | param key | default | notes |
|---|---|---|---|
| Cycles | `cycles` | 100 | |
| Dose pressure (Torr) | `dose_pressure_torr` | 0.020 | full-volume setpoint (ai1 Baratron) |
| Dose time (s) | `dose_s` | 0.05 | micro-pulse valve open time |
| Pump A after dose (s) | `pump_a_s` | 10 | EE-ALD: monotonic elapsed time. EE-CVD: lit-time gated |
| Beam exposure (s) | `beam_s` | 5 | EE-ALD only; counted only while current present |
| Pump B after beam (s) | `pump_b_s` | 10 | EE-ALD only |
| Sample bias (V) | `sample_bias_v` | 0 | magnitude; 0 leaves the stage bias supply off |
| Bias polarity | `sample_bias_polarity` | +1 | lead orientation; signs the LOGGED voltage only |
| Bias lead before beam (s) | `sample_bias_lead_s` | 0.2 | bias on this long before the beam. **Advanced timing** |
| Bias trail after beam (s) | `sample_bias_trail_s` | 0.2 | bias off this long after the beam. **Advanced timing** |
| Ar soft-open pulses | `ar_soft_open_pulses` | 1 | 0 = plain flip. **Advanced timing**; server-side, applies to every open |
| Ar pulse width (s) | `ar_soft_open_on_s` | 0.05 | width of each soft-open pulse |
| Ar pulse gap (s) | `ar_soft_open_gap_s` | 0.5 | delay between them |
| Min beam current (µA) | `min_current_ua` | 500 | UI takes µA, sends amps. **Advanced timing** since 2026-08-25 |
| Gas overlap (s) | `gas_overlap_s` | 0.5 | single handoff time shared by both gas transitions; with Simultaneous it is only the lead-in |
| Gas order | `{mfc1,mfc2}_gas_order` | first / second | or `simultaneous` — both gases cover the whole window, % ignored. Needs two |
| Fill pulse on/off (s) | `fill_pulse_on_s` / `fill_pulse_off_s` | 0.10 / 0.30 | fill valve pulse timing |
| Fill flag tolerance (%) | `tolerance_frac` | 20% | UI takes %, sends fraction |
| Reignite pulse/settle (s) | `reignite_pulse_s` / `reignite_settle_s` | 0.10 / 0.15 | ≈2.2 attempts/s incl. the 0.2 s poll |
| Pre-start Ar flow (sccm) | `ar_sccm` | 4 | pre-start only |
| Ar valve settle (s) | `valve_delay_s` | 1 | pre-start only |
| Current hold time (s) | `hold_s` | 5 | pre-start only; a drop restarts the count |

Every parameter in this table can be changed **while the run is running**
(2026-09-01). The browser posts the same body it would start a run with, the
server diffs it against what the run is using, and only what moved is applied
and logged. Timing changes land the next time that step runs; a gas flow that is
on right now is re-commanded immediately; the cycle count is re-read every
cycle. Each edit is recorded in the run's `_run_params.txt` under **CHANGES
DURING THE RUN**, with the elapsed time and cycle number it happened at.

**Advanced timing** is grouped by mechanism (2026-08-28): precursor fill
regulation, beam strike and reignite, the sample-bias bracket, the Ar soft
open, and end of run — each group with its own explanation under it, rather
than twelve fields in one grid under one paragraph explaining all of them.

The three `ar_soft_open_*` values are the odd ones out: they are not run
parameters at all. A manual open from the Hardware tab carries no run
parameters, so the server takes them from `config/run_params.json` as the UI
saves them (`Supervisor.set_soft_open_params`, called from `POST
/api/run_params` and once at startup) and they are sent with a run only so the
run's parameters file records what it actually ran with.

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

**Two independent copies of every run's trace, both automatic:**
- **Client-side auto-download**: `recordRun()` accumulates a per-run buffer
  from the moment Start is pressed (keyed on `recipe.started_at`); on
  completion `downloadRun()` writes a CSV — columns `elapsed_s,
  stage_temp_c, sample_current_a, precursor_dosing,
  precursor_pressure_torr, chamber_pressure_torr`, time zeroed to the Start
  press. Requires the browser to stay open through the run.
- **Server-side run export** (`DataLogger.start_run_export` /
  `write_run_sample` / `stop_run_export`, in `reactor/datalog.py`): opens
  `data/<run>/<stem>_run.csv` the instant *any* recipe
  starts (`Supervisor.start_recipe`, so this covers file recipes too, not
  just EE-ALD/EE-CVD), and appends one row per telemetry tick (5 Hz
  default) straight from the same sample dict the trend buffer uses — no
  extra device I/O. Columns are the same sample dict, dynamically
  discovered, plus `recipe_cycle`/`cycle_number`/`recipe_step`, so it's a
  *richer* trace than the client's fixed six columns.

  **This is the raw file** — every telemetry tick, nothing dropped, including
  the samples taken while the clock was frozen. `_bycycle.csv` and the merged
  file are the filtered views of it.

  `recipe_step` says what was happening, and that includes the two things that
  are *not* recipe steps:

  | `recipe_step` | meaning |
  |---|---|
  | `reignite` | the plasma was out and being restruck |
  | `pause` | the operator had the run paused |
  | anything else | the step's own description, e.g. `electron beam 10 s` |

  So narrowing a raw run file down to just the deposition is
  `recipe_step not in ("reignite", "pause")`, and that is exactly the filter
  `_bycycle.csv` already applies.

  There used to be a separate `paused` 0/1 column beside the step, so a reignite
  logged as *the electron-beam step, flagged paused*. A reignite is an event in
  its own right, not part of the step it interrupts (operator request,
  2026-08-21) — one descriptive column instead of two. `RecipeProgress.log_step`
  produces the label; `RecipeRunner.pause_reason` says which freeze is active,
  operator winning over reignite when both are. The merge reads **both** forms,
  so run files written before the change still merge correctly.

  **A cell is filled only on the rows where that channel was actually
  read.** The three poll loops run at different rates — DAQ `site.loop_hz`
  (~2 Hz, which also carries the HV supply), instruments `site.current_hz`
  (~5 Hz, and the row cadence), MFCs `site.mfc_hz` (~1 Hz) — so most rows
  carry a fresh ammeter reading and a blank pressure, flow or HV value. That
  is deliberate: repeating the last value would claim measurements that never
  happened. Blank means *not sampled here*, not zero. Commanded state
  (`dosing`, `beam_on`) is never blanked. The live UI is unaffected — it keeps
  showing the last known value.

  The **Glassman HV supply** contributes `hv_<id>_voltage` (V),
  `hv_<id>_current` (mA) and `hv_<id>_arcs` (count) — `hv_hv_*` with the
  current config. The program logs what the supply reports; the only thing it
  ever commands is HV off at the end of the run. Column names carry no units
  on purpose, so the analysis page's saved plot layout keeps matching them.
  See [GLASSMAN_FL.md](GLASSMAN_FL.md).

  The **four Keithley 2260B DC supplies** contribute `psu_<id>_voltage` (V) and
  `psu_<id>_current` (A) — `psu_stage_bias_*`, `psu_steering_*`,
  `psu_grid_bias_*`, `psu_collimating_*`. They share the Glassman's 2 Hz slow
  loop, so their cells fill on roughly every other row like pressure does.
  **`psu_stage_bias_voltage` is signed** by the run's bias-polarity toggle. The
  separate `psu` namespace is deliberate: each device declares its own prefix,
  which is what lets the Glassman keep its established `hv_hv_*` column names
  and not break saved plot layouts. See [KEITHLEY_2260B.md](KEITHLEY_2260B.md).

  (MFCs used to be polled inside the instrument loop, where a 0.45–0.9 s
  HTTP read throttled every sample and the export logged at 2.1 Hz rather
  than 5. See `Supervisor._mfc_loop`.)

  Closed in
  `Supervisor.finish_run()`, which the recipe runner calls however the run
  ends — done, aborted, or crashed — so a closed browser no longer loses
  anything. Independent of the operator's own Data Logging toggle; no button
  to press. Status (active / path / row count) is in
  `state().logging.run_export`.

Four more files are written automatically alongside it, same stem:

- `<stem>_bycycle.csv` — the same channels keyed by **fractional cycle
  number** instead of time, with paused (reignite / operator-pause) samples
  left out, so a property-vs-cycle plot is clean with no post-processing. See
  `RecipeRunner.cycle_fraction`.
- `<stem>_run_params.txt` — a one-time report of the exact parameters the
  run was launched with: header block (run, recipe, mode, cycles, cycle
  length, nominal run time), the plain-English summary of the cycle
  architecture and gas timeline, every UI parameter, and every setup / cycle /
  teardown step. Written for EE-CVD too.

  **Plain text, not JSON** (changed 2026-08-21). It was a JSON dump under a
  stem of its own, `<stamp>_ald_run_params.json`, timestamped at the moment it
  ran so it could land a second off the rest of the set. Zach could not open
  it ("I dont know how to open them"), which is fair — `.json` has no default
  handler on this machine. It is written UTF-8 **with BOM** so Notepad renders
  the em-dash in the summary. `DataLogger.format_run_params` builds it and is
  importable on its own, which is how the old `.json` files on disk were
  re-rendered as `.txt`.
- `<stem>_events.log` — every event the run produced, one line per event:
  wall clock, seconds since the run started, kind, message. Written as it
  happens and flushed, so an aborted or crashed run keeps what it had.
- `<stem>_errors.log` — the same lines, filtered to errors and flags. Both
  are plain text and **unbounded**; the in-memory buffers are the live view,
  these are the run's own record (2026-09-09).

### One folder per run

Every file a run writes goes into `data/<run name>/` — `data/Mo-015/` — so a
run is one thing to open, copy or send:

```
data/Mo-015/
    Mo-015_260821_131320_run.csv              trace, by time
    Mo-015_260821_131320_bycycle.csv          trace, by fractional cycle
    Mo-015_260821_131320_run_params.txt       the settings, readable
    Mo-015_260821_131320_events.log           every event, timestamped
    Mo-015_260821_131320_errors.log           just the errors and flags
    Mo-015_260821_131256_ellipsometer.csv     FS-1 sidecar for this run
    Mo-015_260821_131320_reactor_synced.csv   the post-run merge, once made
```

Requested 2026-08-21, when a flat `data/` holding five files per run stopped
being navigable. Details that matter:

- The folder is named for the **run**, not for a run's filename stem, so
  repeated attempts at Mo-015 collect together and are told apart by the
  timestamp already in each filename. An **unnamed** run falls back to the bare
  timestamp, `data/260821_131320/` (not the full stem, which would drag the
  recipe slug into the folder name).
- `DataLogger.run_dir` is set by `start_run_export` — the first moment both the
  run name and the run's start stamp are known — and cleared by
  `stop_run_export`. Outside a run, files fall back to `data/` itself.
- The **ellipsometer sidecar** is the awkward one: the FS-1 streams
  continuously, so its acquisition almost always opens *before* Start run, when
  neither the run name nor the folder exists. It therefore starts loose in
  `data/`, and `_adopt_open_sidecar` renames and moves it into the run folder
  when the run begins. Windows will not rename a file with an open handle, so
  that closes, moves, and reopens in append mode; nothing already captured is
  lost. A sidecar opened *during* a run goes straight into the folder.
- The **merged file** is written next to the reactor run it was built from
  (`/api/ellipsometer/merge`), not loose in `data/`.
- `/api/data/files` recurses (`rglob`) and reports each name **relative to the
  data dir** — `Mo-015/Mo-015_..._run.csv` — which is what the analysis page's
  pickers show and what `/api/data/file?name=` resolves. It sorts by mtime, not
  by name: with folders in play, path order is not chronological.
- Files written before this change still work; the listing finds them at either
  level. The ones on disk were moved into folders by hand at the same time.

### Hardware tab

Valves (grouped by control box, each individually actuable, with rename)
and the MFC tiles (live flow + settable flow + rename) up top; other
pressure gauges, other inputs, instruments, and primary-sensor detail
below. Any valve, MFC, or Baratron can be renamed from its ✎ button —
persisted to `config/labels.json`, blank reverts to the `reactor.yaml`
default.

### Diagnostics tab

Valve-identification sweep, data logging controls, the connections table,
**ellipsometer sync**, and the event log. A pinned header chip surfaces the
newest `error` or `flag` event for two minutes regardless of which tab is
open, so a fill-pressure flag during a run on the Run tab isn't missed just
because the log itself lives elsewhere.

**Ellipsometer sync** closes the loop on the FS-1: during a run the reactor
subscribes to the instrument's live broadcast and writes a per-acquisition
sidecar of `(fs_time -> reactor_clock)` pairs. Afterwards you refit the run in
the FS-1 software, download that file, drop it in here with this run's sidecar
and run export, and get back one plot-ready CSV, keyed by fractional cycle
number with reignite-paused samples dropped.

The output is a **union of instants, not a resampling**: every reactor sample
keeps its own row (ellipsometry columns blank) and every FS-1 measurement gets
its own row at its true time (reactor columns blank), told apart by a `source`
column. Nothing is interpolated onto anything else, so every number in the file
is one that was actually measured. The single exception is `cycle_number` on an
ellipsometry row, which is interpolated from the reactor's own recorded
time→cycle curve — the cycle number is a function of the clock, not a
measurement, and without it those rows could not be plotted against cycle at
all. An FS-1 point taken while the run was paused, or outside the cycling
window, is dropped rather than given a position the tool was never at.

The join fits a line through the sidecar
pairs rather than matching row-for-row, so a missed live sample doesn't break
it. The live thickness the stream carries is the instrument's uncalibrated
fit and is never treated as the answer. Code:
`reactor/analysis/ellipsometer_merge.py`, `POST /api/ellipsometer/merge`.

## The Analysis page (`/analysis`) — post-run plotting

A separate page, not a fourth tab, and
deliberately so: it reads finished CSVs and can touch no hardware, so it stays
out of the control UI entirely and can be opened alongside a live run. Linked
from the header of the main interface and from the Ellipsometer sync card.

It plots any CSV the reactor writes, picked from the data folder (read-only,
via `GET /api/data/files` and `/api/data/file`) or dropped in from disk:

| File | What it is |
|---|---|
| `*_bycycle.csv` | channels vs fractional cycle number, reignite-paused samples already dropped — the usual one |
| `*_reactor_synced.csv` | the above unioned with the measured ellipsometry rows, from Ellipsometer sync |
| `*_run.csv` | the raw per-time run export |

A **grid of plot cells** (1–4 columns, adjustable height). Each cell picks its
own **Y1**, an optional **Y2** on a second right-hand axis, and an **X**
column — defaulting to `cycle_number` when the file has one, which is the
point of the by-cycle export. Every axis takes an explicit **min/max** (blank
= auto-fit) and Y axes have a **log** toggle, since chamber pressure spans
decades. Hovering gives a crosshair and a value readout; each plot exports to
**PNG** or **CSV**, both named after the source file and the plot itself.

A 0/1 column — `beam_on`, `dosing`, `paused` — is detected automatically and
drawn as a **step**, because it is a state, not a measurement: interpolating
between samples would draw a valve as half open.

**The layout persists in `localStorage` and is re-applied by column name**, so
the intended workflow is: run → refit in the FS-1 software → Ellipsometer sync
(which now also saves the merged CSV into the data folder) → open Analysis,
and the same grid of plots repopulates against the new file. A column the new
file does not have keeps its selection, and the plot says so in amber rather
than silently blanking. `Export layout` / `Import layout` move a grid between
machines.

### Auger (AES) spectra

A drop box under Ellipsometer sync takes the AES tool's text export — a header
line (`Element ; Region 1 of 1; … ; AES;`) over `kinetic energy (eV)` and
intensity columns — and puts the file straight into its own plot at the top of
the grid: autoscaled on both axes, the same min/max boxes as every other plot,
no second Y axis.

A spectrum is its own **dataset**, held apart from the loaded run CSV, because
it shares no x axis with a deposition (kinetic energy, not cycle number).
Nothing is merged: loading a new run file repopulates the CSV plots and leaves
the Auger ones alone. A file holding several element windows gets one plot per
window. Spectra are stored beside the layout in `localStorage` so they come
back on a reload; re-dropping a file refreshes the plot it already has instead
of stacking up a second one, and removing a spectrum's last plot drops the
spectrum too.

**Confirmed on a real refit file end to end, 2026-08-25.**

## To actually run it in the lab

1. Close LabVIEW. Start `python -m reactor`, open in a real browser.
2. Confirm live readings look right (pressure, Baratrons, current, MFCs).
3. Set the run parameters. The fill valve (`rpm_top`) and precursor-Baratron
   labelling are confirmed; double-check the current threshold (500 µA) is
   right for the process.
4. Optionally press **Pre-start** first to strike the plasma and prime Ar +
   fill pressure ahead of time; confirm the dialog once the three valves are
   in REMOTE and supplies are on.
5. Press **Start run**. Watch the phase strip + Run monitor. The gentle flag
   will warn on pressure drift; the beam will reignite on its own if the
   plasma drops.
6. On completion the CSV downloads automatically.

## Likely next iterations

- Audit EE-CVD cycle timing the way EE-ALD was audited — `reactor-2ou`. The
  EE-ALD clock is now pinned to 0.1 s by `tests/test_run_timing.py`; EE-CVD's
  cycle clock (`_lit_s` / `_cycle_clock`, driven by the 0.2 s watchdog tick) has
  not had the same treatment.
- Identify the NI 9265 current outputs — `reactor-5u2` (low priority, not
  needed for normal operation).

## Runtime boundaries and recording health

Pre-start coordination now lives in `reactor/control/prestart.py`; recipe schema
and builders in `reactor/control/recipe_model.py`; recipe execution remains in
`reactor/control/recipe.py`. These extractions preserve the specified hardware
sequence and the difference between stopping and aborting pre-start.

Recording writes run in an ordered dedicated worker. The raw trace, by-cycle
file and sidecar preserve their existing formats and per-channel freshness. A
failed write or a full recording backlog shows a persistent recording error in
the header chip and event log; later successful samples do not clear evidence
of lost data. The experiment continues. Shutdown drains accepted writes.

A rejected duplicate Start request cannot rename the active experiment or
change its configured valve identifiers. If Abort arrives while a start is
waiting for recording preparation, no recipe hardware commands are started.
See [ARCHITECTURE.md](ARCHITECTURE.md) for thread ownership and timing limits.
