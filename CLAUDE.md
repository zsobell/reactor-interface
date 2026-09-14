# CLAUDE.md — Reactor Interface

Project context for Claude Code. Read this first; it points to the deeper docs.

## What this is

A Python + browser control interface for Zach's **UHV electron-beam ALD reactor**,
replacing an old, buggy LabVIEW program. Single owner of state + hardware is
`reactor/supervisor.py`; the GUI is one self-contained `index.html`. Full picture:
**[README.md](README.md)**, then **[docs/HARDWARE.md](docs/HARDWARE.md)** and
**[docs/RUN_PROGRAM.md](docs/RUN_PROGRAM.md)**.

## The one rule that overrides everything

**Zach is the sole arbiter of reactor behavior. There are NO software interlocks,
limits, or automatic actions, and you do NOT add any without his explicit OK** —
propose it and wait for a yes. A previous version added unrequested safety
machinery built on assumptions and it destroyed trust; it was all removed. See
**[docs/CONTROL_MODEL.md](docs/CONTROL_MODEL.md)**. Only three guards exist, all
requested: a gentle "flag" when precursor fill pressure drifts >20% off
setpoint (warns, never stops), the Ar MFC refusing a nonzero setpoint while its
isolation valve is closed, and that valve being **pulsed open** rather than
flipped open (2026-08-26).

Automatic actions that DO exist, each individually requested — do not remove
them as "unrequested safety machinery", and do not treat them as licence to add
more: MFCs zeroed + fill valve closed at run end; **HV commanded off at run end
or abort** (2026-08-21); the beam relay parked de-energised (9 V battery — see
[docs/HARDWARE.md](docs/HARDWARE.md)); the **sample bias switched on/off around
each beam** (2026-08-26); and the one-click pre-start abort.

Related standing conventions:
- **Every parameter Zach edits lives in the UI**, never a YAML/file/code edit.
- **Don't assume hardware facts** — query the reactor (`tools/discover_hardware.py`,
  read-only) or ask. The old LabVIEW VI is full of dead code; it is not a spec.
- **Don't casually actuate hardware.** Connecting is read-only by design. Actuate
  only when Zach directs it.

## Environment / commands

Windows 10. Python 3.12 in `.venv/` (deps already installed). Shell is PowerShell;
a Bash tool (Git Bash) is also available. No `npm`/`go`/`bash` on PATH.

Normal launch is the Desktop shortcut **Reactor Interface** (pinnable):
`pythonw.exe -m reactor --port 8000 --open`, no console window, output to
`server.log`. Under pythonw `sys.stdout`/`sys.stderr` are None, so `__main__`
redirects them to that file - without it uvicorn dies on startup with no trace.

```bash
python -m reactor            # serve the interface at http://127.0.0.1:8000
python -m reactor --check    # validate config + print the I/O summary, no serving
python -m reactor -m tools.discover_hardware --survey-inputs   # read-only hardware probe
```

Use a **real browser** (Chrome/Edge) for the UI — embedded preview panes don't
composite scroll correctly.

**Hard constraint:** NI-DAQmx gives one program exclusive use of a module's
analog input, so **the LabVIEW VI must be closed** before running this against the
DAQ, or it reports "resource reserved". MFCs (network) and the DMM (USB) are
unaffected.

### Quick sanity checks after changes

```bash
.venv\Scripts\python.exe -c "import reactor.supervisor, reactor.server.app, reactor.control.recipe"
.venv\Scripts\python.exe -m reactor --check
.venv\Scripts\python.exe -m tests.run_all
```

There is no pytest suite, on purpose (see tests/README.md) - but there is a
real one: `tests/test_*.py` run a real `Supervisor` against
`reactor/testing/virtual_reactor.py`'s fake DAQ/MFC/instrument devices (fake
hardware boundary, real everything above it - not a hand-rolled Supervisor
stand-in). Run any single file directly (`python -m tests.test_ee_cvd_recipe`)
or all of them (`python -m tests.run_all`). Any change to
`supervisor.py`/`control/recipe.py`/`datalog.py` should pass this before the
app-level check. The app itself is still verified by loading it and driving
the API/UI - the virtual reactor proves control logic, not that ai1 is
really the precursor-1 Baratron or that a valve physically opens.

## Architecture (where things live)

