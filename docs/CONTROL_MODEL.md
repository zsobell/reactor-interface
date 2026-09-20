# What this program does to the reactor

The complete, honest answer. Read it before assuming the software will protect
anything.

## Automatic actions follow the operator's specified behavior

Commands execute **exactly as given**. There is no chamber-pressure interlock, no
MFC setpoint clamp, no watchdog that closes valves, no arm/disarm gate, no
general refusal to actuate, or general auto-close on shutdown. The requested
Ar isolation interlock, soft-open sequence, run/pre-start cleanup and flags below
are explicit exceptions. A setpoint is written as typed subject to that isolation
interlock; a valve follows its configured opening sequence.

This is deliberate and was done at the operator's explicit direction. **Zach is
the sole arbiter of reactor behavior.** An earlier version of this program had a
pile of unrequested safety machinery built on assumptions about a reactor the
author did not understand; it blocked legitimate commands and destroyed trust. It
was all removed on 2026-07-31.

**Do not add any interlock, limit, or automatic action without Zach's explicit
say-so.** If you think one is warranted, propose it and wait for a yes. (See the
`no-unrequested-safety-features` memory.)

## The guards that exist — because they were requested

The principal guards below are all explicitly requested by Zach and narrowly
scoped:

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

3. **Ar pneumatic soft open** (2026-08-26). The Ar isolation valve is never
   opened in one flip. Every open of it — the Hardware tab, pre-start, a
   recipe step — bleeds it in with a **0.05 s pulse, waits 0.5 s**, and only
   then opens it, so the Ar built up behind it does not dump into the reactor
   all at once. Zach's words: *"so as to let built up Ar into the reactor more
   slowly."* It was five pulses on the first pass; he cut it to one on
   2026-08-26 because the pneumatic is too slow for a short command to move it
   far, so five pulses were simply five inrushes and the chamber gauge tripped
   off anyway. Which valve behaves this way is `soft_open` in
   `config/reactor.yaml`; the three numbers are operator settings in the Run
   tab's **Advanced timing** panel, and setting the pulse count to 0 turns it
   back into a plain flip. It is in `Supervisor.set_valve`, not in the callers,
   precisely so that "any time" means any time.

   The pulses' *closes* deliberately use the quiet write path, so they do not
   fire the isolation interlock above — they are part of opening, not a close,
   and re-opening an already-open valve must not silently stop the gas.

4. **Exclusive experiment ownership.** A normal run, pre-start, valve-ID
   sweep, fill task and HCPES acquisition cannot quietly compete for the same
   controls. In particular, HCPES owns every configured MFC, its four support
   supplies and the plasma relay from accepted start through cleanup. Every
   background MFC is either a plan axis or held at zero; manual background-MFC
   changes are refused until HCPES releases ownership.

These exist **only** because the operator asked for them directly. None is a
precedent for adding more without asking first.

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

That quotation records the original request. The later sample-bias bracket
supersedes its bias timing; the table below describes current behavior.

What that means in code (`Supervisor.supplies_output_on` / `_off`, driven by
`prestart_output` in `config/reactor.yaml`):

| Trigger | Effect (steering, grid, collimating) | Effect (sample bias) |
|---|---|---|
| Pre-start begins | outputs **ON**, before any gas | **armed** — level and polarity set, output left **OFF** |
| Beam on / beam off | **nothing** | **ON** a lead time before, **OFF** a trail time after |
| Reignite / pause | **nothing** | **nothing** |
| Run ends, aborts, or crashes | outputs **OFF** | output **OFF** |
| **Stop** pre-start (`stop_prestart`, no longer a button) | **nothing** (hands over primed, like Ar and the fill) | **nothing** (stays armed, output off) |
| **Abort** pre-start | outputs **OFF** | output **OFF** |

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
asked for current fields, so `set_current` now exists. In the normal
ALD/CVD/pre-start workflow, **nothing sets a current automatically**; only the
operator's field reaches it, and the test suite asserts that a full
pre-start-plus-run produces zero current writes. HCPES is the explicit
exception: its reviewed plan programs steering and collimating current for each
condition, as described in the HCPES section below.

The **CV/CC indicator** on each card is derived from measurement versus
setpoint — whichever limit the output has actually reached — and shows nothing
when neither has been. It is not read from a status register: `:OUTP:MODE?`
returns 0 regardless of state on these units, which had everything reading CV.
See [KEITHLEY_2260B.md](KEITHLEY_2260B.md).

