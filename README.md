# Reactor Interface

A Python + browser control interface for Zach's **UHV electron-beam ALD reactor**,
replacing the old, buggy LabVIEW program (`LV Prog Main Zach.vi`). Everything a
non-programmer needs to change lives in the web UI or in one YAML file; the code
underneath is plain, diffable, and greppable.

> **Resuming in a new chat? Read this whole file, then
> [docs/HARDWARE.md](docs/HARDWARE.md) and [docs/RUN_PROGRAM.md](docs/RUN_PROGRAM.md).**
> Project memory belongs in Beads (`bd remember`), not ad hoc memory files.
> [docs/CONTROL_MODEL.md](docs/CONTROL_MODEL.md) describes the operator-requested
> interlock, flags, sequences and cleanup. **Zach is the sole arbiter of reactor
> behavior — do not add automatic hardware actions without his approval.**

---

## What this reactor is

A ultra-high-vacuum chamber for electron-beam-assisted ALD. Confirmed by querying
the hardware (not by trusting the old VI, which is full of dead code):

- **Cold-cathode chamber gauge** (base ~3e-8 Torr) + **3 Baratrons** (10 Torr heads)
- **Sample thermocouple**, a precursor-bubbler thermocouple, + two more
- **Keithley DMM6500** measuring sample current (the plasma/e-beam diagnostic)
- **3 MKS G50 mass flow controllers** (Ar, H2, N2) — flow, temperature and
  setpoint all over Modbus; HTTP only for full scale and identity
- **Film Sense FS-1 in-situ ellipsometer**, read-only over its live TCP stream
- **XP Glassman FL1.5F1.0 high-voltage plasma supply** (1500 V / 1.0 A) on USB,
  monitor-only apart from a commanded **HV off** at the end of a run or on an
  abort — its voltage, current and arc count are logged; it is set by hand
- **4 Keithley 2260B DC supplies** — stage bias, steering coils, grid bias,
  collimating coils. Logged, and their outputs switched on at pre-start
- **Pneumatic valves** across two control boxes, incl. precursor manifolds, a
  micro-pulse dose valve, forelines, a gate valve, and a plasma-ground relay
- **NI CompactDAQ** (two cDAQ-9174 chassis) for all analog/digital I/O

Full channel-by-channel map: **[docs/HARDWARE.md](docs/HARDWARE.md)**.

## Starting it

Desktop shortcut **Reactor Interface** — pinnable to the taskbar. It launches
`pythonw.exe -m reactor --port 8000 --open`, so there is **no console window**,
and opens the UI in the browser.

Because there is no console, everything the console would have printed goes to
`server.log` in the project root instead — startup, device connections, and any
reason the server failed to come up. That file is the only record a windowless
launch has; check it first if clicking the shortcut appears to do nothing.

To watch it live instead, run it with a console:

```bash
.venv\Scripts\python.exe -m reactor --port 8000
```

To pick up a code change, use **Diagnostics → Shut down server** (it also kills
any other reactor server still running, so nothing is left holding the DAQ or
the serial ports) and start it again from the shortcut.

---

## Run it

Python 3.12 in a venv (already set up in `.venv/`).

```bash
python -m reactor
```

Opens `http://127.0.0.1:8000/`. **Open that in a real browser (Chrome/Edge)** on
the reactor PC — the embedded preview panes don't composite scroll correctly.

```bash
python -m reactor --check      # validate config and print the I/O summary, no serving
python -m reactor --open       # also open a browser
```

Localhost needs no login. Before exposing the server beyond it (Tailscale/LAN),
set `REACTOR_PASSWORD` (and optionally `REACTOR_USER`, default `reactor`) in the
environment — that switches on HTTP Basic auth across pages, API *and* the
telemetry WebSocket. It is a login, not encryption: run it over the VPN or LAN,
where the transport is already private. With no `REACTOR_PASSWORD` set, startup
logs a warning and the server is open.

### The one hard constraint: LabVIEW vs. this program

NI-DAQmx gives **one** program exclusive use of a module's analog input. **Close
the LabVIEW VI before running this** or it reports "resource reserved" on the DAQ
channels. The MFCs (network devices) and the DMM (USB) are unaffected.

---

## The GUI

