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
```

There is no pytest suite. Control-logic is spot-checked with fake-DAQ harnesses in
the scratchpad; the app is verified by loading it and driving the API/UI.

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
  devices/{base,nidaq,mks_mfc,instrument}.py   DAQ (one DAQmx task per DO line), MKS G50 MFCs
                        (HTTP read / Modbus write), DMM6500
  control/recipe.py     recipe engine + step types (dose/wait/electron_beam/beam_start/beam_stop/
                        start_fill/...) + build_ald_recipe/build_cvd_recipe for the two UI-driven modes
  server/app.py         FastAPI HTTP + WebSocket; thin wrapper over Supervisor
  server/static/index.html   the entire GUI (HTML+CSS+vanilla JS, no build step; 3 tabs: Run/Hardware/Diagnostics)
tools/                  discover_hardware.py (read-only), watch_channels.py (read-only), pulse_line.py (drives one line)
docs/                   HARDWARE, RUN_PROGRAM, CONTROL_MODEL, IDENTIFYING_HARDWARE, LABVIEW_ANALYSIS
```

## Hardware quick reference (all identified; details in docs/HARDWARE.md)

- **NI cDAQ** two chassis. Pressure = cold cathode `cDAQ2Mod1/ai3`, curve
  `P[Torr]=10^(V-10)`. 3 Baratrons on cDAQ2Mod1: ai0 Ar, ai1 precursor-1 dose,
  ai2 precursor-2 dose (10 Torr heads, 1 V = 1 Torr — confirmed). Stage TC
  `cDAQ1Mod4/ai1`, precursor bubbler TC `cDAQ1Mod4/ai0`.
- **3 MKS G50 MFCs** (Ar/H2/N2) at `192.168.2.221/.222/.223`. Read over the
  device HTTP interface, write setpoint over Modbus. **Quirk: the MFC zeros its
  setpoint when the Modbus master disconnects** — flow only holds while the
  program stays connected. The Ar MFC has an operator-requested isolation
  interlock: setpoint refused above 0 sccm while `ar_pneumatic` is closed.
- **Keithley DMM6500** (USB) = sample current, the plasma/e-beam diagnostic.
- **11 valves** across two control boxes, each on its own DAQmx DO task so one
  write never re-drives (and can't silently flip) a sibling on the same
  module; `plasma_ground` (cDAQ1Mod3 line9) is the e-beam relay (OFF = beam
  ON). No valve-position feedback on the DAQ — last-commanded state persists
  to `config/valve_state.json` across restarts.
- **NI 9265** current outputs: purpose unknown, deferred (`reactor-5u2`).

## Current state (2026-08-06)

All I/O identified and working; MFC read+write, every valve, and every
display label controllable from the UI, with valve state surviving a
restart. Two run modes are built and logic-verified with fake-DAQ /
fake-supervisor harnesses, fully driven from a tabbed UI (Run / Hardware /
Diagnostics):

- **EE-ALD** — background fill regulation → dose → beam-with-
  current-check/reignite → pump, per cycle.
- **EE-CVD** — background fill regulation → beam held on for the whole run
  (its own reignite watchdog) with dose+pump-A cycling on top of it; pump A
  is lit-time gated so it locks to the plasma, the dose never is (freezing a
  precursor pulse would dump precursor into the chamber).

Both share a single-overlap gas-scheduling scheme (H2/N2 on/off around the
beam or the cycle) and an operator **pre-start** sequence (Ar on, fill
pulsing, strike-and-hold the plasma with unlimited retries, then ground the
beam) that primes the tool ahead of Start run. Live pressure/current/MFC-
flow/temperature plots each have an independent time window, hover, and
drag-to-zoom; CSV auto-download on run completion.

**Not yet run on real hardware end-to-end** (`reactor-alz`). Open lab item:
tune run parameters (`reactor-2z1`).

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
