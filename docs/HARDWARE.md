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

Four Prolific USB-to-serial adapters: `COM3`, `COM4`, `COM5`, `COM6`.
Purpose unknown — candidates are the gauge controller, a pump controller, or the
MFCs if they are serial rather than Ethernet.

### Other

- **Keithley DMM6500** on USB. Not yet enumerated over VISA from this program;
  the resource string still needs capturing.
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

#### Readings come over HTTP, not Modbus

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

#### Why not Modbus for reading — a wrong answer that looked right

A read-only register sweep found only pages `0xA000`–`0xD000`, with `0xA000`
(setpoint), `0xC000`, `0xC002` and `0xC006`. `0x4000` and `0x4004` — the addresses
in the generic MKS documentation — return exception 2, illegal data address. Not
this family's map.

It was tempting to call `0xC000` "flow": it read 0.1 while idle, which looks like
a zero offset. Then the Ar unit was set to 5 sccm and actually flowed 5 sccm —
and `0xC000` and `0xC002` both *stayed* at 0.1. **Neither is flow.** Reading alone
never found the flow register, and a plausible-looking assignment would have
shipped as a wrong number labelled "flow".

#### Setpoint units — an ambiguity worth knowing about

`0xA000` holds the setpoint in **engineering units (sccm), not percent of full
scale**. With full scale at 29 sccm, a 5 sccm setpoint read back as exactly 5.0;
5 % of 29 would be 1.45.

Note how nearly this was missed: if full scale had been 100 sccm, sccm and percent
would be numerically identical and no reading could have told them apart. Code
assuming percent would have worked by accident on a 100 sccm device and been
wrong on every other one.

#### Still to do

Writes remain **locked** (`register_map.confirmed: false`). Reading is fully
verified; no setpoint write has been round-tripped on this hardware yet.

The safe way to confirm it: write an MFC's **current** setpoint back to itself.
That is a no-op in process terms but exercises the whole write-and-verify path.
It still needs a deliberate go-ahead, because it is a write to a live gas
controller.

### Keithley DMM6500

`USB0::0x05E6::0x6500::04429995::INSTR` —
`KEITHLEY INSTRUMENTS,MODEL DMM6500,04429995,1.0.04b`. In SCPI mode, accepts the
configured setup, and reads ~1.8 µA DC.

### End-to-end read verified

Running `python -m reactor` against the live chamber: pressure, both
thermocouples and the DMM all read, at 2 Hz, and both log files write correctly.
Only the MFC fails to connect (placeholder IP).

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
  precursor-1 micro-pulse dose valve; `rpm_top` fills the precursor-1 full volume;
  `plasma_ground` (cDAQ1Mod3 line9) is the e-beam relay (off = beam on).
- **3 Baratrons** (all cDAQ2Mod1, 10 Torr heads, 1 V = 1 Torr): `ai0` = Ar
  Baratron (confirmed rising to 2.72 Torr under 5 sccm Ar); `ai1` = Precursor 1
  dose pressure, `ai2` = Precursor 2 dose pressure (labelled "lower reading =
  prec1, higher = prec2" — to verify in lab).
- **MFC setpoint writing** — unlocked and verified; the write lock was removed
  with all other software limits. Flow holds only while the program keeps its
  Modbus connection (MFC watchdog zeros it on disconnect).

## Still assumed

| Assumption | Status | How to check |
|---|---|---|
| Which precursor Baratron is which | ai1→prec1, ai2→prec2 by the lower/higher rule | verify in the lab; swap the two channels in `gauges:` if reversed |
| Fill valve is `rpm_top` (vs `rpm_bottom`) | operator thinks top; "switch if wrong" | run the ALD fill and watch which manifold valve charges the volume |
| Gauge **gain** is exactly 1 decade/V | offset confirmed at one pressure; gain not independently confirmed | compare against the controller at a very different pressure |
| TC type is **K** | assumed, most common | ask whoever wired it |
| What `tc_a` / `tc_b` measure | two thermocouples wired and reading; roles unknown | trace them, rename in `aux_inputs` |
| What the NI 9265 current outputs drive | unknown (deferred by operator) | see below |

### Unexplained hardware

The **NI 9265** provides four 0–20 mA analog outputs. With no heater control on
this tool, what these drive is an open question — possibly nothing any more,
possibly an analog setpoint on something.

Worth noting for whenever it is identified: the code's analog-output path calls
`add_ao_voltage_chan`, which is wrong for a 9265. A current-output module needs
`add_ao_current_chan`. Nothing configures an analog output today, so this is a
note rather than a bug — but it needs fixing before the 9265 is ever used.
