# Identifying what is wired where

> **Status (2026-08-06): identification is complete.** All inputs (pressure, 3
> Baratrons, stage TC, bubbler TC) and all 11 valves are mapped and confirmed,
> including the last two loose ends — which precursor Baratron is which, and
> `rpm_top` as the precursor-1 fill valve, both confirmed by Zach 2026-08-06.
> See [HARDWARE.md](HARDWARE.md) and `config/reactor.yaml`. What follows is the
> *method*, kept for any hardware added later.

Every channel was found the same way: **change one thing, look at what moved.**
No guessing from labels, folder names, or documentation that might describe a
different unit.

| Thing | How it was pinned down |
|---|---|
| Stage thermocouple | read open-circuit (2385 °C) before connecting, 21 °C after — only channel that changed |
| Pressure gauge channel | one of eight inputs carried a stable 2.47 V, the rest at the noise floor |
| Gauge curve | matched the controller's own display; offset landed on a round −10 |
| Ar Baratron | ai0 rose 0.3→2.7 V under 5 sccm Ar; the others stayed put |
| Valves | drove each digital-output line and watched the box |

**Inputs are easy and safe to identify. Outputs are not**, and the difference
matters.

---

## Inputs: the three Baratrons

Safe, because reading a channel cannot change anything.

They are on `cDAQ2Mod1` (NI 9201, ±10 V) — the only voltage-input module, and
they plug in next to the cold-cathode gauge. `ai3` is the cold cathode, so they
are three of `ai0`, `ai1`, `ai2`, `ai4`–`ai7`.

The survey could not separate them because a Baratron at base pressure reads
essentially 0 V, same as an unconnected input. Only `ai2` showed anything at all
(0.0197 V, marginally above the ~0.005 V noise floor).

### Procedure

1. Close the LabVIEW VI (it holds every analog-input module).
2. Start the watcher and leave it running:

   ```bash
   .venv\Scripts\python.exe -m tools.watch_channels --threshold 0.02
   ```

3. Make **one** of the three pressures change — backfill a predose volume, or
   isolate one and let it drift up. Anything that moves it clear of zero.
4. The channel it names is that gauge. Repeat per gauge.

`--threshold` is in volts. At 0.02 V a 10 Torr Baratron reports at about
0.02 Torr, so lower it if you are working with small changes.

**How these three were actually settled:** `ai0` gave itself away by rising to
2.72 Torr under 5 sccm Ar while the others stayed put. `ai1` and `ai2` were
separated by their resting offsets — the lower reading is precursor 1, the
higher precursor 2 — confirmed by Zach 2026-08-06.

### Also needed: each head's full-scale range

Printed on the Baratron head — 1, 10, 100, 1000 Torr. These are linear
0–10 V = 0–full scale, so the range is what converts volts to Torr. **A 10 Torr
head read with the 1000 Torr preset is wrong by 100×.** Presets available:
`baratron_0p1torr`, `_1torr`, `_2torr`, `_10torr`, `_100torr`, `_1000torr`.

---

## Outputs: the valves

**Different rules apply.** Identifying an output means energising it, and on this
system that opens a pneumatic valve. There is no read-only equivalent — a DAQmx
digital output line cannot be sensed, only driven. (All 11 valves have since been
identified this way; the method below is kept for anything added later.)

Note: earlier versions of this program refused to actuate an "unidentified"
valve. That refusal — and all other software gating — was **removed** at the
operator's direction (see [CONTROL_MODEL.md](CONTROL_MODEL.md)). `identified` is
now just a descriptive label. The real protection during identification is the
operator's REMOTE/OFF valve positions and the sweep's live STOP.

### What is available

32 digital output lines across three modules:

| Module | Type | Lines |
|---|---|---|
| `cDAQ1Mod3/port1` | NI 9375 | `line0`–`line15` (16) — note `port0` is input-only |
| `cDAQ2Mod2/port0` | NI 9472 | `line0`–`line7` (8), 24 V sourcing |
| `cDAQ2Mod3/port0` | NI 9472 | `line0`–`line7` (8), 24 V sourcing |

Two control boxes, so the natural split is one box per module (or the two 9472s
together). **That is a guess and is not written anywhere in the config.**

### The cheap way: read the wiring

Far better than toggling. Trace which cDAQ module each control box's cable goes
to, and whether the box's outputs map to lines in order. A labelled box (D1, D2,
…) usually does map in order, but confirm it rather than assume.

### The in-app sweep (built for this)

The web UI has a **Valve identification** card. It pulses each output line of a
chosen control-box module in turn — per line, on/off × N, then a gap, then the
next line — while you watch the box. When a valve moves, click its name to bind
it to the line being pulsed. A sticky red **STOP** bar drives every line low the
instant you hit it.

Preconditions:
- valves you want to test in REMOTE position, the rest OFF (a line pulsed to an
  OFF valve does nothing — this is the physical protection during identification)
- LabVIEW closed (it holds the DAQ)
- outputs armed

The marks you make are written into `config/reactor.yaml` afterward. Only lines
you actually saw move get `identified: true`.

### The careful way: supervised toggling (command line)

Only if the wiring cannot be read. Conditions first:

- the chamber in a state where an unexpected valve opening is harmless — no
  precursor, gas supplies isolated at the bottle or manual valve
- someone watching and listening at the box
- one line at a time, briefly, with the next line only after the previous is
  confirmed

There is a helper for this. It is deliberately **not** wired into the main
program, requires an explicit line argument, prints what it is about to do, and
requires typed confirmation:

```bash
.venv\Scripts\python.exe -m tools.pulse_line cDAQ2Mod2/port0/line0
```

Do not run it casually.

### RULED OUT: reading valve state back from the DAQ

The attractive idea was to identify valves with nothing energised from
software: `cDAQ1Mod3/port0` has 16 digital **inputs**, so if either control box
wired position or status feedback back to the DAQ, flipping a valve by hand at
the box would show up there. `tools/watch_channels.py` was written to catch
exactly that — it baselines every input and prints a line whenever one moves.

It does not work here. Tested 2026-07-30 with several valves physically ON (Ar
MFC to remote, a precursor valve, plasma ground): **all 16 digital inputs low,
and every spare analog input at its base-pressure baseline.** Nothing on the
DAQ reflects valve state.

Conclusion: there is no valve-position feedback wired to this DAQ. The DAQ only
*commands* the outputs and reads nothing back, so every remaining method needs
someone physically at the box. This is also why the program persists
last-commanded valve state to `config/valve_state.json` rather than reading it
(see [CONTROL_MODEL.md](CONTROL_MODEL.md)).

`watch_channels` is still the right tool for **inputs** — it is how the
Baratrons were separated above.

---

## Recording what you find

In `config/reactor.yaml`, per valve — set the `line`, give it a real `label`, and
mark `identified: true` once you've seen it move:

```yaml
  - id: "prec1"
    label: "Precursor 1 micro pulse valve"
    bank: "upper"
    line: "cDAQ1Mod3/port1/line0"
    identified: true                # a descriptive note that you confirmed it
```

`identified` is now just a label recording that a human confirmed the line's
destination; it does not gate actuation (all software gating was removed — see
[CONTROL_MODEL.md](CONTROL_MODEL.md)). The value of setting it honestly is
documentation: a future reader trusts a valve marked identified.