The control page is [index.html](reactor/server/static/index.html), with
`control.css`, `control.js`, and the `live-charts.js` ES module. It uses vanilla
JavaScript with no build step or external libraries. It talks to the
server over a WebSocket — DAQ-bound readings (pressure, thermocouples) publish
at `site.loop_hz` (2 Hz default), while sample current and MFC flow publish at
the faster `site.current_hz` (5 Hz default) since they have no DAQ coupling.

Three tabs:

- **Run** — hero numeric readouts (chamber pressure, stage temp, precursor
  fill pressure, bubbler temp, sample current), the **EE-ALD / EE-CVD run
  panel** with collapsible gas scheduling / advanced timing / pre-start
  panels, and three independent live plots (run monitor, MFC flow,
  temperatures) each with its own time window, hover readout, and
  drag-to-zoom.
- **Hardware** — valves (grouped by control box, each individually actuable
  and renameable), the 3 MFCs (live flow + settable flow, renameable), other
  pressure gauges, other inputs, instruments, Keithley power-supply voltage/current
  controls and output toggles, and the Glassman monitor (HV off at run end), the **ellipsometer** readout (stream
  state, points banked this acquisition, live fit), and primary-sensor detail.
- **Diagnostics** — a valve-identification sweep tool, data logging, a
  connections table (every device, the FS-1 included), and the event log. A
  header chip shows any condition that is wrong **right now** — fill pressure
  off setpoint, a dead plasma, a disconnected device — and clears itself the
  moment the condition does; the log is the history. Post-run ellipsometer sync
  lives on the Analysis page, next to the plots it feeds.

Plus a separate **[Analysis page](reactor/server/static/analysis.html)** at
`/analysis` for post-run plotting — a persistent grid of
property-vs-cycle plots over the run CSVs and the ellipsometry-merged file,
and a drop box for **Auger (AES) spectra**, each of which gets its own
autoscaled plot alongside them.
It reads finished files and can touch no hardware, which is why it is its own
page rather than a fourth tab: it can be left open beside a running experiment.
Details in [docs/RUN_PROGRAM.md](docs/RUN_PROGRAM.md).

### The run panel + plots (the main workflow)

Everything is editable **in the interface** — no YAML/code editing for normal use:
- Choose **EE-ALD** (pulsed beam, fixed exposure per cycle) or **EE-CVD**
  (continuous beam, dosing on top of it) from the mode selector. Set cycles,
  dose pressure, dose time, pump A, (EE-ALD: beam exposure, pump B), min
  current, gas scheduling, and advanced timing. They persist on the server in `config/run_params.json`; each browser keeps a
  localStorage cache.
- Optional **Pre-start**: opens the Ar isolation valve, flows Ar, starts the
  precursor fill pulse, strikes and holds the plasma (retries indefinitely
  until stopped), then grounds the beam — priming the tool before Start run.
- **Start run** builds and launches the run (`POST /api/run/ald` or
  `/api/run/cvd`).
- A **phase strip** highlights the active phase with a live countdown; the
  beam shows exposure remaining (EE-ALD) or lit-time banked (EE-CVD), both
  pausing during a reignite.
- **Run monitor**: stacked live plots — chamber pressure (log, optionally
  smoothed for display only) on top, sample current (auto-ranging µA/mA) on
  the bottom, with **plasma-relay flips overlaid** on the current trace
  (dashed grey = scheduled, solid red = reignite). Drag to zoom, arrow keys
  to pan/zoom while hovering, hover for a value readout, "Follow live" to
  re-attach.
- When a run ends, a **CSV auto-downloads** (elapsed time zeroed to Start, stage
  temp, sample current, precursor dosing, precursor pressure, chamber pressure).
  The server independently writes its own, richer copy the moment any run
  starts and closes it however the run ends — so a closed browser no longer
  loses the trace.

`_run.csv` is the **raw** trace (every tick, reignites and pauses included, each
named in `recipe_step`); `_bycycle.csv` is the same minus anything frozen, keyed
by cycle; the merged file is that plus ellipsometry.

**Every file a run writes goes in one folder, named for the run:**