```
config/reactor.yaml     the ONLY hardware map (channels, gauge curves, valves, MFCs). Errors name the key.
config/recipes/*.yaml   file recipes; EE-ALD/EE-CVD are built from UI params instead (build_ald_recipe/build_cvd_recipe)
config/labels.json      operator display-name overrides (valves/MFCs/gauges), persisted from the UI
config/run_params.json  Run-tab parameters, SERVER-owned so every browser (incl. over Tailscale)
                        sees the same values; each browser's localStorage is only a cache
config/analysis_layout.json  Analysis-page plot grid, SERVER-owned for the same reason
                        (2026-08-26). Dropped Auger spectra stay per-browser: data, not layout
config/valve_state.json last-commanded valve state, restored (not hardware-read) into the model at startup
reactor/
  config.py             pydantic validation of the YAML
  supervisor.py         single owner of state + all hardware commands; control loop; fill-pressure
                        regulator; pre-start sequence; valve-ID sweep; telemetry fan-out over WebSocket
  datalog.py            tab-delimited run logs
  devices/{base,nidaq,mks_mfc,instrument}.py   DAQ (one DAQmx task per DO line; no analog-output
                        path on purpose), MKS G50 MFCs (flow/temp/setpoint all over Modbus;
                        HTTP only for full scale + identity), DMM6500
  control/recipe.py     recipe engine + step types (dose/wait/electron_beam/beam_start/beam_stop/
                        start_fill/...) + build_ald_recipe/build_cvd_recipe for the two UI-driven modes
  devices/glassman_fl.py     XP Glassman FL HV plasma supply over serial. Polls V/I/arc-count;
                        the ONE command sent is hv_off() at run end / abort. No setpoints,
                        no HV-on, ever (docs/GLASSMAN_FL.md)
  devices/keithley_2260b.py  4 Keithley 2260B DC supplies (stage bias / steering / grid /
                        collimating). Logs V+I; switches the three COIL outputs on at pre-start,
                        off at run end; the sample bias is armed at pre-start and its output
                        brackets each beam instead. Sets voltage on the sample-bias unit only.
                        Ports resolved by USB SERIAL, never by COM number (docs/KEITHLEY_2260B.md)
  devices/ellipsometer.py    FS-1 live TCP stream: read-only subscriber + record decoder
  analysis/ellipsometer_merge.py   post-run join of a refit FS-1 file onto the reactor clock
  testing/virtual_reactor.py fake DAQ/MFC/instrument, real Supervisor above them (see tests/README.md)
  server/app.py         FastAPI HTTP + WebSocket; thin wrapper over Supervisor. Optional HTTP Basic
                        auth over everything incl. the WebSocket, on only if REACTOR_PASSWORD is set
  server/static/index.html   the control GUI (HTML+CSS+vanilla JS, no build step; 3 tabs:
                        Run / Hardware (valves, MFCs, Pressure Sensors, Thermocouples, Ammeter,
                        Power supplies across a full row, Ellipsometer) / Diagnostics (event log,
                        data logging, connections, and a collapsed valve-ID sweep))
  server/static/analysis.html   post-run plotting page at /analysis. Reads finished
                        files only - no hardware, no telemetry - which is why it is a separate page,
                        not a 4th tab. Persistent grid of property-vs-cycle plots; layout is
                        SERVER-owned (config/analysis_layout.json), localStorage only a cache. A dropped Auger (AES) spectrum is its own dataset (kinetic
                        energy, not cycle number) with its own plot, never merged into the run file.
tools/                  discover_hardware.py (read-only), watch_channels.py (read-only), pulse_line.py (drives one line),
                        probe_glassman.py (read-only: finds the HV supply's port/baud/address)
docs/                   HARDWARE, RUN_PROGRAM, CONTROL_MODEL, IDENTIFYING_HARDWARE, LABVIEW_ANALYSIS, GLASSMAN_FL
```

## Hardware quick reference (all identified; details in docs/HARDWARE.md)

- **NI cDAQ** two chassis. Pressure = cold cathode `cDAQ2Mod1/ai3`, curve
  `P[Torr]=10^(V-10)`. 3 Baratrons on cDAQ2Mod1: ai0 Ar, ai1 precursor-1 dose,
  ai2 precursor-2 dose (10 Torr heads, 1 V = 1 Torr — confirmed). Stage TC
  `cDAQ1Mod4/ai1`, precursor bubbler TC `cDAQ1Mod4/ai0`.