### The sample bias follows the beam (changed 2026-08-26)

The stage/sample bias supply is the conditional one, and the only supply whose
**voltage** this program sets. Until 2026-08-26 its output came on at pre-start
and stayed on for the whole run, like the coils. It no longer does, because a
live bias makes the stage thermocouple unreadable:

> "the sample bias thermocouple issue has become untenable. We need the sample
> bias to trigger 0.2 s before the e-beam and turn off 0.2 s after. [...] At
> least then I can get good thermocouple data when the e-beam is off."

So now:

- **Pre-start arms it**: the level and the lead orientation are programmed, the
  output is left **off**, and an event says "armed ... output follows the beam".
  At a Sample bias of 0 the supply is never switched on at all.
- **Each beam brackets it.** In EE-ALD the bias comes up `Bias lead` seconds
  before every cycle's beam and drops `Bias trail` seconds after it — both in
  the Run tab's Advanced timing panel, 0.2 s by default. In EE-CVD the beam is
  one long step, so the bias leads the strike at the start of the run and drops
  after the beam at the end of it.
- **A reignite does not cycle it.** It brackets the beam *step*, not every flip
  of the plasma-ground relay — a 0.1 s reignite pulse is shorter than the lead,
  so chasing it would leave the bias down for most of the restrike.
- **It costs no run time.** Both flips are scheduled tasks, not awaited steps:
  the lead counts down inside pump A and the trail inside pump B, so a cycle
  still takes exactly the sum of its step durations
  (`tests/test_sample_bias_bracket.py` asserts this alongside the timings).
- **The level is written once per run**, on the first bracket, so a level
  adjusted by hand on the Hardware tab mid-run is not overwritten every cycle.
- **However a run ends** — finished, aborted, or crashed — every queued flip is
  cancelled and the output is switched off. An abort does not honour the trail.

Only the voltage is set. **Pre-start never sets a current limit** on any of the
four — that stays wherever the front panel or the Hardware-tab current field
last put it.

The `+`/`−` polarity toggle is **bookkeeping, not control**. A 2260B is
single-quadrant and cannot source a negative voltage, so the sign never reaches
the instrument: it records which way the leads were run onto the stage and is
applied to the *logged* voltage. There is no software limit on the bias
magnitude beyond the supply's own rating.

Because this is the one output that puts a potential on the sample, the
pre-start confirmation dialog states it explicitly — magnitude, sign, and that
pre-start only arms it, the stage being energised around each beam once the run
starts.

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

`Supervisor.hv_off()` asserts HV Off on every configured supply as part of the
requested cleanup paths:

- `finish_run()` — however a run ends: completed, aborted, or crashed. (The
  recipe runner calls it from its own `finally`, so a crash is covered too.)
- `abort_prestart()` (below) — the same intent for the pre-run state.
- HCPES cleanup — completion, stop, failure or server shutdown while HCPES owns
  the reactor.

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

`Supervisor.abort_prestart()` is one click that runs the active pre-start
recipe's snapshotted abort sequence. Editing or deleting the saved recipe after
launch cannot change that cleanup. Abort continues best-effort through later
steps if one cleanup command fails, retains the failure in status, and consumes
the snapshot once so a repeated abort does not actuate again.

The protected **Current pre-start** recipe preserves the requested cleanup: Ar
flow to zero then the isolation valve closed, fill regulation stopped and the
fill valve closed, HV off, DC outputs off, and the beam relay
**de-energised**. Custom recipes expose every cleanup action for operator review
before launch; the program does not invent inverse commands for their start
steps.

It exists because `stop_prestart` only ends the *sequence* and deliberately
leaves the tool primed — Ar flowing, fill pulsing, beam grounded — which is the
state a successful pre-start hands to Start run. That meant the Stop button
greyed out at exactly the moment the operator most often wanted to back out.
**That button was removed on 2026-08-28**, along with `POST
/api/prestart/stop`: abort is the one way out, running or already struck, and
it calls `stop_prestart` itself on the way through. The method stays; only the
button and its route went.

The relay ending de-energised is deliberate, not an oversight: the relay box
runs off a 9 V battery that drains only while the relay is energised (see
[HARDWARE.md](HARDWARE.md)), so at rest it belongs off. HV is commanded off
**first** and the ground released after (reordered 2026-09-09 — releasing the
ground energises the beam path, so doing that while the supply might still be
up was the wrong way round), and there is then nothing for an un-grounded relay
to do.

