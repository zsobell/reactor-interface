# Keithley 2260B DC supplies

Four programmable DC supplies drive the beam column and the sample stage. The
reactor logs all four, switches their outputs on at pre-start and off at the end
of a run, and sets the voltage of one of them from a run parameter.

| Role | Model | Serial | Rated | Power |
|---|---|---|---|---|
| **Stage Bias** | 2260B-250-4 | `1412016` | 250 V / 4.5 A | 360 W |
| **Steering Coils** | 2260B-80-13 | `1408023` | 80 V / 13.5 A | 360 W |
| **Grid Bias** | 2260B-800-1 | `1407084` | 800 V / 1.44 A | 360 W |
| **Collimating Coils** | 2260B-250-9 | `1405224` | 250 V / 9 A | 720 W |

Identified 2026-08-21 by plugging them in one at a time and watching USB
enumeration — the same connect-and-compare method that identified the stage
thermocouple. Sample bias and stage bias are the same thing; Zach uses the names
interchangeably.

---

## Transport: USB CDC, not USBTMC

Unlike the DMM6500, a 2260B's USB port is a **USB CDC virtual COM port**
(`USB\VID_05E6&PID_2260`, enumerating as "USB Serial Device"). Windows binds it
with the in-box `usbser` driver — nothing to install, unlike the Glassman. It
still speaks SCPI, just over a serial link, so VISA sees `ASRL<n>::INSTR` rather
than `USB0::…::INSTR`.

### Matched by USB serial, never by COM port

Four near-identical supplies share one rack, and Windows renumbers COM ports
freely. The USB serial number is in the device descriptor, so it is the only
stable handle:

- `config/reactor.yaml` carries `usb_serial`, not `port`.
- `Keithley2260B.find_port` resolves the port from it at connect.
- `connect()` then checks the serial reported by `*IDN?` against the configured
  one and **refuses the device on a mismatch.**

That last check is not paranoia. The failure it prevents is putting 800 V of
grid supply onto the sample stage because two USB cables were swapped at the
back of a rack, and nothing else in the system would notice.

As found 2026-08-21, ports landed COM9–COM12 on hub locations `1-1.1`…`1-1.4`
in plug order. Treat that as a snapshot, not a fact.

---

## What this program commands

### Automatically (requested 2026-08-21)

**`:OUTP ON` / `:OUTP OFF`.** All four outputs come on during pre-start and go
off when a run ends, aborts, or crashes.

**`:SOUR:VOLT`.** On the sample-bias unit only, and only when the run's
**Sample bias** field is non-zero.

Nothing sets a **current** automatically. `tests/test_keithley_supplies.py`
asserts that a full pre-start-plus-run produces zero current writes.

### By hand, from the Hardware tab (requested 2026-08-25)

Each supply's card carries a **voltage field**, a **current field**, a **Set**
button and an **output toggle**. Either field may be left blank — blank means
"leave that one alone" — and when both are given, **voltage is sent first**, so
raising both never briefly runs the new voltage against the old, lower current
limit.

Turning an output **on** asks for confirmation; turning it off never does.

This supersedes an earlier rule worth recording, because the reasoning changed
rather than being forgotten. Until 2026-08-25 the driver deliberately never
touched a current limit at all: all four supplies were found with Zach's working
setpoints dialled in, and those were his alone. He then asked for current fields,
so `set_current` exists — but only the operator's field reaches it.

As found 2026-08-21, and still the values the supplies carry:

| | As found 2026-08-21 |
|---|---|
| Stage Bias | 20.000 V, 0.500 A |
| Steering Coils | 30.070 V, 3.700 A |
| Grid Bias | 100.000 V, 0.200 A |
| Collimating Coils | 150.000 V, 2.500 A |

All four outputs were off.

### CV / CC indicator — derived, not read

Each card shows a **CV** or **CC** light. It is worked out from the supply's own
behaviour, not from a status register:

- measured current sitting at the current setpoint → **CC**
- measured voltage sitting at the voltage setpoint → **CV**
- neither → **blank**, and the output being off is also blank

"Sitting at" means within **1%** (`MODE_TOLERANCE`). Zach's supplies "sometimes
drift a fraction of a %", so that is comfortably outside the noise while still
far inside the gap between a regulated value and an unregulated one. The
comparison uses **magnitudes**, so a negative-polarity sample bias still reads
CV rather than never matching.

Right at the knee — both limits reached at once — whichever is regulating more
tightly wins.

**Why not a register?** `:OUTP:MODE?` was tried first and is wrong. It is
accepted by the instrument but returns `0` on all four supplies in every state
observed, including coils demonstrably running in constant current, so the UI
confidently labelled everything CV (reported 2026-08-25). It evidently means
something other than the present operating mode. `:STAT:QUES:COND?` reads `0` in
every state seen too, so its CV/CC bits — if it has any — are no better. It was
dropped from the poll, which also gave back ~10 ms a tick.

The derived version needs no vendor decoding and is self-evidently correct,
which the guessed register bit was not.

### They are not cycled with the beam

The outputs come up at pre-start and **stay on for the whole run**. They are
deliberately not tied to plasma events — a reignite does not touch them.

Zach's reason, worth preserving because it is not obvious from the code: the
collimating coil is what keeps the plasma stable when the beam dump is grounded.
Cycling it with the beam would be actively harmful, not merely wasteful.

### When each trigger fires

| Trigger | Effect |
|---|---|
| Pre-start begins | outputs **ON** (bias only if non-zero) — before Ar, so the tool can be watched settling |
| Reignite, pause, beam on/off | **nothing** |
| Run ends, aborts, or crashes | outputs **OFF** (`Supervisor.finish_run`) |
| **Stop** pre-start | **nothing** — see below |
| **Abort** pre-start | outputs **OFF** |