- **3 MKS G50 MFCs** (`ar`, `mfc1`, `mfc2`) at `192.168.2.221/.222/.223`. Flow, temperature,
  valve position and setpoint are **all read over Modbus** (~1 ms), from the
  register table each unit serves at `http://<host>/modbus.html` - NOT the
  generic MKS docs, which are a different family. Measured values are INPUT
  registers (FC 4), settable ones HOLDING (FC 3); getting that wrong is what
  made an earlier attempt conclude flow was unreachable. HTTP is used only for
  full scale (per device AND per gas: 29 / 7 / 50 sccm as currently set, not in the
  Modbus map) and identity, at connect and on the slow refresh. Polled on their
  own loop at `mfc_hz` 6 Hz, deliberately just above the 5 Hz row rate so every
  logged row has a fresh flow. **Quirk: the MFC zeros its
  setpoint when the Modbus master disconnects** — flow only holds while the
  program stays connected. The Ar MFC has an operator-requested isolation
  interlock: setpoint refused above 0 sccm while `ar_pneumatic` is closed.
  **The gas is selected on the MFC itself, and it MOVES** - the `.222` unit ran
  H2 and now reports `2: NH3` (gas-table index and name), full scale 7 sccm.
  Hence the rule (Zach, 2026-09-09): **nothing static may name a gas, and the
  gas actually in use is labelled everywhere.** The two process-gas lines are
  therefore identified by CHANNEL - ids `mfc1`/`mfc2`, labels "MFC 1/2 -
  reactive background", UI params `mfc1_gas_*`/`mfc2_gas_*` (they were
  `h2_gas_*`; `server/app.py:migrate_params` renames a saved or stale-browser
  one on read). What the line is CALLED comes from the device at runtime:
  `Supervisor._gas_name` (operator rename > device gas, **empty if neither** -
  it never invents one) and `gas_label` for display, feeding the Run tab's gas
  table and hint, the MFC plot legend, the Hardware tile label (`mfc_label`
  swaps the head, keeps "- reactive background"), the alert chips, the event
  log, the recipe's step prose (`recipe.set_gas_display_names`) and every log
  HEADING (`DataLogger.set_gas_names`: `mfc_mfc1` -> `mfc_NH3`, `mfc.mfc1.flow`
  -> `mfc.NH3.flow`, the manual log's "MFC 1 sccm" -> "NH3 sccm"). With no gas
  reported it falls back to the channel, never to a guess. **The `ar` line is
  still keyed by gas** (id `ar`, `gas: "Ar"`, and the `ar_pneumatic` valve it is
  interlocked to) - it is out of the gas-scheduling table and renaming it would
  reach into pre-start, the soft open and the interlock; say the word and it
  becomes MFC 3. Consequence Zach accepted: a saved Analysis plot keyed on an
  old column name has to be re-picked once.
- **Keithley DMM6500** (USB) = sample current, the plasma/e-beam diagnostic.
- **Film Sense FS-1 ellipsometer** at `169.254.1.1:4001`, direct link-local
  Ethernet. **Read-only**: the reactor subscribes to the instrument's live
  broadcast and never writes to it (ports 4000/4010 deliberately untouched).
- **11 valves** across two control boxes, each on its own DAQmx DO task so one
  write never re-drives (and can't silently flip) a sibling on the same
  module; `ar_pneumatic` is **soft-opened** — every open of it, from anywhere,
  is one 0.05 s bleed pulse, a 0.5 s wait, and only then open (`soft_open` in
  the YAML, timings in the UI's Advanced timing; `Supervisor.set_valve`); `plasma_ground` (cDAQ1Mod3 line9) is the e-beam relay (OFF = beam
  ON). **Its resting state is de-energised (OFF)** — the relay box runs off a
  9 V battery that drains only while the relay is energised, so run end and
  pre-start abort both park it off (and command HV off alongside). No
  valve-position feedback on the DAQ — last-commanded state persists to
  `config/valve_state.json` across restarts.
- **XP Glassman FL1.5F1.0** HV plasma supply (1500 V / 1.0 A) on USB, `COM8`
  **19200 8N1 address 1** - none of which is the documented default or readable
  off its DIP switches; the COM number moved once already. Polled at 2 Hz for
  voltage/current/arc count. **The only command this program sends it is HV OFF**
  (run end or abort, requested 2026-08-21; confirmed on hardware 2026-08-25); there is no way to set a level or
  turn HV on. Zach sets it by hand on the front panel. Note any Set command
  leaves the supply in REMOTE until LOC/REM is pressed.
  `python -m tools.probe_glassman` if it ever goes quiet.
  Full protocol + bring-up account in **[docs/GLASSMAN_FL.md](docs/GLASSMAN_FL.md)**.
- **4 Keithley 2260B DC supplies** on USB, SCPI over a **CDC virtual COM port**
  (NOT USBTMC like the DMM6500): `stage_bias` 2260B-250-4 #1412016,
  `steering` 2260B-80-13 #1408023, `grid_bias` 2260B-800-1 #1407084,
  `collimating` 2260B-250-9 #1405224. **Matched by USB serial, never by COM
  number** - four near-identical supplies on one rack and Windows renumbers
  ports freely; the driver refuses a unit whose *IDN? serial disagrees. Outputs
  are switched ON at pre-start and OFF at run end/abort, and are deliberately
  NOT cycled with the beam (the collimating coil stabilises the plasma when the
  beam dump is grounded). Only the sample-bias unit's VOLTAGE is ever set, from
  the run's Sample bias field, and only when it is non-zero. Since 2026-08-25 the
  Hardware tab also carries per-supply voltage/current fields, an output toggle
  and a CV/CC light, so all four can be driven by hand; nothing sets a current
  AUTOMATICALLY. Since 2026-08-26 the sample bias no longer runs all run: pre-start
  only ARMS it and its output brackets the beam (see Current state).
  **[docs/KEITHLEY_2260B.md](docs/KEITHLEY_2260B.md)**.
- **NI 9265** current outputs: purpose unknown, deferred (`reactor-5u2`).
- **ACCES USB-AO16-8A** 8-channel analog output board: present and healthy,
  purpose not established, unused by this program.

## Current state (2026-08-26)

All I/O identified and working; MFC read+write, every valve, and every
display label controllable from the UI, with valve state surviving a
restart. Two run modes, fully driven from a tabbed UI (Run / Hardware /
Diagnostics):

- **EE-ALD** — background fill regulation → dose → beam-with-
  current-check/reignite → pump, per cycle. **Run and tuned on real
  hardware** (`reactor-alz`, `reactor-2z1`, confirmed 2026-08-06).
- **EE-CVD** — background fill regulation → beam held on for the whole run
  (its own reignite watchdog) with dose+pump-A cycling on top of it; pump A
  is lit-time gated so it locks to the plasma, the dose never is (freezing a
  precursor pulse would dump precursor into the chamber). Logic-verified
  against the virtual reactor only, not yet confirmed on real hardware.

Both share a single-overlap gas-scheduling scheme (MFC 1/MFC 2 on/off around the
beam or the cycle - or **Simultaneous**, added 2026-08-26, where both gases
cover the WHOLE window at their own flows instead of dividing it; the % is
greyed out, and exactly one gas set to Simultaneous refuses the run at Start
rather than running as a 100% "first") and an operator **pre-start** sequence (Ar on, fill
pulsing, strike-and-hold the plasma with unlimited retries, then ground the
beam) that primes the tool ahead of Start run, plus a one-click **Abort
pre-start** that undoes all of it (Ar off, fill off, relay de-energised, HV
off) and stays live after the plasma has struck. Abort is the ONLY way out of
a pre-start since 2026-08-28: the Stop button beside it (and `POST
/api/prestart/stop`) went, because it only ended the *sequence* and left the
tool primed, so it greyed out just when backing out was wanted and Abort had
to be pressed anyway. `Supervisor.stop_prestart` stays - `abort_prestart`
calls it. Live pressure/current/MFC-
flow/temperature plots each have an independent time window, hover, and
drag-to-zoom. Every run's trace is captured twice, automatically: a
client-side CSV auto-download, and a richer server-side CSV
(`DataLogger.start_run_export`) that survives a closed browser, alongside a
by-cycle CSV and a run-parameters **.txt** report.

**The three run files are raw / filtered / merged.** `_run.csv` is the raw
trace: every telemetry tick, nothing dropped, reignites and operator pauses
included. `_bycycle.csv` is the same rows minus anything frozen and minus
setup/teardown, keyed by fractional cycle. The merged `_reactor_synced.csv` is
that plus the ellipsometry. There is **no `paused` column** - since 2026-08-21
`recipe_step` names a freeze itself (`reignite`, `pause`), because a reignite is
an event in its own right, not part of the beam step it interrupts. Filtering a
raw file to the deposition is `recipe_step not in ("reignite", "pause")`. The
merge honours the old `paused` column too, so pre-2026-08-21 runs still merge.

**One run = one folder.** Everything a run writes lands in `data/<run name>/`
(`data/Mo-015/`), sharing one stem, with an unnamed run falling back to
`data/<stamp>/`. `DataLogger.run_dir` is set by `start_run_export` and cleared
by `stop_run_export`. The ellipsometer sidecar is the awkward case - the FS-1
streams continuously so its acquisition opens BEFORE the run exists - so it
starts loose in `data/` and `_adopt_open_sidecar` renames and moves it in when
the run begins (close/move/reopen: Windows will not rename an open file). The
merged file is written next to the run it came from. `/api/data/files`
recurses and returns names relative to the data dir, sorted by mtime.

The parameters file is **plain text, not JSON** - Zach could not open a .json.
`datalog.format_run_params` renders it (header, summary, every UI parameter,
every setup/cycle/teardown step) as UTF-8-with-BOM so Notepad shows the
em-dash. It is importable on its own, which is how the historical .json files
were re-rendered as .txt.

**Run timing is exact.** A cycle takes the sum of its step durations, and the
"est. remaining" countdown is that number times the cycle count, ticking down
in real time and freezing only for a reignite or an operator pause
(`RecipeRunner.run_remaining_s`; `tests/test_run_timing.py` holds it to 0.1 s).

**However a run ends, it ends the same way (2026-09-09).** An abort never
reaches the teardown, so it used to stop at "beam grounded" and leave the relay
ENERGISED on its 9 V battery, and the Run panel kept the bar and the cycle/step
readouts frozen as though the run were still going. Now `finish_run` grounds
first (via the runner), commands HV off, and only THEN parks the relay
de-energised - the same resting state a clean run's teardown leaves - and the
panel clears for anything that is not a completed run (a finished one keeps its
final tally). Pre-start abort was reordered to match: HV off, then release the
ground.

**Switching a scheduled gas OFF mid-run now stops it (2026-09-09).** An
unticked gas simply vanishes from the rebuilt recipe, and `apply_params` only
looked at gases present in BOTH, so the live schedule object survived and kept
commanding its old flow every cycle - a line switched off, showing 0 in every
field, still being driven to 0.8 sccm. A gas that disappears is now shut off at
once and dropped from the plan; one that appears is picked up
(`tests/test_live_params.py`).
Before 2026-08-21 the beam step slept its settle time before starting its
exposure clock and ticked in fixed 0.2 s steps, so Mo-015's 150 cycles ran
124 s long, and the countdown was extrapolated from measured pace in the
browser, so it wandered. How the whole timing scheme holds together - the four
loops, the one-wait-per-step rule, what freezes and why a run is deterministic
in exposure rather than wall time - is
**[docs/RUN_PROGRAM.md](docs/RUN_PROGRAM.md)**, "Timing: how a run stays on
schedule".

The **FS-1 ellipsometer** streams read-only into a per-acquisition sidecar
during a run. Its live readout (stream state, points banked this acquisition,
live fit + fit residual) is on the **Hardware** tab; its connection row is in
the Diagnostics connections table with every other device; and the post-run
sync — merging a refit FS-1 file back onto the reactor clock, keyed by cycle
number — lives on the **Analysis page** next to the plots it feeds. Nothing
ellipsometer-related is on the Diagnostics tab any more. **Confirmed working
across a real deposition, 2026-08-25.**

The **Analysis page** (`/analysis`) plots the merged file: a
persistent grid of property-vs-cycle plots, layout owned by the SERVER since
2026-08-26 (`config/analysis_layout.json` via `/api/analysis_layout`, browser
localStorage demoted to a cache - it used to be localStorage-only, so every
machine had a different grid) and re-applied by column name so a newly merged
file repopulates it. A column
counts as numeric on the cells that HAVE a value, not on the row count — in a
merged file nearly every column is sparse by construction, and the old rule
silently hid thickness, resistivity, bubbler, stage temp and the HV channels
from the dropdown entirely. Series are drawn as ONE line through every datum;
a row with no value for that column is skipped, not treated as a break (which
is what made merged files look dashed). An **Auger (AES) spectrum** dropped on
the same page gets its own autoscaled plot, kept as a separate dataset.

The **Glassman FL plasma supply**: voltage, current and arc count polled at
2 Hz on the slow control loop, shown on a Hardware-tab card with no setpoint
inputs, and logged into both the manual log and the per-run CSV. **The one
command this program sends it is HV OFF** at run end or abort (requested
2026-08-21) — preserving the front-panel levels, since the FL's Set frame
always carries them. There is still no way to set a level or turn HV on, and
`disconnect()` still commands nothing. Setpoint control remains a deliberate
not-yet (`docs/CONTROL_MODEL.md`, `docs/GLASSMAN_FL.md`).

The header **alert chips** show only conditions that are true right now (fill
pressure off setpoint, plasma out, a disconnected or faulted device, a setpoint
its measurement disagrees with) and clear themselves when they clear; they used
to pin the newest error event for two minutes. Up to **four at once** since
2026-09-01, with a "+N more" chip past that. The event log is the history.

**Pause now stops the ACTION as well as the clock (2026-09-01)** - requested:
"in the purge step the timer keeps moving, in the e-beam step the beam stays
on". Pause was only honoured at a step BOUNDARY, and `_sleep` (every wait, every
dose) never looked at it at all. Now a pause closes the dose valve, grounds the
beam, and freezes every clock; resume reopens/re-strikes with the step's
remaining time intact and a fresh strike-settle window. The sample bias and the
scheduled gases are deliberately left alone - Zach's call when asked. See
`tests/test_pause.py`.

**Run parameters are editable mid-run (2026-09-01)**, also requested - an N2
flow set too low on Mo-017 could not be raised, because a recipe was a snapshot
taken at Start. `Supervisor.update_run_params` diffs against what the run is
using and copies the new numbers into the RUNNING steps; a gas already flowing
is re-commanded at once, the fill regulator is retuned rather than restarted,
and the cycle count is re-read every cycle (lowered below the current cycle, the
run finishes that cycle and stops). Every edit lands in the parameters .txt
under **CHANGES DURING THE RUN** with its elapsed time and cycle number
(`tests/test_live_params.py`).

**Setpoint-vs-measurement warnings (2026-09-01)**: the precursor fill
pressure's rule, applied to every MFC and to any supply whose output is on -
`Supervisor.setpoint_flags()`, same Fill flag tolerance, cleared the moment the
device catches up. **Warn only**: nothing is refused, clamped or changed, and no
minimum-flow threshold was invented (the MFCs' own `min_setpoint` is 0.015 sccm
and would not have caught the 0.6 sccm case anyway). A supply in **CC mode is
exempt** (2026-09-09): a current-limited supply sits below its voltage setpoint
by definition, and flagging the coils for it produced a warning that stood for
whole runs.

**A new run never inherits the previous run's ellipsometer acquisition
(2026-09-01)**. The FS-1 broadcasts whether or not its own acquisition is
running, so stopping one run and starting another usually leaves no gap in the
stream - and `_adopt_open_sidecar` used to hand the second run the first one's
open file, points and all, still in the FIRST run's folder. A sidecar is now
adopted once; a second run gets a second acquisition. The >5 s idle-gap rule
that also starts a new one was already there and already worked
(`ellipsometer.idle_gap_s`).

The **four Keithley 2260B DC supplies** (stage bias, steering, grid,
collimating) are in as of 2026-08-25: voltage and current logged for all four,
monitor cards on the Hardware tab, and the three COIL **outputs switched on at
pre-start and off at run end/abort** — confirmed on hardware 2026-08-25. They
stay on for the whole run and are never cycled by plasma events. The **sample
bias** is the conditional one - a run field for both EE-ALD and EE-CVD, with a
+/- toggle that records lead orientation and signs the logged voltage (the
supply is single-quadrant, so the sign never reaches the instrument). *Min
beam current (µA)* moved into the Advanced timing collapsible to make room.
Current limits are set on the front panels and never touched by this program.

**The sample bias now FOLLOWS THE BEAM (2026-08-26)** - requested because a
bias held on for the whole run made the stage thermocouple unreadable
("untenable"; Zach wants good TC data while the beam is off). Pre-start ARMS
the supply (level + polarity, output off); each beam then switches its output
on `Bias lead before beam` s early and off `Bias trail after beam` s late,
both in Advanced timing,
0.2 s by default. EE-ALD brackets every cycle's beam step; EE-CVD, whose beam
is one long step, brackets the run. A reignite does NOT cycle it - it brackets
the beam step, not every relay flip. Both flips are scheduled tasks that run
down inside pump A / pump B, so a cycle still takes exactly the sum of its step
durations. The level is written once per run, so a Hardware-tab adjustment
mid-run is not fought. However a run ends, queued flips are cancelled and the
output goes off. Virtual-reactor verified only
(`tests/test_sample_bias_bracket.py`); not yet run on hardware.

**The Ar pneumatic is SOFT-OPENED (2026-08-26)**, also on request: opening it in
one flip dumps the Ar built up behind it into the reactor, so every open - the
Hardware tab, pre-start, a recipe step - bleeds it in with one 0.05 s pulse,
waits 0.5 s, and only then opens it (~0.55 s). It was 5 pulses for the first
few hours of 2026-08-26; Zach cut it to 1 the same day because the pneumatic is
too slow for a short command to move it far, so the extra pulses just made
extra inrushes and the chamber gauge tripped off regardless. It lives in
`Supervisor.set_valve`, which is what makes "any time" true; the valve is
flagged `soft_open` in the YAML (a plumbing fact) and the three numbers are UI
settings in Advanced timing. Because a manual open carries no run parameters,
those numbers are read SERVER-side from `config/run_params.json`
(`set_soft_open_params`, called on every save and at startup). The pulses' closes
use the quiet write path so they do not trip the isolation interlock that
zeroes the Ar setpoint on a real close. Virtual-reactor verified only
(`tests/test_soft_open.py`).

**UI pass, 2026-08-28.** Four requested changes, no behaviour underneath
them: (1) the pre-start **Stop** button is gone, described above. (2) **Advanced
timing** is grouped by mechanism - precursor fill regulation, beam strike and
reignite, sample bias bracket, Ar soft open, end of run - each group carrying
the part of the old single hint that is about it; field labels say what the
number does (`Valve settle` → `Ar valve settle`, `Min current` → `Min beam
current`, `Pump A` → `Pump A after dose`, `Bias lead` → `Bias lead before
beam`, and so on - the param KEYS are untouched, only the labels). (3) The
**estimate shows at all times**: between runs the countdown slots read
"est. duration" / "finish if started now" for the run the parameters currently
describe, refreshed as they are edited, and switch back to "est. remaining" /
"finish" once a run owns them. The number comes from `POST /api/run/estimate`,
which builds the recipe a run WOULD build and returns `cycle_seconds() x
cycles` - the same arithmetic the countdown starts from, deliberately not a
second copy of it in the browser (`tests/test_run_estimate.py`). (4) On the
Hardware tab the wide **Power supplies** card moved up under Valves/MFCs, so
Pressure Sensors, Thermocouples, Ammeter and Ellipsometer sit in one row of
four beneath it.

The **Connections table** distinguishes three states, not two: `OK`, `RETRY`
(amber) for a device the reconnect loop is still working on, and `FAIL` for one
it is not. A retrying row leads with the stable part - `try 37, 3s ago · down
3m34s` - and the driver's error trails it, because the error text changes shape
between attempts. The table is `table-layout:fixed` with an explicit colgroup:
before 2026-08-26 it re-solved its columns from cell content on every repaint,
so the Device column visibly jumped every time a reconnect message changed
width. Full cell text is in a `title` tooltip since the cells now ellipsise.

The **event log** is the Diagnostics tab's main panel, top-left (it and the
valve-ID card swapped places 2026-08-26 - the sweep is now a collapsed card at
the bottom). It holds 200 000 entries server-side (20 000 until 2026-09-09)
and the browser scrolls all of them, seeded once from `/api/events` and
appended from the live frame (which carries only the last 200 - sending the
whole buffer at 5 Hz would be ~1 MB/s over Tailscale). `server.log` is the
permanent record; the in-memory buffer starts empty after a restart.