## HCPES characterization is its own acquisition owner (2026-09-18)

HCPES characterization is not a cyclic ALD/CVD recipe and does not reuse the
main precursor-fill pre-start. The operator saves and reviews a versioned plan
whose ordered blocks define the Cartesian parameter space. Every plan resolves:

- Ar MFC flow;
- stage-bias magnitude plus a separately confirmed physical polarity;
- grid-bias voltage;
- steering and collimating current; and
- every configured background MFC, either explicitly fixed/swept or locked at
  zero. There is no unowned background MFC during an HCPES run.

The supply protocol and machine-readable records retain amperes. The HCPES
builder, live monitor, readable condition YAML and Analysis page present
steering, collimating and measured stage current in **mA** by default, converting
only at that human-facing boundary.

Start is refused until the exact saved revision is previewed and the operator
confirms the displayed stage-lead orientation. Positive is the default. HCPES
then owns all configured MFCs, the four support supplies and plasma relay;
manual writes and competing run/pre-start/fill/sweep starts are refused until
cleanup finishes. Its restricted startup commands, in order:

1. every MFC to zero;
2. the Ar pneumatic isolation valve open;
3. the first condition's MFC and four-supply programs (without artificial
   per-parameter delays while there is not yet a plasma to settle);
4. all four HCPES support-supply outputs on; and
5. the plasma relay to beam-on, followed immediately by the startup
   stage-current stability gate.

Startup and every successful plasma re-establishment use the stricter
**establishment profile**: a spike-resistant DMM6500 stage-current trend below
`0.1 mA/min` continuously for 20 s, with an independent 60 s maximum settle
wait. A timeout while plasma remains present does not discard or halt the
condition: it records `settled=false` and proceeds.

Between ordinary parameter changes the operator chooses either the default
fixed 3 s delay after each changed parameter or one **parameter-change
profile** after all changes are applied. That separate current profile defaults
to drift below `0.3 mA/min` continuously for 3 s, with a 10 s maximum settle
wait. It never forces the 20 s establishment window on an ordinary parameter
change. If plasma disappears during this shorter gate, it is abandoned and the
full recovery/establishment behavior takes over.

Qualified collection is count-based: after settling, accept the next five fresh
sample-current readings by default. There is no separate HCPES sample interval
or collection-duration target; acquisition follows the instrument telemetry
rate (normally 5 Hz, so five readings take about one second). Plasma-present
defaults to an absolute stage current of at least `0.1 mA`. A lower sample
pauses qualification, pulses the relay for the configured 1 s and waits the
configured 1 s before checking again. Reignition attempts consume a separate,
user-editable 30 s retry budget; time observing an established plasma does not
consume that budget. Every relight gets a fresh, complete establishment gate.
If plasma drops during that gate, retry resumes with its remaining budget.
Retry exhaustion marks only that condition inaccessible and continues the grid.

Stop, failure, server shutdown and normal completion all run this ordered
cleanup and retain receipts:

1. command every MFC to zero;
2. close Ar isolation after those zero commands;
3. command HV off without changing its front-panel programs;
4. switch the stage-bias, grid-bias, collimating and steering outputs off; and
5. park the plasma relay de-energized in beam-on state.

Each immutable session directory has both operator-facing and machine-facing
files. `run_summary.txt` is the Notepad-friendly overview; `timeline.csv` is a
concise chronological activity/timer log; and multi-document `points.yaml`
breaks results into one readable subsection per condition. `manifest.yaml`
describes the plan/status/files, `points.csv` is spreadsheet-ready, `raw.jsonl`
retains every full telemetry snapshot, `qualified.jsonl` contains only accepted
samples, and `point_channels.jsonl` holds statistics for every numeric qualified
channel. Missing measurements remain missing rather than becoming zero. Aborted
and failed sessions keep honest partial data.

Full positive-to-negative characterization uses two separately started
sessions. After the first session has completed its full cleanup, the operator
may create an exact opposite-polarity clone, switch off/verify supplies,
physically swap the stage leads, confirm the new orientation and start again.
The derived `campaign.yaml` and `combined_points.csv` link but never modify the
sources. Analysis sorts by signed stage bias and retains both zero points,
negative session first at the tie, so a polarity-swap discontinuity remains
visible. Incompatible signatures are flagged and never silently stitched. The
run-sequence/acquisition-order view means the order conditions completed, not a
physical swept coordinate: it is for spotting drift over the experiment. Each
point hover reports every commanded condition, source session/point, result,
settle state and observed stage-current drift.

