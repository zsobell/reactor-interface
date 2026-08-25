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

## The DC supply outputs (requested 2026-08-21)

The four Keithley 2260B supplies — stage bias, steering coils, grid bias,
collimating coils — are the one place this program switches a power supply
output on and off by itself. Zach asked for it directly:

> "set the outputs of the steering, collimating, and grid bias to all turn on
> on prestart and turn off on abort/stop/end of run"

and, on when the sample bias should come up:

> "steering/grid/collimating/bias can stay on all run. No need to actuate for
> plasma on/off events. Collimating in particular is important for plasma
> stability when the beam dump is grounded. Start at prestart so I can see how
> things are working before the run starts."

What that means in code (`Supervisor.supplies_output_on` / `_off`, driven by
`prestart_output` in `config/reactor.yaml`):

| Trigger | Effect |
|---|---|
| Pre-start begins | outputs **ON**, before any gas |
| Reignite / pause / beam on-off | **nothing** |
| Run ends, aborts, or crashes | outputs **OFF** |
| **Stop** pre-start | **nothing** (hands over primed, like Ar and the fill) |
| **Abort** pre-start | outputs **OFF** |

Three properties of this are deliberate and should not be "tidied":

- **They are not tied to the beam.** A reignite does not touch them. Cycling
  the collimating coil with the plasma would destabilise the very thing it is
  there to stabilise. `tests/test_keithley_supplies.py` asserts that a reignite
  produces no output transition at all.
- **`stop_prestart` does not switch them off**, only `abort_prestart` does —
  matching how Ar and the fill regulation are already treated.
- **`disconnect()` never switches an output off.** Losing a serial handle, or
  restarting the server, must not drop the collimating coil out from under a
  running plasma.

### Manual control from the Hardware tab (requested 2026-08-25)

Each 2260B card also carries a **voltage field, a current field and an output
toggle**, so all four supplies can be driven by hand. `POST
/api/supply/{id}/{voltage,current,output}` are the only routes from the browser
to a supply output, and they exist for the Keithleys alone — the Glassman has no
set path in its driver beyond `hv_off`, and asking for one returns an error
rather than silently doing nothing.

Three details:

- **Blank means "leave it".** Either field can be submitted on its own.
- **Voltage is sent before current** when both are given, so raising both never
  briefly runs the new voltage against the old, lower current limit.
- **Turning an output on asks for confirmation**; turning it off never does.

This changed a previous rule rather than overlooking it. Until 2026-08-25 the
driver deliberately never touched a **current limit** — the supplies were found
with Zach's working setpoints dialled in and those were his alone to set. He
asked for current fields, so `set_current` now exists. **Nothing sets a current
automatically**; only the operator's field reaches it, and the test suite
asserts that a full pre-start-plus-run produces zero current writes.

The **CV/CC indicator** on each card is derived from measurement versus
setpoint — whichever limit the output has actually reached — and shows nothing
when neither has been. It is not read from a status register: `:OUTP:MODE?`
returns 0 regardless of state on these units, which had everything reading CV.
See [KEITHLEY_2260B.md](KEITHLEY_2260B.md).

### The sample bias

The stage/sample bias supply is the conditional one, and the only supply whose
**voltage** this program sets. Its output comes on at pre-start **only when the
run's Sample bias field is non-zero**; at zero it is explicitly commanded off
and an event says so, rather than leaving the operator to infer it.

Only the voltage is set. **Pre-start never sets a current limit** on any of the
four — that stays wherever the front panel or the Hardware-tab current field
last put it.

The `+`/`−` polarity toggle is **bookkeeping, not control**. A 2260B is
single-quadrant and cannot source a negative voltage, so the sign never reaches
the instrument: it records which way the leads were run onto the stage and is
applied to the *logged* voltage. There is no software limit on the bias
magnitude beyond the supply's own rating.

Because this is the one output that puts a potential on the sample, the
pre-start confirmation dialog states it explicitly — magnitude, sign, and
whether the stage will be energised at all.

Details in [KEITHLEY_2260B.md](KEITHLEY_2260B.md).

## Devices this program reads but (almost) never commands

Some hardware is deliberately monitor-only. Connecting to it is read-only, and
there is no path from the UI to any write.

- **Film Sense FS-1 ellipsometer.** Subscribes to the instrument's broadcast;
  the trigger sockets are deliberately untouched. Still fully read-only.
- **XP Glassman FL1.5F1.0 HV plasma supply.** Polled for voltage, current and
  arc count at 2 Hz and logged. Zach sets voltage and current by hand on the
  front panel, and this program **sends it exactly one command: HV OFF.**

### The one HV command (requested 2026-08-21)

