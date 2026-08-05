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
server over a WebSocket at the loop rate (2 Hz).

Panels: chamber pressure, the 3 Baratrons, stage temperature, the DMM, the 3
MFCs (live flow + settable flow), the 11 valves (grouped by control box, each
individually actuable), the **ALD + e-beam run** panel, data logging, the
**Run monitor** (two live plots), a valve-identification sweep tool, a
connections table, and an event log.

### The ALD run panel + Run monitor (the main workflow)

Everything is editable **in the interface** — no YAML/code editing for normal use:
- Set cycles, dose pressure, dose time, pump A, beam exposure, pump B, min
  current, and fill-pulse params. They persist in the browser (localStorage).
- **Start run** builds and launches the run (`POST /api/run/ald`).
- A **phase strip** (Dose · Pump A · E-beam · Pump B) highlights the active
  phase with a live countdown; the beam shows *exposure remaining* (which pauses
  during a reignite).
- **Run monitor**: stacked live plots — chamber + precursor pressure (log) on
  top, sample current on the bottom, with **plasma-relay flips overlaid** on the
  current trace (dashed grey = scheduled, solid red = reignite). Drag to pan,
  wheel to zoom, "Follow live" to re-attach.
- When a run ends, a **CSV auto-downloads** (elapsed time zeroed to Start, stage
  temp, sample current, precursor dosing, precursor pressure, chamber pressure).

How the run actually behaves is documented in **[docs/RUN_PROGRAM.md](docs/RUN_PROGRAM.md)**.

---

## Architecture

```
config/reactor.yaml         every hardware address, channel, gauge curve — the ONLY hardware map
config/recipes/*.yaml       file-based recipes (the ALD run is built from UI params instead)
reactor/
  config.py                 validates the YAML; errors name the offending key
  supervisor.py             THE single owner of state + the only path to hardware.
                            Control loop, MFC/valve commands, fill-pressure regulator,
                            valve-ID sweep, telemetry fan-out. Read this to know what the
                            program can do.
  datalog.py                tab-delimited run logs (LabVIEW-compatible columns)
  devices/
    base.py                 Reading + Device base (no gates, no interlocks)
    nidaq.py                NI-DAQmx: analog in, analog out, digital out, raw-line pulsing
    mks_mfc.py              MKS G50: reads over the device's HTTP interface, writes setpoint over Modbus
    instrument.py           SCPI over VISA (the DMM6500)
  control/
    recipe.py               recipe engine + step types (dose/wait/electron_beam/start_fill/...)
                            + build_ald_recipe() for the UI-driven run
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

## Current state (2026-08-01)

**Working and verified against real hardware:** all inputs (pressure, 3
Baratrons, stage TC + 2 more TCs, DMM current), all 3 MFCs (read + write, flow
holds while the program stays connected), all 11 valves (individually actuable).
The ALD run engine is built and its control logic verified with a fake-DAQ
harness — **but a full ALD run has not yet been executed on real hardware.**

**Editable entirely in the UI:** MFC setpoints, every valve, and all ALD run
parameters.

**Known open items:**
- The two precursor-dose Baratrons (ai1, ai2) are labelled by the rule "lower
  reading = precursor 1, higher = precursor 2" — to verify in the lab.
- The precursor-1 fill valve is assumed to be `rpm_top` (right manifold top);
  switch to `rpm_bottom` if reversed.
- The NI 9265 current-output module's purpose is unknown (deferred).
- ALD run parameters (dose pressure, all durations, cycles, 500 µA current
  threshold) are starting values to tune in the lab.

---

## Common changes

| Want to… | Do this |
|---|---|
| change MFC flow | set it on the MFC card in the UI |
| open/close a valve | its Open/Close button in the UI (grouped by control box) |
| change any ALD run parameter | the ALD run panel fields (persist automatically) |
| rename a Baratron / thermocouple once identified | edit its `label` in `config/reactor.yaml` |
| fix the gauge curve or a channel | `config/reactor.yaml` (see docs/HARDWARE.md) |
| add a driver for new equipment | a class in `reactor/devices/` returning `Reading`s; the UI/logger pick it up |

---

## What this program will and will not do to the reactor

**[docs/CONTROL_MODEL.md](docs/CONTROL_MODEL.md)** is the complete answer. Summary:
commands execute exactly as given. There are **no** software interlocks, limits,
clamps, or automatic actions. The only guard is the operator-requested gentle
"flag" when precursor fill pressure drifts >20% off setpoint — it warns, it does
not stop. This is deliberate: Zach is the sole arbiter of reactor behavior.
