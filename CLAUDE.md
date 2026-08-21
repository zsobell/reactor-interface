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
**[docs/CONTROL_MODEL.md](docs/CONTROL_MODEL.md)**. Only two guards exist, both
requested: a gentle "flag" when precursor fill pressure drifts >20% off
setpoint (warns, never stops), and the Ar MFC refusing a nonzero setpoint
while its isolation valve is closed.

Automatic actions that DO exist, each individually requested 2014 do not remove
them as "unrequested safety machinery", and do not treat them as licence to add
more: MFCs zeroed + fill valve closed at run end; **HV commanded off at run end
or abort** (2026-08-21); the beam relay parked de-energised (9 V battery 2014 see
[docs/HARDWARE.md](docs/HARDWARE.md)); and the one-click pre-start abort.

Related standing conventions:
- **Every parameter Zach edits lives in the UI**, never a YAML/file/code edit.
- **Don't assume hardware facts** — query the reactor (`tools/discover_hardware.py`,
  read-only) or ask. The old LabVIEW VI is full of dead code; it is not a spec.
- **Don't casually actuate hardware.** Connecting is read-only by design. Actuate
  only when Zach directs it.

## Environment / commands

Windows 10. Python 3.12 in `.venv/` (deps already installed). Shell is PowerShell;
a Bash tool (Git Bash) is also available. No `npm`/`go`/`bash` on PATH.

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
  devices/ellipsometer.py    FS-1 live TCP stream: read-only subscriber + record decoder
  analysis/ellipsometer_merge.py   post-run join of a refit FS-1 file onto the reactor clock
  testing/virtual_reactor.py fake DAQ/MFC/instrument, real Supervisor above them (see tests/README.md)
  server/app.py         FastAPI HTTP + WebSocket; thin wrapper over Supervisor. Optional HTTP Basic
                        auth over everything incl. the WebSocket, on only if REACTOR_PASSWORD is set
  server/static/index.html   the control GUI (HTML+CSS+vanilla JS, no build step; 3 tabs: Run/Hardware/Diagnostics)
  server/static/analysis.html   post-run plotting page at /analysis (prototype). Reads finished
                        files only - no hardware, no telemetry - which is why it is a separate page,
                        not a 4th tab. Persistent grid of property-vs-cycle plots; layout in
                        localStorage. A dropped Auger (AES) spectrum is its own dataset (kinetic
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
- **3 MKS G50 MFCs** (Ar/H2/N2) at `192.168.2.221/.222/.223`. Flow, temperature,
  valve position and setpoint are **all read over Modbus** (~1 ms), from the
  register table each unit serves at `http://<host>/modbus.html` - NOT the
  generic MKS docs, which are a different family. Measured values are INPUT
  registers (FC 4), settable ones HOLDING (FC 3); getting that wrong is what
  made an earlier attempt conclude flow was unreachable. HTTP is used only for
  full scale (per device AND per gas: Ar 29 / H2 10 / N2 50 sccm, not in the
  Modbus map) and identity, at connect and on the slow refresh. Polled on their
  own loop at `mfc_hz` 6 Hz, deliberately just above the 5 Hz row rate so every
  logged row has a fresh flow. **Quirk: the MFC zeros its
  setpoint when the Modbus master disconnects** — flow only holds while the
  program stays connected. The Ar MFC has an operator-requested isolation
  interlock: setpoint refused above 0 sccm while `ar_pneumatic` is closed.
- **Keithley DMM6500** (USB) = sample current, the plasma/e-beam diagnostic.
- **Film Sense FS-1 ellipsometer** at `169.254.1.1:4001`, direct link-local
  Ethernet. **Read-only**: the reactor subscribes to the instrument's live
  broadcast and never writes to it (ports 4000/4010 deliberately untouched).
- **11 valves** across two control boxes, each on its own DAQmx DO task so one
  write never re-drives (and can't silently flip) a sibling on the same
  module; `plasma_ground` (cDAQ1Mod3 line9) is the e-beam relay (OFF = beam
  ON). **Its resting state is de-energised (OFF)** — the relay box runs off a
  9 V battery that drains only while the relay is energised, so run end and
  pre-start abort both park it off (and command HV off alongside). No
  valve-position feedback on the DAQ — last-commanded state persists to
  `config/valve_state.json` across restarts.
- **XP Glassman FL1.5F1.0** HV plasma supply (1500 V / 1.0 A) on USB, `COM8`
  **19200 8N1 address 1** - none of which is the documented default or readable
  off its DIP switches; the COM number moved once already. Polled at 2 Hz for
  voltage/current/arc count. **The only command this program sends it is HV OFF**
  (run end or abort, requested 2026-08-21); there is no way to set a level or
  turn HV on. Zach sets it by hand on the front panel. Note any Set command
  leaves the supply in REMOTE until LOC/REM is pressed.
  `python -m tools.probe_glassman` if it ever goes quiet.
  Full protocol + bring-up account in **[docs/GLASSMAN_FL.md](docs/GLASSMAN_FL.md)**.
- **NI 9265** current outputs: purpose unknown, deferred (`reactor-5u2`).
- **ACCES USB-AO16-8A** 8-channel analog output board: present and healthy,
  purpose not established, unused by this program.

## Current state (2026-08-21, after the Mo-015 run)

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

Both share a single-overlap gas-scheduling scheme (H2/N2 on/off around the
beam or the cycle) and an operator **pre-start** sequence (Ar on, fill
pulsing, strike-and-hold the plasma with unlimited retries, then ground the
beam) that primes the tool ahead of Start run, plus a one-click **Abort
pre-start** that undoes all of it (Ar off, fill off, relay de-energised, HV
off) and stays live after the plasma has struck. Live pressure/current/MFC-
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
Before 2026-08-21 the beam step slept its settle time before starting its
exposure clock and ticked in fixed 0.2 s steps, so Mo-015's 150 cycles ran
124 s long, and the countdown was extrapolated from measured pace in the
browser, so it wandered.

The **FS-1 ellipsometer** streams read-only into a per-acquisition sidecar
during a run. Its live readout (stream state, points banked this acquisition,
live fit + fit residual) is on the **Hardware** tab; its connection row is in
the Diagnostics connections table with every other device; and the post-run
sync — merging a refit FS-1 file back onto the reactor clock, keyed by cycle
number — lives on the **Analysis page** next to the plots it feeds. Nothing
ellipsometer-related is on the Diagnostics tab any more. Not yet validated
across a real deposition (`reactor-nde`).

The **Analysis page** (`/analysis`, prototype) plots the merged file: a
persistent grid of property-vs-cycle plots, layout remembered in localStorage
and re-applied by column name so a newly merged file repopulates it. A column
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

The header **alert chip** shows only conditions that are true right now (fill
pressure off setpoint, plasma out, a disconnected or faulted device) and
clears itself when they clear; it used to pin the newest error event for two
minutes. The event log is the history.

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
