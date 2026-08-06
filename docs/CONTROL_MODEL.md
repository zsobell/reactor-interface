# What this program does to the reactor

The complete, honest answer. Read it before assuming the software will protect
anything.

## There are no software interlocks

Commands execute **exactly as given**. There is no chamber-pressure interlock, no
MFC setpoint clamp, no watchdog that closes valves, no arm/disarm gate, no
refusal to actuate, no auto-close on shutdown. A setpoint is written as typed; a
valve opens the instant it's told; nothing acts on its own.

This is deliberate and was done at the operator's explicit direction. **Zach is
the sole arbiter of reactor behavior.** An earlier version of this program had a
pile of unrequested safety machinery built on assumptions about a reactor the
author did not understand; it blocked legitimate commands and destroyed trust. It
was all removed on 2026-07-31.

**Do not add any interlock, limit, or automatic action without Zach's explicit
say-so.** If you think one is warranted, propose it and wait for a yes. (See the
`no-unrequested-safety-features` memory.)

## The guards that exist — because they were requested

Two, both explicitly requested by Zach, both narrow:

1. **Fill-pressure flag.** During a run (EE-ALD or EE-CVD), if the precursor
   fill pressure drifts more than ±20% off its setpoint (a run parameter,
   default), the program emits a gentle **"flag"** event (amber in the event
   log, plus an OUT OF BOUNDS marker on the run panel and the fill-pressure
   hero readout). It **does not stop the run.**
2. **Ar MFC / `ar_pneumatic` isolation interlock.** The Ar MFC's setpoint
   cannot be raised above 0 sccm while its isolation valve (`ar_pneumatic`)
   is closed, and closing that valve zeroes the MFC's setpoint. This is
   configured per-MFC (`isolation_valve` in `config/reactor.yaml`, currently
   only on `ar`) and enforced in `Supervisor.set_mfc_setpoint` /
   `set_valve`. The UI disables the Ar tile's Set Flow button and shows why
   while the valve is closed.

Both exist **only** because the operator asked for them directly, and both
are narrow — a flag that never stops anything, and a single valve/MFC pairing
that refuses one specific invalid combination rather than gating anything
reactor-wide. Neither is a precedent for adding more without asking first.

## What still protects the hardware (not this program)

- **The operator.** Every consequential action is a human decision in the UI.
- **The MFC's own watchdog.** MKS G50 units zero their setpoint when the Modbus
  master disconnects, so commanded flow only holds while the program is
  connected. (Not something this program does — a device feature to know about.)
- **The valves' physical positions.** Pneumatic valves have remote/off/manual
  positions; a valve in "off" won't actuate on a software command.
- **Whatever hardware interlocks the reactor itself has.** Keep them.

## Config-validation checks (not reactor limits)

`config/reactor.yaml` is validated on load for typos: duplicate ids, a valve
pointing at a nonexistent control box, two valves sharing one DAQ line. These
prevent a broken *config*; they do not constrain reactor operation.

## Things worth knowing when driving hardware

- **Connecting is read-only.** Opening a device session (MFC, DMM, DAQ inputs)
  never writes a setpoint or moves a valve. DAQmx output tasks are created lazily
  on first write, so startup can't twitch an output.
- **Valve position after a restart is not reliably "closed."** This used to say
  closing DAQmx output tasks resets lines low on task close - that was an
  assumption, and it was contradicted 2026-08: the Ar pneumatic isolation valve
  stayed physically open across a server restart. The program never commands a
  reset on stop; whatever the line does on task close is DAQ hardware
  behaviour, and it should not be assumed to go low. Because there is no
  valve-position feedback (see IDENTIFYING_HARDWARE.md), the program now
  persists the last-commanded state per valve (`config/valve_state.json`) and
  restores it into its internal model at startup - this is a best-effort
  record of what was last commanded, not a hardware-confirmed reading, and it
  never writes to hardware on restore (connecting stays read-only).
- **SCPI instruments go to remote mode when talked to.** The DMM6500 stops
  free-running under remote control, so its display freezes on the last reading;
  the program sends a "go to local" (USBTMC GTL) on disconnect. If the program is
  killed rather than closed cleanly, press EXIT on the DMM front panel.
- **The valve-ID sweep has a live STOP** (operator-requested) that drives every
  swept line low. **Pre-start** (see docs/RUN_PROGRAM.md) has the same shape: its
  plasma-strike step retries **indefinitely, with no timeout or attempt
  limit**, by explicit instruction — the only way to stop it is the operator
  pressing Stop pre-start. These are the emergency/manual-stop controls in the
  program; nothing else auto-stops.
- **A digital-output write only ever touches its own line.** Early on, writing
  one valve re-drove its whole DAQ module from an in-memory vector, which could
  silently close a *different* valve sharing that module (e.g. starting a run
  closed the Ar isolation valve). Fixed 2026-08-03 by giving every valve its
  own single-line DAQmx task. Worth remembering if a future change touches
  `reactor/devices/nidaq.py`: never go back to one task per module for DO.