Above it since 2026-09-09 is a separate **error log** - same entries,
filtered to `Supervisor.ERROR_KINDS` (`error` and `flag`), fed the same way
from `/api/errors` and the frame's `errors` tail - because the errors were
being found by scrolling the event log. Every run also writes BOTH to its own
folder as it goes: `<stem>_events.log` and `<stem>_errors.log`, plain text,
unbounded, flushed per line so an aborted run keeps them
(`DataLogger.write_event`).

Diagnostics has a **Shut down server** button (2026-08-25): it stops this
server AND kills any other reactor server still running, then you restart from
the shortcut. It replaced a Restart button that re-exec'd the process - that
lasted a few hours, because an old instance survived the restart and sat holding
COM8-COM12 while the new one owned port 8000, leaving every device unreachable.
**The button still did not work until 2026-08-28**, and the reason was not in
this program's teardown at all: uvicorn drains open connections BEFORE running
the lifespan shutdown, and that drain is unbounded by default, so one WebSocket
whose peer never answers the close (a laptop asleep over Tailscale) parked the
whole stop - port 8000 released, `Supervisor.stop()` never reached, DAQ and
COM8-COM12 held indefinitely, and no log line to say so because uvicorn says it
at INFO. The drain is bounded (`SHUTDOWN_DRAIN_S`; a timeout, not `force_exit`,
so the teardown still runs), and the hard deadline behind it can fire - it used
to die on a `NameError` for a `log` that `__main__.py` never defined.

