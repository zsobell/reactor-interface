# Hardware inventory

Two sections, deliberately separated: what has been **verified** by querying the
hardware, and what is still **assumed**. Nothing here is inferred from the old
LabVIEW file — that turned out to be an unreliable guide (see
[LABVIEW_ANALYSIS.md](LABVIEW_ANALYSIS.md)).

---

## Verified

Enumerated with `python -m tools.discover_hardware` on the reactor PC.
NI-DAQmx driver 22.5.0. None of these are simulated devices.

### cDAQ1 — cDAQ-9174 chassis (serial 1831219)

| Slot | Module | Capability |
|---|---|---|
| Mod1 | **NI 9265** | 4 × analog **output**, 0–20 mA current |
| Mod2 | **NI 9211** | 4 × thermocouple input, ±80 mV |
| Mod3 | **NI 9375** | 16 × digital in (`port0`) + 16 × digital out (`port1`) |
| Mod4 | **NI 9211** | 4 × thermocouple input, ±80 mV |

### cDAQ2 — cDAQ-9174 chassis (serial 19F064E)

| Slot | Module | Capability |
|---|---|---|
| Mod1 | **NI 9201** | 8 × voltage input, ±10 V |
| Mod2 | **NI 9472** | 8 × digital output, 24 V sourcing |
| Mod3 | **NI 9472** | 8 × digital output, 24 V sourcing |

### Serial ports

Four Prolific USB-to-serial adapters: `COM3`, `COM4`, `COM5`, `COM6`. Purpose
still unknown, and nothing in this program uses them. (Not the MFCs — those are
Ethernet, confirmed below. Candidates are the gauge controller or a pump
controller.) One data point since: **`COM4` asserts DSR and CD**, so something
powered is attached to it, but it is silent and unidentified. The other three
show no sign of anything connected.

Plus one that *is* in use: **`COM8`, a TI TUSB3410 bridge — the Glassman HV
supply's USB port.** See [Glassman FL supply](#glassman-fl-high-voltage-supply)
below. Its COM number is not stable; a driver reinstall moved it from COM7.

### Other

