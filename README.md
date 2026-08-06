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

- **Cold-cathode chamber gauge** (base ~6e-8 Torr) + **3 Baratrons** (10 Torr heads)
- **Sample thermocouple** + two more thermocouples
- **Keithley DMM6500** measuring sample current (the plasma/e-beam diagnostic)
- **3 MKS G50 mass flow controllers** (Ar, H2, N2) over Modbus TCP
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
  pressure gauges, other inputs, instruments, and primary-sensor detail.
- **Diagnostics** — a valve-identification sweep tool, data logging, a
  connections table, and the event log (a pinned header chip surfaces the
  newest warning regardless of which tab is open).

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

How the run actually behaves is documented in **[docs/RUN_PROGRAM.md](docs/RUN_PROGRAM.md)**.

---

## Architecture

```
config/reactor.yaml         every hardware address, channel, gauge curve — the ONLY hardware map
config/recipes/*.yaml       file-based recipes (EE-ALD/EE-CVD are built from UI params instead)
config/labels.json          operator-set display-name overrides for valves/MFCs/gauges (blank = default)
config/valve_state.json     last-commanded valve state, restored into the in-memory model on
                            startup (never written to hardware on restore — connect stays read-only)
reactor/
  config.py                 validates the YAML; errors name the offending key
  supervisor.py             THE single owner of state + the only path to hardware.
                            Control loop, MFC/valve commands, fill-pressure regulator,
                            pre-start sequence, valve-ID sweep, telemetry fan-out. Read this to
                            know what the program can do.
  datalog.py                tab-delimited run logs (LabVIEW-compatible columns)
  devices/
    base.py                 Reading + Device base (no gates, no interlocks)
    nidaq.py                NI-DAQmx: analog in, analog out, digital out, raw-line pulsing.
                            One DAQmx task PER LINE for digital out, so a write to one valve
                            can never re-drive (and silently flip) a sibling on the same module.
    mks_mfc.py               MKS G50: reads over the device's HTTP interface, writes setpoint over Modbus
    instrument.py           SCPI over VISA (the DMM6500)
  control/
    recipe.py               recipe engine + step types (dose/wait/electron_beam/beam_start/
                            beam_stop/start_fill/...) + build_ald_recipe() and build_cvd_recipe()
                            for the two UI-driven run modes
  server/
    app.py                  FastAPI HTTP + WebSocket; thin wrapper over Supervisor methods
    static/index.html       the entire GUI
tools/
  discover_hardware.py      read-only enumeration: DAQ, VISA, serial, Modbus, gauge-curve solver, DMM status
  watch_channels.py         read-only: watch all inputs, report what changes (channel identification)
  pulse_line.py             drive ONE digital output line (valve identification), with confirmation
docs/                       HARDWARE.md, RUN_PROGRAM.md, CONTROL_MODEL.md, IDENTIFYING_HARDWARE.md, LABVIEW_ANALYSIS.md
```

The shape that matters: **`Supervisor` is the only thing that can move hardware.**
The web layer calls its methods and nothing else.

---

## Current state (2026-08-06)

**Working and verified against real hardware:** all inputs (pressure, 3
Baratrons, stage TC, bubbler TC + 2 more TCs, DMM current), all 3 MFCs (read +
write, flow holds while the program stays connected), all 11 valves
(individually actuable, each on its own DAQmx line so one write can't flip a
sibling), valve state persisted across restarts, editable display labels.

**Built and logic-verified with fake-DAQ / fake-supervisor harnesses, not yet
run against real hardware end-to-end** (tracked in `reactor-alz`): the
**EE-ALD** run (pulsed beam, one exposure per cycle) and **EE-CVD** run
(continuous beam, dosing on top of it), the operator **pre-start** sequence,
gas scheduling (single overlap field, freezes with the plasma), and the
±20% fill-pressure flag.

**Editable entirely in the UI:** MFC setpoints, every valve, run mode and all
its parameters, and every valve/MFC/gauge display name.

**Known open items:**
- The two precursor-dose Baratrons (ai1, ai2) are labelled by the rule "lower
  reading = precursor 1, higher = precursor 2" — to verify in the lab
  (`reactor-gkw`).
- The precursor-1 fill valve is assumed to be `rpm_top` (right manifold top);
  switch to `rpm_bottom` if reversed (`reactor-zo0`).
- The NI 9265 current-output module's purpose is unknown, deferred
  (`reactor-5u2`).
- Run data export is client-side only (a closed browser loses the CSV);
  server-side export is tracked (`reactor-f3j`).
- Run parameters (dose pressure, all durations, cycles, 500 µA current
  threshold) are starting values to tune in the lab (`reactor-2z1`).

Full history of what's shipped and what's still open lives in the **bd**
issue tracker (`bd list --status=closed` / `bd ready`), not just in this file.

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