**Reworked again 2026-09-10** (`reactor-1sm`), because it was still slow and
still could not be trusted. Zach: "20 s hold is way too long, and there is no
way for me to know if it worked or not. I need some confirmation things are
shut down and ready to be booted again." Three things were wrong:

- **It was slow before it said anything.** The sibling sweep shelled out to
  `powershell.exe` for a `Win32_Process` query - 1-3 s of cold start - INSIDE
  the request, so the button greyed and sat there. Servers now register
  themselves in `config/instances/` (`reactor/instances.py`) and are ended
  through the Win32 API; the sweep is microseconds and the whole request
  answers in ~20 ms. **Gotcha found building it:** the PID-reuse guard cannot
  compare `sys.executable` against `QueryFullProcessImageNameW` - under a venv
  those legitimately DISAGREE (the `.venv\Scripts` shim vs the base interpreter
  Windows actually runs), so the first cut matched nothing and silently swept
  nothing. The registry records both and compares `image`.
- **"Server stopped" was never evidence.** uvicorn releases the listening
  socket BEFORE the lifespan teardown, so the page's "not responding any more"
  arrived while the DAQ and COM8-COM12 could still be held. The teardown now
  runs INSIDE `POST /api/server/shutdown`, which answers with a **receipt** -
  every step, what was released (by port: `grid_bias (COM11)`, `DAQ tasks`),
  what failed, elapsed - and the button shows it. `Supervisor.stop()` returns
  that receipt and is idempotent, so the lifespan calling it again is a no-op.
  The page then polls at 250 ms (was 1500) purely to confirm the process is
  gone, and ends on "Stopped and released - ready to start again".