`stop_prestart` deliberately leaves them on. It only ends the *sequence* and
hands the tool over primed for Start run, exactly as it leaves Ar flowing and
the fill pulsing. `abort_prestart` is the full undo, and that does switch them
off.

---

## Sample bias, and the polarity toggle

The run's **Sample bias (V)** field replaced *Min current (µA)* in the main
EE-ALD / EE-CVD parameter grid; min current moved into **Advanced timing**.

- Enter a **magnitude**. Zero means the stage bias output stays off for that
  run, and the event log says so rather than leaving you to infer it.
- The **polarity toggle** (`+` / `−`) records **which way the leads were run
  onto the stage**. The 2260B is single-quadrant and physically cannot source a
  negative voltage, so the sign never reaches the instrument — it is applied to
  the **logged** voltage, so `psu_stage_bias_voltage` reads `−12` when the leads
  are reversed. That is bookkeeping Zach asked for, not a control feature.
- The current limit is not part of this: pre-start never sets one. It stays
  whatever the front panel or the Hardware-tab current field last put there.

The pre-start confirmation dialog states the bias explicitly — magnitude, sign,
and whether the stage will be energised at all — because that is the one output
in the set that puts a potential on the sample.

---

## Polling

One compound query per tick::

    :MEAS:VOLT?;:MEAS:CURR?;:OUTP?;:STAT:QUES:COND?;:SOUR:VOLT?;:SOUR:CURR?

Measured on the hardware:

| | |
|---|---|
| All four **concurrently**, 4 values (measured + output + status) | ~46 ms |
| All four **concurrently**, 6 values (the above + setpoints) | **~68 ms** |
| All four **sequentially**, 4 values | ~132 ms |

Roughly 10 ms per extra value, essentially all of it USB-CDC round-trip latency.

They ride the 2 Hz slow control loop (`site.loop_hz`) in `Supervisor._cycle`
alongside the DAQ and the Glassman, and `_cycle` gathers them, so the cost is
~77 ms of a 500 ms budget — about 15%. **Do not turn that gather into a loop**;
sequentially they cost several times as much.

The setpoints are polled every tick rather than refreshed slowly so the card's
"Setpoint (device)" row never lags behind a Set the operator just made, or
behind someone turning the knob on the front panel. The editable fields are
*never* overwritten by the poll — they are empty inputs with a Set button, the
same pattern the MFC tiles use, so a refresh cannot fight you mid-type.

`:STAT:QUES:COND?` is surfaced raw as `questionable`. Non-zero means the supply
has flagged something — OVP, OCP, over-temperature or a fan fault — but the bit
map is not documented in anything available here, so the UI shows the raw value
rather than inventing a meaning for it. Check the front panel.

### Logged channels

Only measured voltage and current are logged; setpoints and the CV/CC mode go
to the Hardware card, not the run file.

| Snapshot key | Run-export column | `logging.columns` |
|---|---|---|
| `psu.stage_bias.voltage` | `psu_stage_bias_voltage` | `Bias V` |
| `psu.stage_bias.current` | `psu_stage_bias_current` | `Bias A` |
| `psu.steering.*` | `psu_steering_voltage` / `_current` | `Steering V` / `A` |
| `psu.grid_bias.*` | `psu_grid_bias_voltage` / `_current` | `Grid V` / `A` |
| `psu.collimating.*` | `psu_collimating_voltage` / `_current` | `Collim V` / `A` |

Voltage in V, current in A. Stage-bias voltage is **signed** by the polarity
toggle.

The `psu` namespace is separate from the Glassman's `hv`. Each device declares
its own `key_prefix` and `log_channels()`, so the Glassman keeps its established
`hv_hv_*` columns — the analysis page keys saved plot layouts on column name,
and renaming them would silently break every stored layout.

Because these are read at 2 Hz while run-export rows are written at 5 Hz, their
columns fill on roughly every other row and are blank between — the same as
pressure and the thermocouples. Blank means *not sampled here*, not zero.

---

## No remote/local handoff

The DMM6500 needs a USBTMC go-to-local on disconnect or its front panel sits
frozen on the last reading. **The 2260B has no equivalent.** Checked on the
hardware — all four of these are rejected with `-113 "Undefined header"`:

    :SYST:LOC    :SYST:LOCal    :SYST:REM    :SYST:RWL

So there is nothing to hand back, and `disconnect()` just closes the port. Do
not copy `instrument.return_to_local` into this driver.

`disconnect()` also does **not** switch the output off. That belongs to run
teardown, not to losing a serial handle: restarting the server must not drop the
collimating coil out from under a running plasma.

---

## Firmware spread

| Supply | Firmware |
|---|---|
| Stage Bias | `01.84.20190904` |
| Steering, Grid, Collimating | `01.72.20150702` |

Four years apart. Everything this driver uses works on both, but check both
before relying on any command added later — the two older-serial units
(1407084, 1408023) were likely bought together and the stage bias came later.

---

## Files

| Path | What |
|---|---|
| `reactor/devices/keithley_2260b.py` | driver, port-by-serial resolution, poll, output control |
| `reactor/config.py` | `PowerSupplyCfg` — `usb_serial`, `prestart_output`, `sample_bias` |
| `config/reactor.yaml` | the four `power_supplies:` entries, `logging.columns` |
| `reactor/supervisor.py` | `supplies_output_on` / `supplies_output_off`, pre-start and run-end hooks |
| `reactor/testing/virtual_reactor.py` | `FakeKeithley` |
| `reactor/server/static/index.html` | Hardware-tab cards, Sample bias field, pre-start dialog |
| `tests/test_keithley_supplies.py` | identification, switching behaviour, the "not cycled by a reignite" guarantee |