All of the above is software sequencing tested with fake devices. It does not
qualify physical lead orientation, actual MFC/supply response, DMM noise,
plasma accessibility, timing, or cleanup response on the reactor.

## Ending a RUN lands in the same place (2026-09-09)

Operator: *"Abort does not leave the plasma ground in the right position or
clear the run progress section."* A clean run's teardown parks the relay
de-energised, but an abort never reaches a teardown, so it stopped at the
runner's immediate "ground the beam" and left the relay **energised** — on that
same 9 V battery — for as long as nobody noticed.

`Supervisor.finish_run()` now ends every run the same way, in this order:

1. the runner grounds the beam at once (its own `finally`, unchanged — this is
   the fast response while HV may still be up),
2. HV off, DC supply outputs off,
3. the relay released to its resting, de-energised state — skipped if it is
   already there, so a clean run does not write the line twice.

The Run panel follows: a run that ended by abort clears its progress bar, cycle
counter, step and remaining-time readouts and its phase strip. A **completed**
run keeps its final tally — that is the result, not stale state.

## Pause stops the action, not just the clock (2026-09-01)

Operator: *"pause doesn't really work. in the purge step the timer keeps moving,
in the e-beam step the beam stays on. God knows what happens if I pause in the
dose step... It needs to stop the current action (e-beam or dose) and stop the
timer. The step should resume with the correct timing on resume."*

Pause used to be honoured only at a step **boundary** and inside the beam step's
tick loop. `_sleep` - which every wait and every dose ran on - never looked at
it, so a pump counted straight through a pause and a paused dose held its valve
open for as long as the operator was away.

Now, on pause:

- the **dose valve closes**, and reopens on resume with the rest of its pulse
  still to run;
- the **plasma ground goes back on** (beam off), in EE-ALD and EE-CVD alike, and
  the beam is re-struck on resume with a fresh settle window so the re-strike is
  not misread as a dead plasma and reported as a reignite;
- **every clock stops**: the step countdown, the exposure budget, the cycle
  number and "est. remaining". The step resumes with exactly what was left.

What pause does **not** touch, asked and answered the same day: the **sample
bias stays energised** and the **scheduled gases keep flowing**. Do not add
either without asking.

## Setpoint vs measurement warnings (2026-09-01)

The precursor fill pressure has always flagged when it drifts off setpoint.
Operator, after a background MFC on Mo-017 sat at a flow it never reached with
nothing to say so: *"All params should be monitored like the precursor
pressure"* — and, on what the check should be, *"just a warning that the
setpoint doesn't match the measured flow value during a run."*

`Supervisor.setpoint_flags()` applies exactly the fill regulator's rule -
`|measured - commanded| / commanded` past the same **Fill flag tolerance (%)** -
to every MFC and to any supply whose output is on. Up to four live conditions
show as chips in the top right of the header, and each clears itself the moment
its condition does.

**A supply in CC mode is exempt** (2026-09-09). A current-limited supply sits
below its voltage setpoint by definition — that is what constant current *is* —
so judging it against that setpoint produced a warning that stood for the whole
run, on the coils, every run. The check now applies only where the supply
claims CV, and `mode_label()` already says nothing at all unless a limit has
actually been reached, so an unloaded or settling output is not judged either.

**These warn and nothing else.** No setpoint is refused, clamped or altered by
any of it; a flagged value is still written to the hardware exactly as typed.
There is a `SETPOINT_SETTLE_S` grace after each commanded change so a device on
its way to a new value is not called a mismatch - a display debounce, nothing
about the control path depends on it. A commanded **zero** is not monitored:
there is no relative baseline, and a gas that is off is not a fault.

Deliberately NOT added: any minimum-flow or out-of-range check. The MFCs report
a `min_setpoint` of 0.015 sccm, which would not have caught the 0.6 sccm case
anyway, and every other candidate threshold would have been a number this
program invented.

## Parameters are editable mid-run (2026-09-01)

Operator: *"I need to be able to change parameters mid run."* A recipe used to
be a snapshot taken at Start - the Run tab's fields stayed editable during a run
and simply went nowhere.