`Supervisor.hv_off()` asserts HV Off on every configured supply, and is called
from exactly two places, both of them an ending:

- `finish_run()` — however a run ends: completed, aborted, or crashed. (The
  recipe runner calls it from its own `finally`, so a crash is covered too.)
- `abort_prestart()` (below) — the same intent for the pre-run state.

There is still **no way to set a voltage, and no way to turn HV on**, from the
supervisor, the API or the browser. The Hardware-tab card has no inputs; not
disabled inputs, absent ones. Voltage/current control remains a not-yet.

Two protocol facts shape how the off is sent, and both are load-bearing:

- The FL's Set frame **always carries a voltage and a current program** — there
  is no "HV off only" packet. Sending zeros would wipe the levels Zach dialled
  in, so `GlassmanFL.hv_off` echoes the supply's own last reading back with the
  frame. Do not "simplify" that to `set_hv(False, 0, 0)`.
- **Any Set command puts the supply into REMOTE.** The front-panel LOC/REM
  button switches it back; the button does nothing while already local, which is
  normal and not a fault. So after a run ends, the supply will be sitting in
  remote until Zach presses LOC/REM.

Two older decisions still stand and should not be quietly reversed:

- **`disconnect()` does not send HV OFF.** A driver for this same supply
  elsewhere in the group does, after an incident where their app exited leaving
  HV energised. This program still does not: HV off belongs to *a run ending*,
  not to *the server stopping*, or closing a browser tab would kill a plasma
  Zach set by hand. Stopping the server closes the port and nothing else.
- **There is no voltage limit.** An earlier plan capped the setpoint at 1000 V;
  Zach withdrew it once this became read-only. Nothing here sets a voltage, so
  there is still nothing to clamp — but **if setpoint control is ever added, the
  cap should be reconsidered at the same time** (on a 1500 V supply, 1000 V was
  2730 counts, `0xAAA`). Details in [GLASSMAN_FL.md](GLASSMAN_FL.md).

## The pre-start abort (requested 2026-08-21)

`Supervisor.abort_prestart()` is one click that undoes a pre-start: Ar flow to
zero then the isolation valve closed, fill regulation stopped and the fill valve
closed, the beam relay **de-energised**, and HV off.

It exists because `stop_prestart` only ends the *sequence* and deliberately
leaves the tool primed — Ar flowing, fill pulsing, beam grounded — which is the
state a successful pre-start hands to Start run. That meant the Stop button
greyed out at exactly the moment the operator most often wanted to back out.

The relay ending de-energised is deliberate, not an oversight: the relay box
runs off a 9 V battery that drains only while the relay is energised (see
[HARDWARE.md](HARDWARE.md)), so at rest it belongs off. HV is commanded off in
the same call, so there is nothing for an un-grounded relay to do.

## The Shut down server button (requested 2026-08-25)

Diagnostics has a **Shut down server** button. It stops this server *and kills
any other reactor server still running*, so nothing is left holding the DAQ or
the serial ports. You then start it again from the shortcut.

It replaced a Restart button that re-exec'd the process. That was removed the
same day it shipped: on 2026-08-25 an old instance survived the restart, so two
servers were up at once - the newer one holding port 8000 while the older one
still held COM8-COM12 - and every device looked unreachable. Zach's call was
"just a button that kills all servers in use", and stopping cleanly is both
simpler and easier to see than a restart that half-worked.

Mechanics: other instances are terminated first, then this one shuts down
through the normal lifespan teardown - so the server you are talking to releases
its devices properly, and any orphan holding a serial port is gone before you
restart. It then calls `os._exit(0)` rather than falling out of `main()`,
because something in the stack keeps a non-daemon thread alive; that is exactly
how the orphan survived.

**A shutdown is not a neutral act on this tool.** The confirm dialog spells out
what follows and changes wording depending on whether a run is live:

- **A running recipe is ABORTED**, with the full end-of-run teardown: MFCs
  zeroed, fill valve closed, HV commanded off, DC supply outputs switched off.
- **Gas stops either way.** The MKS G50s zero their own setpoints when the
  Modbus master disconnects. Device behaviour, not something this program does.
- **Valve lines are not commanded.** Last-commanded state is persisted and
  restored at startup.
- **With no run active, HV and the DC supply outputs stay as they are** -
  neither driver commands anything on `disconnect()`. So a shutdown outside a
  run stops the gas while leaving the supplies energised.

There is **no guard**: it will stop the server during a run if you confirm it.
The dialog informs, it does not refuse.

If the server was started some other way (not `python -m reactor`), there is no
shutdown hook and the endpoint returns 501 rather than pretending.

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