- **A copy that failed to bind still lingered**, which is what the old note
  here claimed was fixed. uvicorn 0.52's `Server.startup()` does
  `sys.exit(STARTUP_FAILURE)` on a bind failure, and that `SystemExit`
  propagates out of `server.run()` - jumping straight over the `os._exit` that
  FOLLOWED it. Seen on 2026-09-10: two servers started in the same second, one
  bound port 8000, the other lingered holding COM10+COM11, so `steering` and
  `grid_bias` looked like dead hardware for an hour. `server.run()` is now in a
  `try/finally` and the exit is in the finally, checked structurally (AST, not
  a string match) by `tests/test_server_shutdown.py`.

`SHUTDOWN_DEADLINE_S` is 3 s, down from 20: it was sized for a teardown that
had not happened yet, and now everything after the response is socket cleanup.

Shutdown kills siblings first, then tears itself down and calls `os._exit`
(falling out of `main()` does NOT end the process - a non-daemon thread keeps
it alive, which is how that orphan survived). It aborts a running recipe AND a
pre-start - running or merely primed - because Zach's rule is "safety over data
collection"; gas stops either way. The confirm dialog spells that out; there is
no guard beyond the dialog.

Full history — everything shipped and everything still open — is in the
**bd** issue tracker (`bd list --status=closed`, `bd ready`), not just this
file.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