```
data/Mo-015/
    Mo-015_260821_131320_run.csv              trace, by time
    Mo-015_260821_131320_bycycle.csv          trace, by fractional cycle
    Mo-015_260821_131320_run_params.txt       the settings, in plain text
    Mo-015_260821_131256_ellipsometer.csv     FS-1 sidecar for this run
    Mo-015_260821_131320_reactor_synced.csv   the post-run merge, once made
```

An unnamed run gets a folder named for its timestamp instead. The parameters
file is plain text, not JSON, and opens in Notepad.

How the run actually behaves is documented in **[docs/RUN_PROGRAM.md](docs/RUN_PROGRAM.md)**.

---

## Architecture

A single Supervisor owns hardware. Recipe/pre-start controllers use its command
methods, telemetry projects its state, and recording writes through a dedicated
worker. Analysis file work runs off the event loop. Read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ownership, timing, startup,
shutdown, persistence and failure behavior.

```
config/reactor.yaml           hardware addresses, channels and gauge scaling
config/recipes/*.yaml         optional file-based recipes
config/{labels,valve_state,last_run,run_params}.json  operator metadata/settings
reactor/
  __main__.py                 CLI and single-process Uvicorn lifecycle
  config.py                   validated hardware/configuration models
  supervisor.py               hardware ownership, polling, commands and run admission
  telemetry.py                stable snapshots and bounded WebSocket fan-out
  recording.py                ordered file work on a dedicated recording thread
  datalog.py                  manual/run/by-cycle CSVs and ellipsometer sidecars
  run_report.py               pure formatting of readable run parameters
  control/
    recipe_model.py           recipe schema and pure ALD/CVD builders
    recipe.py                 recipe execution, gas schedules and exposure clocks
    prestart.py               pre-start sequence, stop and abort lifecycle
  devices/
    base.py                   Reading and device lifecycle contract
    nidaq.py                  DAQ inputs; one output task per valve line
    mks_mfc.py                MKS G50 Modbus measurements/setpoints; HTTP metadata/fallback
    instrument.py             VISA/SCPI instruments such as the DMM6500
    glassman_fl.py             Glassman monitoring; application commands HV off only
    keithley_2260b.py          Keithley monitoring, manual controls and requested run outputs
    ellipsometer.py            read-only FS-1 TCP stream and decoder
  analysis/ellipsometer_merge.py  pure refit/sidecar clock mapping and merge
  server/
    app.py                    HTTP control, lifecycle, authentication and pages
    data.py                   analysis/file routes, with worker-based file operations
    static/index.html         control page markup
    static/control.css        control-page styles
    static/control.js         controls, parameters and telemetry rendering
    static/live-charts.js     live plotting and chart interaction
    static/analysis.html      analysis page markup
    static/analysis.css       analysis-page styles
    static/analysis.js        finished-run and Auger analysis
  testing/virtual_reactor.py   fake hardware, real application/controllers, temporary files
tools/
  discover_hardware.py         read-only hardware enumeration and identification
  probe_glassman.py            read-only HV supply port/baud/address probe
  watch_channels.py           input observation for channel identification
  pulse_line.py               one-line actuation tool, with confirmation
tests/                        python -m tests.run_all
```

Rejected duplicate starts leave the active run's metadata unchanged. Disk write
failures and recording-backlog overflow appear in the alert chip, telemetry and
event log; they do not stop the experiment. Errors remain visible for the server
session because a subsequent successful row cannot restore lost data. Run files
are drained on completion and shutdown.

---

## Current state (2026-08-11)

**Working and verified against real hardware:** all inputs (pressure, 3
Baratrons, stage TC, bubbler TC + 2 more TCs, DMM current), all 3 MFCs (read +
write, flow holds while the program stays connected), all 11 valves
(individually actuable, each on its own DAQmx line so one write can't flip a
sibling), valve state persisted across restarts, editable display labels, and
the FS-1 ellipsometer stream (connects and captures sidecars).

**EE-ALD** (pulsed beam, one exposure per cycle) has now been **run and
tuned on real hardware** (`reactor-alz`, `reactor-2z1`, confirmed
2026-08-06).

