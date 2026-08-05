# What the old LabVIEW program tells us — and what it doesn't

`LV Prog Main Zach.vi`, LabVIEW 22.3.1, saved 2023-11-29.

**Read this before mining the VI for anything.** It is an inherited program with
a lot of dead code, and treating it as a specification produces confident,
wrong answers. It did exactly that to me — the record of how is at the bottom.

For what the tool actually has, see [HARDWARE.md](HARDWARE.md).

---

## Reliable: the interface types

These come from the VI's linker/dependency records and its DLL imports, which are
hard evidence about which libraries the program was built against.

### MKS mass flow controllers, Modbus TCP

Driver: `MKS modbus Driver Library V2_0 BETA RELEASE/MKS LV14`. SubVIs actually
called by the main VI:

| VI | Purpose |
|---|---|
| `MB ENet GoOnline` / `GoOffline` | open / close the TCP session |
| `MB Get Device Info` | identify the device |
| `MB MFC Flow` | **read** actual flow |
| `MB MFC Valve Position` | **read** valve drive |
| `MB MFC Setpoint+` | **write** flow setpoint |

Plain Modbus/TCP on port 502, one unit ID per MFC. Ports directly to `pymodbus`.

### NI-DAQmx

DAQmx palette calls present: analog-input voltage, analog-input thermocouple,
analog-output voltage, digital-output single line, plus task plumbing. Confirmed
at the DLL layer by `nilvaiu.dll` imports `DAQRead1Chan1SampF64`,
`DAQReadNChan1Samp1DF64`, `DAQWrite1Chan1SampF64`,
`DAQWrite1Chan1Samp1LineBool`.

So: analog in, analog out, digital out. **Which channels, and what is attached to
them, the VI does not reliably say.**

### Program structure

A state machine driven by enum type-defs (`Main States.ctl`, `Main Enum.ctl`,
`Deposition Enum.ctl`, `Timer enum.ctl`, `MFC enum.ctl`, `Zone states.ctl`,
`precursor valve enum.ctl`), with shared state in **functional global
variables** — `System Status FGV.vi`, `Timer FGV.vi`, `Timer MFC FGV.vi`,
`Global Exit Variable.vi`.

Loop timing came from the `subTimeDelay.vi` Express VI configured at 1 s, in the
same loop that drew the front panel.

### Why it was hard to work on

Three structural causes, independent of what the reactor is:

1. **FGVs are global mutable state.** Any subVI can change system status from
   anywhere, so there is no single owner of "what the tool is doing". Stale reads
   and races are the design, not findable defects.
2. **Timing welded to the UI loop.** A 1 s delay in the loop that redraws the
   front panel means step timing drifts whenever the UI is busy.
3. **Addresses buried in the block diagram.** Changing a channel or an IP means
   editing wires.

The replacement fixes these three specifically: one `Supervisor` owns state,
recipe timing runs on its own task off absolute deadlines, and every address
lives in `config/reactor.yaml`.

---

## Unreliable: everything about the process

The VI contains substantial dead code. Two confirmed vestiges:

- **`QCM Mass`** — a column in every log file. There is no QCM on this tool.
- **Heater control** — `Heaters Status.vi`, `RTD Monitor Heater On.vi`,
  `Tc Monitor Heater off.vi`, `Zone states.ctl`, and an NI PID autotune VI. There
  is no heater control on this tool. One thermocouple measures the sample stage,
  and nothing closes a loop on it.

Once two major subsystems in a program are dead, the rest of it cannot be used to
infer what the tool does. The log header `Time / Pressure / QCM Mass / A / B / C / D`
is a good illustration: one column is confirmed dead, and the current best guess
for A/B/C/D comes from the *hardware* (two 4-channel thermocouple modules
exist), not from the VI.

---

## Correction log

Kept deliberately, because the failure mode is worth remembering.

**I concluded this was a hot-wall viscous-flow ALD reactor with four heater
zones.** It is a UHV chamber with no heater control.

How that happened:

1. I read the project folder name `Rev4/7364.George.CIRES.DARPA GaN` and inferred
   a research group and a tool type. That is a guess about a directory name. I
   then wrote it into the analysis as a finding.
2. I took `Zone states.ctl` + `RTD Monitor Heater On.vi` + four log columns as
   evidence of four controlled heater zones. They are consistent with that, and
   also consistent with dead code plus a four-channel TC module.
3. The pressure reading of 5.9e-8 Torr flatly contradicts viscous flow. I noticed
   it, wrote a note that a log-scale gauge was needed, and did not revisit the
   conclusion it disproved.
4. I then built a simulator with invented numbers — a growth-per-cycle, a heater
   time constant, a pressure/flow coefficient — and showed screenshots of it
   working. None of those numbers were knowledge. A validated-looking screenshot
   of invented physics is worse than no screenshot, because it looks like
   evidence.

The general lesson, now applied throughout: **the tool is the authority, not the
old code.** `tools/discover_hardware.py` queries real hardware and is read-only;
everything it cannot answer is marked unconfirmed in the config and in
[HARDWARE.md](HARDWARE.md) rather than filled in with a plausible value.