`Supervisor.update_run_params()` diffs the new parameters against what the run
is actually using, rebuilds a recipe from them, and copies the numbers into the
**running** Step objects (`RecipeRunner.apply_params`). A gas that is flowing
right now is re-commanded immediately rather than at its next window; the fill
regulator is **retuned in place** rather than restarted, because restarting it
would close the fill valve and drop the chamber off setpoint mid-run.

A step's duration is read when the step starts, so a timing change lands the
next time that step runs. The cycle count is re-read every cycle: raised, the
run keeps going; **lowered below the cycle in progress, that cycle finishes and
the run ends there with its full teardown** (operator's call - never a half
cycle in the data).

Switching a scheduled gas **off** counts as a change like any other
(2026-09-09). It did not used to: an unticked gas simply vanishes from the
freshly built recipe, `apply_params` only looked at gases present in both, and
the live schedule object survived untouched — so a line the operator had
switched off, showing 0 in every field, went on being commanded to its old flow
every cycle. A gas that disappears is now shut off immediately and dropped from
the plan; one that appears is picked up. The EE-ALD lead-in list is re-read
every cycle for the same reason (it used to be captured once before the cycle
loop, which would have re-armed a gas that had just been switched off).

Every change is timestamped from the start of the run, tagged with the cycle,
and written into the run's parameters report under **CHANGES DURING THE RUN**,
which is rewritten on each edit. Without it the report would list the values the
run ended with and quietly describe a run that never happened.

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
how the orphan survived. Since 2026-08-28 that exit is **unconditional** — it
used to run only when the button had been pressed, so a second copy started by
mistake, which fails to bind port 8000 and tears itself back down, took the
plain `return` path and lingered in exactly the same way.

### Why it kept not working (fixed 2026-08-28)

The button was still leaving the old server up. From `server.log`: "[command]
server shutdown requested from the UI" at 14:53:20 and then **not one further
line from that process** - no teardown, no error - while the next server started
20 s later and got "resource is reserved" from the DAQ and "Access is denied" on
COM9. The old instance was still there at 14:54:10, when a second shutdown
finally taskkilled it.

That silence places it exactly: uvicorn's `Server.shutdown()` closes the
listening socket **first** (which is why the next server binds port 8000 and
looks fine), then drains open connections, and only *then* runs the lifespan
shutdown that is `Supervisor.stop()`. No teardown lines with the port already
free means it was parked in the drain — and that drain is
`timeout_graceful_shutdown`, which uvicorn defaults to **None: wait forever**.
One WebSocket whose peer never answers the close - a laptop asleep over
Tailscale, a browser gone without a FIN - holds the whole stop there, keeping
the DAQ and COM8-COM12 for as long as it likes. uvicorn says so at INFO
("Waiting for connections to close"), which `log_level="warning"` suppresses,
which is why the log showed nothing at all.

Two fixes, both in `reactor/__main__.py`:

- **the drain is bounded** (`SHUTDOWN_DRAIN_S`). On timeout uvicorn cancels
  the stragglers and *continues into the lifespan shutdown*, so the devices are
  still released properly. That is why it is a timeout and not `force_exit`,
  which would skip the teardown altogether.
- **the hard deadline behind it can now actually fire.** It never could:
  its first statement referenced a `log` this module never defined, so the
  daemon thread died on `NameError` instead of calling `os._exit(1)`. Nothing
  else in that module used `log`, so nothing ever raised anywhere visible - and
  there is no trace of it in `server.log` because the deadline had never once
  been reached, the drain having hung long before 20 s were up.

Measured end to end afterwards, launched exactly as the shortcut does and with a
client parked on the WebSocket: shim and interpreter both gone **1.3 s** after
the button. `tests/test_server_shutdown.py` pins the mechanism.

### Then reordered so it can be BELIEVED (2026-09-10)

1.3 s is not what the operator experienced, and "it stopped answering" is not
what he needed to know. Zach: *"20 s hold is way too long, and there is no way
for me to know if it worked or not. I need some confirmation things are shut
down and ready to be booted again."*

Both complaints come from the ordering above. The drain releases the socket
**first**, so everything the browser can observe happens before the teardown it
actually cares about - the page was calling success on the DAQ and COM8-COM12
while they might still be held. And the sibling sweep ran a `powershell.exe`
`Win32_Process` query *inside the request*, 1-3 s of cold start before the
browser was told anything at all.

So the teardown moved in front of the answer. `POST /api/server/shutdown` now:

1. sweeps siblings from `config/instances/` (see `reactor/instances.py`) and
   ends them through the Win32 API - no subprocess, microseconds;
2. runs `Supervisor.stop()` **here**, in the request;
3. answers with a **receipt** - each step, what was released and by which port
   (`grid_bias (COM11)`, `DAQ tasks`), what failed, how long it took;
4. and only then asks the process to end.

`Supervisor.stop()` is idempotent and returns that receipt, so the lifespan
calling it again on the way out costs nothing. The whole request answers in
~20 ms in the original idle measurement, not a deadline for active cleanup;
`SHUTDOWN_DEADLINE_S` dropped 20 s → 3 s, since everything after the
response is socket cleanup.

The merged implementation shares one shutdown task across concurrent callers.
Run abort, pre-start abort, device disconnects and recording close have bounded
waits. The receipt distinguishes released ports from failures, including a
five-second recording-close timeout and latched recording errors. Released
ports do not establish that experiment files drained successfully. A late
worker may continue until process exit; a recording failure does not add new
hardware actions.

The third defect was the one that had been doing real damage. uvicorn 0.52's
`Server.startup()` calls `sys.exit(STARTUP_FAILURE)` when the bind fails, and
that `SystemExit` propagates out of `server.run()`, **jumping over the
`os._exit` that merely followed it**. On 2026-09-10 two servers started within
the same second; one bound port 8000, the other failed and lingered holding
COM10 and COM11, so `steering` and `grid_bias` reported themselves unreachable
and looked for all the world like the USB fault that had happened earlier the
same afternoon. `server.run()` is now inside a `try/finally` with the exit in
the finally, and the test checks that **structurally** (walks the AST for a
`Try` containing the `run()` call whose `finalbody` calls `os._exit`) rather
than grepping for `os._exit(0)` - the string was there the whole time.

Note the process tree, which is what made the first press look inert under the
old command-line sweep: the venv's `.venv\Scripts\pythonw.exe` is a launcher
shim that re-execs the real interpreter as a child (measured — from a venv,
`os.getppid()` is the shim while this process's own image is
`Python312\python.exe`), so **two** processes matched the query and it skipped
both, its own PID and its parent. The first press therefore always reported
"killed 0", and it was the *next* server's press that found the pair.

The registry sweep does not have that problem: only the process that actually
runs `main()` registers, so there is exactly one entry per server and the only
PID excluded is the caller's own. The shim is dealt with explicitly instead —
the entry records `ppid`/`pimage`, and `kill_others` takes the parent too, but
**only when that parent is itself a Python interpreter**. Started from a
terminal the parent is the shell, and killing an operator's console because
they launched the server from it would be its own bug. Measured 0.6 ms to sweep
a registered pair, both processes gone.

**Any shutdown aborts whatever is in progress.** Operator decision,
2026-08-25: *"any server shutdown should abort the run or prestart. Safety over
data collection."* The confirm dialog spells out what follows and changes
wording depending on whether a run is live:

- **A running recipe is ABORTED**, with the full end-of-run teardown: MFCs
  zeroed, fill valve closed, HV commanded off, DC supply outputs switched off.
- **A pre-start is ABORTED too** — not merely stopped. `Supervisor.stop()` calls
  `abort_prestart()`, which covers both a sequence still running *and* one that
  completed and left the tool primed. The primed case matters just as much: a
  successful pre-start deliberately leaves Ar flowing, the fill valve pulsing,
  the beam relay set, HV up and the DC supplies on, and a shutdown would
  otherwise walk away from all of it with nothing left to manage it.
- **Gas stops either way.** The MKS G50s zero their own setpoints when the
  Modbus master disconnects. Device behaviour, not something this program does.
- **Unrelated valve lines are not reset.** Run/pre-start cleanup commands the
  valves specified above. Last-commanded state is persisted and restored at startup.
- **With no run or primed pre-start active, HV and the DC supply outputs stay as they are** -
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
  pressing Abort pre-start. These are the emergency/manual-stop controls in the
  program; nothing else auto-stops.
- **A digital-output write only ever touches its own line.** Early on, writing
  one valve re-drove its whole DAQ module from an in-memory vector, which could
  silently close a *different* valve sharing that module (e.g. starting a run
  closed the Ar isolation valve). Fixed 2026-08-03 by giving every valve its
  own single-line DAQmx task. Worth remembering if a future change touches
  `reactor/devices/nidaq.py`: never go back to one task per module for DO.