- **Keithley DMM6500** on USB — enumerated and in use; resource string and
  identity under [Keithley DMM6500](#keithley-dmm6500) below.
- **Film Sense FS-1 in-situ ellipsometer** — see
  [FS-1 ellipsometer](#film-sense-fs-1-ellipsometer) below.
- **XP Glassman FL1.5F1.0 high-voltage plasma supply** on USB (`COM8`) — see
  [Glassman FL supply](#glassman-fl-high-voltage-supply) below.
- **ACCES USB-AO16-8A** — 8-channel analog output board, plugged in and healthy
  on the CyUSB driver since 2026-08-20. **Purpose not established.** It appeared
  the same day as the Glassman and would suit analog programming of it via J1,
  but nothing in this program uses it and Zach has not said what it is for.
- LabVIEW 22.3.1 is installed and was running during discovery.

---

## Two constraints that follow from the above

### 1. DAQmx analog input is exclusive per module

While the LabVIEW VI is running it holds **every** analog-input module. This
program then reports `resource reserved` and shows the affected readings as
failed.

Nothing is damaged by the clash — DAQmx refuses the second request rather than
letting two programs fight over a module. But it does mean **the LabVIEW VI must
be closed before this program can read anything analog.** Bring-up is a handover,
not a side-by-side comparison.

Digital outputs behave the same way, which matters more: do not have both
programs able to drive a valve line.

### 2. The NI 9211 is slow

Roughly 14 samples/second **total**, shared across its four channels. Reading one
thermocouple at 2 Hz is comfortable. Reading all four, plus pressure, plus the
DMM at 1 NPLC, may not keep up.

If the control loop starts falling behind, drop `site.loop_hz` to `1.0` — which is
approximately what the LabVIEW version ran at anyway.

---

## Channel assignments — measured with the LabVIEW VI closed

`python -m tools.discover_hardware --survey-inputs`, 2026-07-30.

### Thermocouples — three connected out of eight

| Channel | Reading | Role |
|---|---|---|
| **`cDAQ1Mod4/ai1`** | **21.1 °C** | **sample stage** — confirmed, see below |
| `cDAQ1Mod2/ai0` | 26.33 °C | connected, unidentified (`aux.tc_a`) |
| `cDAQ1Mod2/ai1` | 24.54 °C | connected, unidentified (`aux.tc_b`) |
| the other five | 2385 °C | open circuit — nothing attached |

2385 °C is the NI 9211's open-circuit full-scale, not a temperature.

**The stage TC was identified by connect-and-compare**, which is worth reusing
for any other channel: survey with the sensor disconnected, connect it, survey
again. `cDAQ1Mod4/ai1` went from 2385 °C to 21.2 °C while every other channel
stayed put. No ambiguity, no guessing.

It is on `Mod4`, not the `Mod2` I had assumed — the two pre-existing
thermocouples are on `Mod2` and the stage is on the other module.

This also settles the old log's **A/B/C/D**: four columns from a four-channel
thermocouple module, not four heater zones.

### Analog voltage — one signal, on `ai3`

| Channel | Reading | Verdict |
|---|---|---|
| `cDAQ2Mod1/ai3` | **2.4718 V**, 15 mV p-p | **the pressure gauge** |
| `cDAQ2Mod1/ai2` | 0.0197 V, 10 mV p-p | marginal — barely above noise, too weak to call |
| `ai0`, `ai1`, `ai4`–`ai7` | ≈0 V, 5 mV p-p | nothing connected |

### MKS mass flow controllers — Modbus TCP

On a dedicated subnet; this PC is `192.168.2.220` on the same NIC that carries
the campus address.

| ID | Gas | Address | Reachable |
|---|---|---|---|
| `ar` | Ar — HCPES | `192.168.2.221:502` | ✅ unit id 1 |
| `h2` | H2 — reactive background | `192.168.2.222:502` | ✅ unit id 1 |
| `n2` | N2 — reactive background | `192.168.2.223:502` | ✅ unit id 1 |

These are **MKS G50** units (product `G_MFC_A_Modbus`), each with its own small
web server. Verified from the devices themselves:

| ID | Gas | **Full scale** | Model | Serial | GCF | Valve |
|---|---|---|---|---|---|---|
| `ar` | 4: Ar | **29 sccm** | GM50A013501RBM020 | 21999844 | 1.39 | N.C. |
| `h2` | 4: H2 | **10 sccm** | GM50A013101RMM020 | 23291683 | 1.00 | N.C. |
| `n2` | 32: N2 | **50 sccm** | GM50A013501RBM020 | 21999843 | 1.00 | N.C. |

All calibrated on N2. All valves normally closed.

#### Full scale comes over HTTP, not Modbus

Flow, temperature and setpoint are read over Modbus (see the register map
below). **Full scale is not in the Modbus map and must come over HTTP.**
`0xC006` is called `Opt_FullScale` and is *not* it — it reads 100.0 on all three
units, whose real full scales are 29 / 10 / 50 sccm.

Each device serves `iobuf.js`, `deviceid.js`, `device_html.js` and `mfc.js` —
plain `name = value;` files behind the web UI. Fetching them is a GET, so it is
read-only, and it is authoritative in a way a guessed register is not:

```
iobuf.flow_sensor = 5.000144      actual flow, sccm
iobuf.setpoint    = 5.000000      commanded setpoint, sccm
iobuf.full_scale  = 29.000000     sccm
iobuf.temp_sensor = 35.78         body temperature
deviceid.over_temp / open_circuit / interface_error    health flags
deviceid.conn_list                who is currently connected
```

**Full scale is read live, every poll, and is never configured.** It changes with
the selected gas — 29 / 10 / 50 sccm across these three — so a hardcoded value
would silently mis-scale every flow number the moment a gas was changed.

#### The Modbus register map — from the device itself

**Each MFC serves its own register table at `http://<host>/modbus.html`.** That
page is the source for every address below (confirmed 2026-08-17):

| I/O | FC (write) | FC (read) | Register | Regs | Unit | Type |
|---|---|---|---|---|---|---|
| Flow | — | **4** | `0x4000` | 2 | sccm | float |
| Temperature | — | 4 | `0x4002` | 2 | degC | float |
| Valve Position | — | 4 | `0x4004` | 2 | 0–100 % | float |
| Flow Hours | — | 4 | `0x4008` | 2 | hrs | int |
| Flow Totalizer | — | 4 | `0x400A` | 2 | sccm | int |
| Flow Set Point | 16 | **3** | `0xA000` | 2 | sccm | float |
| Ramp Rate | 16 | 3 | `0xA002` | 2 | msec | long |
| Unit Type | 16 | 3 | `0xA004` | 2 | — | int |
| Full Modbus Control | 16 | 3 | `0xA006` | 2 | — | int |
| Reset / Open / Close / Flow Zero / En_Opt_in | 5 | 1 | `0xE000`–`0xE004` | 1 | — | int |
| Opt_Kp / Opt_Ki / Opt_Kd / Opt_FullScale | 16 | 3 | `0xC000`/`2`/`4`/`6` | 2 | — | float |

Measured values are **input registers (FC 4)**; settable ones are **holding
registers (FC 3)**.

##### The earlier wrong answer, and why it was wrong

An earlier sweep concluded flow was unreachable over Modbus. Two mistakes, both
instructive:

- It swept with **FC 3 only**. `0x4000` answered "illegal data address" and was
  written off as belonging to another family's map. It is the right address on
  the *wrong function code* — it is an input register.
- It nearly labelled `0xC000` "flow" because that register sat at 0.1 while idle,
  which looks like a zero offset. `0xC000` is **`Opt_Kp`, a PID gain** — which is
  exactly why it stayed at 0.1 while the Ar unit really flowed 5 sccm.

Verified on all three units: FC 4 `0x4000` tracks `iobuf.flow_sensor` to four
decimal places, including sign. Reading is also ~**600× faster** — ~1 ms versus
450–900 ms for one `iobuf.js` GET.

#### Setpoint units — an ambiguity worth knowing about

`0xA000` holds the setpoint in **engineering units (sccm), not percent of full
scale**. With full scale at 29 sccm, a 5 sccm setpoint read back as exactly 5.0;
5 % of 29 would be 1.45.

Note how nearly this was missed: if full scale had been 100 sccm, sccm and percent
would be numerically identical and no reading could have told them apart. Code
assuming percent would have worked by accident on a 100 sccm device and been
wrong on every other one.

#### Writing — confirmed since

Setpoint writes are verified on this hardware: `set_setpoint_sccm` writes
`0xA000` and reads it straight back, and flow follows. There is no write lock —
an early `register_map.confirmed` gate was removed along with every other
software limit (see [CONTROL_MODEL.md](CONTROL_MODEL.md)).

**The one operational catch:** an MKS G50 zeros its setpoint when its Modbus
master disconnects, so commanded flow only holds while this program stays
connected. Stopping the server stops the gas.

### Keithley DMM6500

`USB0::0x05E6::0x6500::04429995::INSTR` —
`KEITHLEY INSTRUMENTS,MODEL DMM6500,04429995,1.0.04b`. In SCPI mode, accepts the
configured setup, and reads ~1.8 µA DC.

### Film Sense FS-1 ellipsometer

In-situ, **read-only**, over a direct link-local Ethernet link —
`169.254.1.1:4001`, confirmed on the wire 2026-08-06. The instrument broadcasts
every dynamic-mode measurement unsolicited on that port at ~1 Hz; this program
subscribes, timestamps each point with the reactor clock, and writes a
per-acquisition sidecar. It **never writes to the instrument** — the trigger
sockets on 4000 and 4010 (how the old LabVIEW program triggered measurements)
are deliberately untouched.

The stream's live thickness is the instrument's uncalibrated fit and is **not**
treated as truth. The point of the sidecar is to put a *refit* file, downloaded
from the FS-1 after a run, back onto the reactor clock — see
[RUN_PROGRAM.md](RUN_PROGRAM.md) and `reactor/analysis/ellipsometer_merge.py`.
Wire format and framing are documented in `reactor/devices/ellipsometer.py`.

Not yet validated across a real deposition (`reactor-nde`).

### Glassman FL high-voltage supply

The plasma supply. **XP Glassman FL1.5F1.0**, rated **1500 V / 1.0 A**, on USB
into its rear-panel **J3**. Firmware revision 02. **Read-only** — the reactor
polls its voltage/current/arc-count monitors at 2 Hz and logs them, and never
commands it; Zach sets levels by hand on the front panel.

Confirmed on the wire 2026-08-21:

```
COM8, 19200 baud, 8N1, address 1
```

**None of that is the documented default and none of it can be read off the DIP
switches** — the manual says 9600 / address 0, every DIP switch on the unit
reads "down", and it answers only at 19200 / address 1. The COM number is not
stable either; a TUSB3410 driver reinstall moved it from COM7 to COM8. If it
goes quiet, sweep the full cross-product of baud rate against address with
`python -m tools.probe_glassman` before suspecting hardware — that exact hole in
a sweep cost a day of misdiagnosis.

Rear panel: **J1** DB-25 analog (interlock jumpered 13 → 25), **J2** RJ45
RS-232/RS-485 (unused, and *not* an Ethernet port despite the socket), **J3**
USB (in use), **J4** empty Ethernet-option mount.

Protocol, scaling, the read-only decision, and the full bring-up account are in
**[GLASSMAN_FL.md](GLASSMAN_FL.md)**. One warning worth repeating here: the
sister-series EJ/ET/EY/FJ/FR manual (102002-177) describes a *different*
product — different connector numbering, different interlock pins, 10-bit
monitors instead of 12-bit, no address byte. Use **102002-168 Rev H**.

### End-to-end read verified

Running `python -m reactor` against the live chamber: pressure, all three
thermocouples, the Baratrons, the DMM and all three MFCs read, and the log files
write correctly.

Note the NI 9201 is 12-bit over ±10 V — about 4.9 mV per count, which on a log
gauge is roughly 1% pressure resolution. Visible as small steps in the log.

---

## The gauge curve — identified

```
P[Torr] = 10 ** (V - 10)          # 1 decade per volt
```

Config preset: `ion_gauge_e10`.

How it was found: at 2.4718 V no preset reproduced the 5.9e-8 Torr the controller
had shown the previous day. Solving for a 1 decade/V curve gave an offset of
−9.70 — *not* a round number, and standard controllers use round ones. That said
the pressure reading was stale, not that the gain was odd. Assuming the round
−10 instead put the chamber at 2.96e-8 Torr, which matched the controller
exactly. The program now reads 3.06e-8 against a controller showing ~3e-8.

**One caveat, worth acting on eventually.** This was matched at a single
pressure, which pins the *offset* but not the *gain* — some other gain with a
compensating offset would fit that one point equally well. The round offset is
strong evidence, not proof.

Next time the chamber is at a very different pressure (mid-dose, or vented),
check the program still agrees with the controller. If the two drift apart at the
far end, the gain needs adjusting, and two (volts, pressure) pairs solve it
exactly.

---

## Identified since (all confirmed 2026-07-31 / 08-01)

- **Valves** — all 11 driven and watched; full line map in `config/reactor.yaml`
  and the `reactor-hardware-inventory` memory. `prec1` (cDAQ1Mod3 line0) is the
  precursor-1 micro-pulse dose valve; `rpm_top` fills the precursor-1 full
  volume — **confirmed by Zach 2026-08-06**; `plasma_ground` (cDAQ1Mod3 line9)
  is the e-beam relay (off = beam on).

  **The relay's resting state is DE-ENERGISED, i.e. `plasma_ground` OFF, i.e.
  the "beam on" sense** — confirmed by Zach 2026-08-21. The relay box is powered
  by a **9 V battery that only drains while the relay is energised**, so leaving
  it energised when the tool is not in use flattens the battery. This is why a
  completed run parks the beam relay off, and why `abort_prestart` de-energises
  it rather than leaving it grounded. It is not a contradiction of "beam off is
  safe": with the HV supply off there is nothing for an un-grounded relay to do,
  which is why both of those paths command HV off in the same breath. Do not
  "fix" this to leave the relay energised at rest.
- **3 Baratrons** (all cDAQ2Mod1, 10 Torr heads, 1 V = 1 Torr): `ai0` = Ar
  Baratron (confirmed rising to 2.72 Torr under 5 sccm Ar); `ai1` = Precursor 1
  dose pressure, `ai2` = Precursor 2 dose pressure — lower reading is
  precursor 1, higher is precursor 2, **confirmed by Zach 2026-08-06**.
- **MFC setpoint writing** — unlocked and verified; the write lock was removed
  with all other software limits. Flow holds only while the program keeps its
  Modbus connection (MFC watchdog zeros it on disconnect).
- **Precursor bubbler thermocouple** — a TC added later, wired on the port
  next to the stage TC. Identified as `cDAQ1Mod4/ai0`, confirmed live reading
  ~32.7 °C on 2026-08-03 (the stage TC on the same module is `ai1`). Shown as
  `aux.bubbler` in the UI and logged.

## Still assumed

| Assumption | Status | How to check |
|---|---|---|
| Gauge **gain** is exactly 1 decade/V | offset confirmed at one pressure; gain not independently confirmed | compare against the controller at a very different pressure |
| TC type is **K** | assumed, most common | ask whoever wired it |
| What `tc_a` / `tc_b` measure | two thermocouples wired and reading; roles unknown | trace them, rename in `aux_inputs` |
| What the NI 9265 current outputs drive | unknown (deferred by operator) | see below |

### Unexplained hardware

The **NI 9265** provides four 0–20 mA analog outputs. With no heater control on
this tool, what these drive is an open question — possibly nothing any more,
possibly an analog setpoint on something (`reactor-5u2`, deferred).

**There is no analog-output path in the code, on purpose.** There used to be an
unreachable one, and it called `add_ao_voltage_chan` — simply wrong for a
current-output module, and a convincing-looking trap for whoever eventually
identifies the 9265. It was removed rather than left to be found. If these
outputs are ever needed, write the path fresh against `add_ao_current_chan`,
add the channels to `config/reactor.yaml`, and populate them in
`Supervisor._build_plan`.
