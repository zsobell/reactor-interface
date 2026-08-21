# Reactor Interface

A Python + browser control interface for Zach's **UHV electron-beam ALD reactor**,
replacing the old, buggy LabVIEW program (`LV Prog Main Zach.vi`). Everything a
non-programmer needs to change lives in the web UI or in one YAML file; the code
underneath is plain, diffable, and greppable.

> **Resuming in a new chat? Read this whole file, then
> [docs/HARDWARE.md](docs/HARDWARE.md) and [docs/RUN_PROGRAM.md](docs/RUN_PROGRAM.md).**
> Also load the memory files (they capture the working relationship and hard-won
> facts). The single most important standing rule is in
> [docs/CONTROL_MODEL.md](docs/CONTROL_MODEL.md): **there are no software
> interlocks; Zach is the sole arbiter of reactor behavior — never add a safety
> feature without asking him first.**

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
- **Pneumatic valves** across two control boxes, incl. precursor manifolds, a
  micro-pulse dose valve, forelines, a gate valve, and a plasma-ground relay
- **NI CompactDAQ** (two cDAQ-9174 chassis) for all analog/digital I/O

Full channel-by-channel map: **[docs/HARDWARE.md](docs/HARDWARE.md)**.

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

One self-contained file: [reactor/server/static/index.html](reactor/server/static/index.html)
(HTML + CSS + vanilla JS, no build step, no external libraries). It talks to the
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
  pressure gauges, other inputs, instruments, power supplies (no setpoints — the
  one command sent is HV off at run end), the **ellipsometer** readout (stream
  state, points banked this acquisition, live fit), and primary-sensor detail.
- **Diagnostics** — a valve-identification sweep tool, data logging, a
  connections table (every device, the FS-1 included), and the event log. A
  header chip shows any condition that is wrong **right now** — fill pressure
  off setpoint, a dead plasma, a disconnected device — and clears itself the
  moment the condition does; the log is the history. Post-run ellipsometer sync
  lives on the Analysis page, next to the plots it feeds.

Plus a separate **[Analysis page](reactor/server/static/analysis.html)** at
`/analysis` (prototype) for post-run plotting — a persistent grid of
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
  current, gas scheduling, and advanced timing. They persist in the browser
  (localStorage).
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

```
config/reactor.yaml         every hardware address, channel, gauge curve — the ONLY hardware map
config/recipes/*.yaml       file-based recipes (EE-ALD/EE-CVD are built from UI params instead)
config/labels.json          operator-set display-name overrides for valves/MFCs/gauges (blank = default)
config/valve_state.json     last-commanded valve state, restored into the in-memory model on
                            startup (never written to hardware on restore — connect stays read-only)
config/last_run.json        name of the last run that actually started, so the Run tab can
                            pre-fill the next one incremented (Mo-014 -> Mo-015)
reactor/
  __main__.py               the CLI entry point: `python -m reactor` serves, `--check` validates
                            the config and prints the I/O summary without serving
  config.py                 validates the YAML; errors name the offending key
  supervisor.py             THE single owner of state + the only path to hardware.
                            Control loop, MFC/valve commands, fill-pressure regulator,
                            pre-start sequence, valve-ID sweep, telemetry fan-out. Read this to
                            know what the program can do.
  datalog.py                tab-delimited run logs (LabVIEW-compatible columns), the automatic
                            per-run CSV + by-cycle export, and the ellipsometer sidecar
  devices/
    base.py                 Reading + Device base (no gates, no interlocks)
    nidaq.py                NI-DAQmx: analog in, digital out, raw-line pulsing. One DAQmx task
                            PER LINE for digital out, so a write to one valve can never
                            re-drive (and silently flip) a sibling on the same module.
                            No analog-output path on purpose - see docs/HARDWARE.md.
    mks_mfc.py              MKS G50: flow/temperature/valve/setpoint over Modbus (register map from
                            the device's own /modbus.html), HTTP only for full scale + identity
    instrument.py           SCPI over VISA (the DMM6500)
    glassman_fl.py          XP Glassman FL HV plasma supply, serial. Polls V/I/arc count; the ONE
                            command sent is hv_off() at run end / abort. No setpoints, no HV on
                            (docs/GLASSMAN_FL.md)
    ellipsometer.py         Film Sense FS-1 live stream: read-only TCP subscriber + record decoder
  control/
    recipe.py               recipe engine + step types (dose/wait/electron_beam/beam_start/
                            beam_stop/start_fill/...) + build_ald_recipe() and build_cvd_recipe()
                            for the two UI-driven run modes
  analysis/
    ellipsometer_merge.py   post-run join: a refit FS-1 file + the live sidecar -> one
                            plot-ready CSV keyed by cycle number (pure text munging, no I/O)
  server/
    app.py                  FastAPI HTTP + WebSocket; thin wrapper over Supervisor methods
    static/index.html       the entire control GUI
    static/analysis.html    the post-run plotting page (/analysis) - reads CSVs and dropped
                            Auger spectra, no hardware
  testing/virtual_reactor.py  fake DAQ/MFC/instrument devices, real Supervisor on top -
                            see tests/README.md
tools/
  discover_hardware.py      read-only enumeration: DAQ, VISA, serial, Modbus, gauge-curve solver, DMM status
  probe_glassman.py         read-only: finds the HV supply's COM port, baud rate and address
  watch_channels.py         read-only: watch all inputs, report what changes (channel identification)
  pulse_line.py             drive ONE digital output line (valve identification), with confirmation
tests/                      control-logic tests against the virtual reactor; python -m tests.run_all
docs/                       HARDWARE.md, RUN_PROGRAM.md, CONTROL_MODEL.md, IDENTIFYING_HARDWARE.md,
                            LABVIEW_ANALYSIS.md, GLASSMAN_FL.md
```

The shape that matters: **`Supervisor` is the only thing that can move hardware.**
The web layer calls its methods and nothing else.

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
  HV-off is wired (`reactor-tp1`, and docs/CONTROL_MODEL.md).
- The FS-1 ellipsometer sync is not yet validated across a real deposition
  (`reactor-nde`).
- Whether stopping the server should ground the beam during a pre-start is
  undecided (`reactor-4h9`).
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
