# Glassman FL high-voltage plasma supply

The plasma power supply: an **XP Glassman FL1.5F1.0**, rated **1500 V / 1.0 A**,
connected to the reactor PC by USB.

The reactor **reads it and logs it, and sends it exactly one command: HV OFF,
when a run ends or is aborted.** Zach sets voltage and current by hand on the
front panel; there is no way to set a level or to turn HV *on* from this
program, no UI setpoint, and no software limit. See
[What this program commands, and why](#what-this-program-commands-and-why).

Sources: XP Glassman *Series FL* instruction manual, doc **102002-168 Rev H**,
26 Aug 2021 (Figures 18–37), and direct observation on this supply. Everything
below marked CONFIRMED was seen on the wire on 2026-08-21.

---

## Wiring

Rear panel, as fitted on this unit:

| Connector | What it is | State here |
|---|---|---|
| **J1** | DB-25, analog remote interface | interlock jumper **13 → 25**, in place for years |
| **J2** | RJ45 IN / OUT, RS-232 / RS-485 serial | unused |
| **J3** | USB "B" | **in use** — this is the link |
| **J4** | mounting for the optional Ethernet adapter | empty (option not fitted) |

Notes that cost time to establish:

- **J2 looks like Ethernet but is not.** It is an RJ45-shaped socket carrying
  RS-232, and it swings to RS-232 levels (up to ±12 V). Never plug it into a
  network port. Glassman ships an RJ45→DB-9 adapter cable for it; this reactor
  does not have one.
- **The FL has no DB-9.** Any DB-9 in the manual's figures is on the *adapter
  cable*, not the supply.
- **USB takes precedence over RS-232 automatically.** To test the RS-232 path
  you must unplug the USB from J3 first, or J2 is ignored.
- **Ethernet, when enabled (S2-10 ON), disables both USB and RS-232.** Not a
  factor here — J4 is empty and S2-10 is down.
- The connector numbering does **not** match the sister-series EJ/ET/EY/FJ/FR
  manual, which has J1 = RS-232, J2 = USB, J3 = 25-pin analog, and puts the
  interlock on pins 3→11. That manual describes a different product. Do not use
  it for this supply — see [Wrong manual](#wrong-manual).

## Link settings — CONFIRMED, and not guessable

```
COM8, 19200 baud, 8 data bits, no parity, 1 stop bit, address 1
```

**None of that matches the documented default, and none of it can be read off
the DIP switches.** The manual gives 9600 / address 0 as default, and every DIP
switch on this unit reads "down", yet it answers only at 19200 / address 1.
Firmware revision **02**.

The USB port presents a virtual COM port through a **TI TUSB3410** bridge, using
TI's generic driver (`umpusbvista`, SLLC428). **The COM number is not stable** —
a driver reinstall already moved this supply from COM7 to COM8.

If it ever goes quiet, do not assume hardware. Run the probe, which sweeps the
full cross-product of every supported baud rate against every valid address:

```bash
.venv\Scripts\python.exe -m tools.probe_glassman
```

Then update `port` / `baud` / `address` under `power_supplies:` in
`config/reactor.yaml`.

---

## Protocol

ASCII, checksummed, CR-terminated. The supply is a **pure slave**: it never
transmits unless asked. Manual-supported baud rates are 2400, 4800, 9600 and
19200.

Every frame:

```
SOH(0x01)   addr   <body>   <checksum>   CR(0x0D)
```

- `addr` is one ASCII hex digit, **0–7** (`0x30`–`0x37`).
- `checksum` is a modulo-256 sum rendered as two **uppercase** hex ASCII
  characters. It covers the **body only** — never the SOH, never the address.
- Lower-case letters are rejected.

### Commands

| Command | Bytes | Body |
|---|---|---|
| Query | 6 | `Q` |
| Version | 6 | `V` |
| Set | 21 | `S` V[3] I[3] `FFF` `000` ctrl[1] `FF` |

The manual's own worked example of a query, which is exactly what this driver
sends at address 1 (`01 31 51 35 31 0D`):

```
address 0:   01 30 51 35 31 0D
```

**Set command** (Figure 31) — reserved fields carry literal values the supply
requires: bytes 10–12 are `"FFF"`, bytes 13–15 are `"000"`, bytes 17–18 are
`"FF"`. The checksum covers bytes 3–18, i.e. `S` through the trailing `FF`.

Digital control nibble (byte 16) — **only one may be asserted per packet**, or
the supply answers with error 4:

| Bit | Meaning |
|---|---|
| 0 | HV Off |
| 1 | HV On |
| 2 | Reset — sets V = 0, I = 0, arc count = 0, HV off |
| 3 | unused |

Asserting none is legal, and is how you change setpoints while leaving the
output state alone.

### Replies

| Reply | Bytes | Layout |
|---|---|---|
| Acknowledge | 2 | `A` CR |
| Query response | 16 | `R` V[3] I[3] arc[2] status[2] fault[2] cksum[2] CR |
| Version | 6 | `B` rev[2] cksum[2] CR |
| Error | 5 | `E` code cksum[2] CR |

The query response checksum covers **all previous bytes except the first**
(bytes 2–13). The version checksum covers the two revision bytes. The error
checksum covers the single code byte.

**Status byte 10**

| Bit | Meaning |
|---|---|
| 0 | Fault |
| 1 | Local/Remote — HI = remote |
| 2 | Current-trip select |
| 3 | HV on |

**Status byte 11** — bit 1: V/I mode, HI = voltage mode. Others unused.

**Fault byte 12** — bit 0 interlock, bit 1 over-temperature, bit 2 input fault.
**Fault byte 13** — bit 2 arc fault, bit 3 current trip.

**Error codes** (Figure 37)

| Code | Meaning |
|---|---|
| 1 | unidentified command code (not S, Q or V) |
| 2 | checksum error |
| 3 | extra byte(s) received |
| 4 | illegal digital control byte |
| 5 | illegal set command with a fault active |
| 6 | processing error |

A malformed command always gets an `E` packet back. **Total silence therefore
means the supply is not receiving or not transmitting — it never means "bad
framing".** That distinction is diagnostic gold; see below.

### Scaling

Every analog field is **12-bit**: `0x000`–`0xFFF` spans zero to full scale, for
setpoints and monitors alike. On this FL1.5F1.0:

| | Full scale | Per count |
|---|---|---|
| Voltage | 1500 V | 0.3663 V |
| Current | 1000 mA | 0.2442 mA |

Arc count is 8-bit, `0x00`–`0xFF` = 0–255 arcs.

> The sister-series EJ/FJ manual specifies **10-bit** (`0x3FF`) monitors. That
> is wrong for the FL. Using it would under-read every value by a factor of 4.

### No communication timeout on the FL

The EJ/FJ-series supplies have a 1.5-second comms watchdog that drops HV and
zeroes the setpoints if the host stops talking. **No such timeout appears
anywhere in the FL manual**, and this supply has been sitting powered with no
host for years without issue. Do not design around one — but if remote control
is ever added, confirm this on the bench before relying on it.

---

## What this program commands, and why

`reactor/devices/glassman_fl.py` implements the full protocol, including
`set_levels`, `set_hv`, `reset` and `hv_off`. Only **`hv_off` is wired up**. The
rest are not reachable from the supervisor, the HTTP API, or the browser: Zach
asked for the protocol to be complete and in place for later, with no
level-setting control surface today.

**Updated 2026-08-21:** one of them, `hv_off()`, is now wired up. Zach asked for
HV to be commanded off whenever a run ends or is aborted, so
`Supervisor.hv_off()` calls it from `finish_run()` and from the pre-start abort.
Two things about how the off is sent are load-bearing:

- The Set frame always carries a V and an I program, so there is no "HV off
  only" packet. `hv_off()` echoes the supply's **last-read levels** back with the
  frame rather than sending 0/0, which would wipe the front-panel settings.
- Any Set command moves the supply into **REMOTE** until LOC/REM is pressed.
  Expect the supply to be in remote after a run ends.

There is still no way to set a level, or to turn HV *on*, from this program.

Three consequences, all deliberate:

1. **`disconnect()` does not send HV OFF.** A driver for this same supply
   elsewhere in the group does exactly that, after an incident where their
   application exited leaving HV energised. This program still does not: HV off
   belongs to *a run ending*, not to *the server stopping* — otherwise closing
   the program would kill a plasma Zach had set by hand. Stopping the server
   closes the port and nothing else.
2. **There is no voltage limit.** An earlier plan capped the setpoint at 1000 V.
   Zach withdrew it once this became read-only: with no way to set a voltage
   there is nothing to clamp. **If remote control is ever added, revisit that
   decision before wiring up `set_levels`** — 1000 V on a 1500 V supply was
   2730 counts (`0xAAA`), exactly.
3. **The UI card has no inputs.** Not disabled inputs — absent ones.

Adding remote control is a change to reactor behaviour and needs Zach's explicit
go-ahead. See [CONTROL_MODEL.md](CONTROL_MODEL.md).

---

## What gets read and where it lands

Polled on the **slow control loop** (`site.loop_hz`, 2 Hz) in
`Supervisor._cycle`, alongside the DAQ analog inputs. It does not get a timer of
its own: 2 Hz is the slowest cadence in the program, and one query round-trips
in **~13 ms** — under 3% of the 500 ms budget — so it costs the thermocouples
nothing.

| Snapshot key | Run-export column | `logging.columns` | Unit |
|---|---|---|---|
| `hv.hv.voltage` | `hv_hv_voltage` | `HV V` | V |
| `hv.hv.current` | `hv_hv_current` | `HV mA` | mA |
| `hv.hv.arc_count` | `hv_hv_arcs` | `HV arcs` | count |

Column names deliberately carry no units: the analysis page keys its saved plot
layout on column name, so they have to stay stable even if `unit_v`/`unit_i`
change.

Because the supply is read at 2 Hz while run-export rows are written at 5 Hz,
its columns are filled on roughly every other row and blank in between —
exactly as pressure and the thermocouples already behave. That is the logger
recording measurements rather than repeating stale ones (`write_run_sample`'s
`blank`). If they are ever wanted on every row, move the poll block from
`_cycle` into `_current_cycle`; **do not add a fourth timer.**

Status and fault flags are not logged — they go to `state()` for the Hardware
tab card and the Diagnostics connections table.

---

## Bring-up, and the mistake worth not repeating

This took a day, and almost all of it was avoidable.

The supply was silent through: both the EJ/FJ frame format and the correct FL
one; SCPI; four terminator variants; every handshake permutation; a serial
break; parity E/O/N; a driver reinstall; a full power cycle; and both pyserial
and NI-VISA. It looked exactly like a dead interface board, and was diagnosed as
one.

It wasn't. The search had a hole in it. Addresses 0–F were swept **at 9600
only**; baud rates 1200–115200 were swept **at addresses 0 and F only**. The one
cell that mattered — 19200 with address 1 — was never tested. The first true
cross-product sweep hit immediately.

Two lessons, both now built into `tools/probe_glassman.py`:

- **Sweep the cross-product, not the margins.** Two thorough-looking
  one-dimensional sweeps are not a two-dimensional sweep.
- **Silence from this supply is information.** It returns an `E` packet for any
  malformed command. Getting *nothing* rules out framing entirely and points at
  the link — which, in hindsight, was pointing at "you are not talking to it on
  the right wire settings" the whole time.

Things that were suspected and are **not** the problem, recorded so nobody
re-investigates them:

- **The interlock.** J1 pins 13→25 are jumpered and fault byte 12 bit 0 reads
  clear. A query is answered regardless — the manual states monitoring works
  "while still in LOCAL control mode ... at any time", with no preconditions.
- **The LOC/REM button not lighting.** Remote mode is entered by *sending a Set
  or Reset command*; the button only switches back **to** local. It does nothing
  while already local. This is normal, not a fault.
- **The USB driver.** TI's generic SLLC428 / `umpusbvista` is fine. A colleague's
  working FL runs the identical driver, showing as "TUSB3410 Device".
- **The DIP switches.** All down, matching a working unit — and yet the address
  and baud are 1 and 19200. Whatever those switches mean here, it is not what
  the sister manual implies. Do not infer settings from them.

### Wrong manual

Much of the early work used doc **102002-177 Rev M2**, the *EJ/ET/EY/FJ/FR*
manual, because the FL manual was not to hand. It is a different product and
misled on every specific: connector numbering, interlock pins, monitor
resolution (10-bit vs 12-bit), the absence of an address byte, and a 1.5 s comms
watchdog the FL does not appear to have. **Use 102002-168 Rev H for this
supply.**

### A note on the reference driver

A colleague's working Python driver for this series was invaluable — it is what
revealed the address byte. Its parser has bugs, though, and they were not copied
here. Against Figure 33 it reads bytes 7–8 as a "fault register" when that field
is the **arc counter**; discards bytes 11–12 as "reserved" when those are the
**real fault monitors** (interlock, over-temperature, input, arc, current trip);
and decodes status bit 2 as "fault" when it is current-trip select, and bit 0 as
unused when it is the actual fault bit. Its HV-on and remote bits are right.
Net effect on their bench: software that believes it is monitoring faults and
is not. Worth passing on.

---

## Files

| Path | What |
|---|---|
| `reactor/devices/glassman_fl.py` | driver, frame codec, decoders |
| `reactor/config.py` | `PowerSupplyCfg` |
| `config/reactor.yaml` | `power_supplies:` block, `logging.columns` entries |
| `reactor/supervisor.py` | construction, polling in `_cycle`, reconnect, `state()` |
| `reactor/testing/virtual_reactor.py` | `FakeSupply` |
| `reactor/server/static/index.html` | Hardware-tab card, connections row |
| `tools/probe_glassman.py` | read-only port/baud/address finder |