**Logic-verified against the virtual reactor (`tests/`), not yet confirmed on
real hardware:** the **EE-CVD** run (continuous beam, dosing on top of it), the
operator **pre-start** sequence, gas scheduling (single overlap field, freezes
with the plasma), and the ±20% fill-pressure flag. Those tests run the real
`Supervisor` and recipe engine against fake devices — they prove sequencing and
reaction, never anything about the physical reactor. See
[tests/README.md](tests/README.md) for exactly where that line falls.

**Editable entirely in the UI:** MFC setpoints, every valve, run mode and all
its parameters, and every valve/MFC/gauge display name.

**Known open items** (`bd ready` for the live list):
- A P1 bug under investigation: TC readings shift, correlated with ~1.5 mA
  sample current with no beam (`reactor-7n0`).
- **A beam step now delivers exactly the exposure you ask for.** It used to run
  `reignite_settle_s` longer (10.2 s for a 10 s step), so runs before and after
  2026-08-21 are not directly comparable at the same `beam_s` — `reactor-a3r`
  has the detail and the conversion.
- EE-CVD cycle timing has not been audited the way EE-ALD was (`reactor-2ou`).
- Remote *setpoint* control of the Glassman is deliberately not built; only
  HV-off is wired (see docs/CONTROL_MODEL.md). HV-off at run end is confirmed
  working on hardware.
- The NI 9265 current-output module's purpose is unknown, deferred
  (`reactor-5u2`, low priority — not needed for normal operation).

Full history of what's shipped and what's still open lives in the **bd**
issue tracker (`bd list --status=closed` / `bd ready`), not just in this file.

### The issue tracker is backed up (set up 2026-08-21)

The **bd** issues live in a Dolt database under `.beads/embeddeddolt/`, which is
gitignored — so until now the tracker existed on this machine only, and a disk
failure would have taken the whole project history with it. It now syncs to the
same GitHub repo as the code, under a separate ref:

```bash
bd dolt push      # upload the issue history to refs/dolt/data
bd dolt pull      # bring down changes made elsewhere
```

The remote is `git+https://github.com/zsobell/reactor-interface.git` — the same
URL git already uses, so it needs no new account, no SSH key and no separate
service. Dolt keeps its data in `refs/dolt/data`, well clear of `refs/heads/`,
so it cannot collide with the source history.

**To restore it on another machine** (verified end to end, 2026-08-21 — a fresh
clone came back with every issue, note and closure intact): clone the repo, then
run `bd bootstrap` inside it. Bootstrap notices `refs/dolt/data` on the origin
remote by itself and clones the database instead of starting an empty one; the
remote URL does not have to be committed anywhere for that to work.

Two things worth knowing:

- **Clone somewhere with a short path on Windows.** Dolt's remote-cache
  directory is long and the hashes on the end are 64 characters, so a deep clone
  path blows past the 260-character limit and `bd bootstrap` fails with
  `Filename too long`. It is not an auth or config problem. `C:\Users\<you>\repo` is fine;
  a temp directory several levels down is not.
- **A `__dolt_remote_info__` branch appears** in the repo's branch list on
  GitHub. That is Dolt's own bookkeeping, not a stray branch of yours; leave it
  alone.

---

## Common changes

| Want to… | Do this |
|---|---|
| change MFC flow | set it on the MFC card in the UI (Hardware tab) |
| open/close a valve | its Open/Close button in the UI (Hardware tab, grouped by control box) |
| change any run parameter | the run panel fields on the Run tab (persist automatically) |
| rename a valve / MFC / Baratron | its ✎ button in the UI (persists to `config/labels.json`; blank reverts to the `reactor.yaml` default) |
| fix the gauge curve or a channel | `config/reactor.yaml` (see docs/HARDWARE.md) |
| add a driver for new equipment | a class in `reactor/devices/` returning `Reading`s; the UI/logger pick it up |

---

## What this program will and will not do to the reactor

**[docs/CONTROL_MODEL.md](docs/CONTROL_MODEL.md)** is the complete answer. Summary:
commands execute exactly as given. There are **no** software interlocks, limits,
clamps, or automatic actions, except two the operator explicitly asked for: a
gentle "flag" when precursor fill pressure drifts >20% off setpoint (warns,
never stops), and the Ar MFC refusing a nonzero setpoint while its isolation
valve is closed. This is deliberate: Zach is the sole arbiter of reactor
behavior.
