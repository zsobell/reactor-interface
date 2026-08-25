"""Keithley 2260B programmable DC power supply, SCPI over its USB serial port.

Four of these drive the reactor:

    role               model          serial    rated
    Stage Bias         2260B-250-4    1412016   250 V / 4.5 A, 360 W
    Steering Coils     2260B-80-13    1408023    80 V / 13.5 A, 360 W
    Grid Bias          2260B-800-1    1407084   800 V / 1.44 A, 360 W
    Collimating Coils  2260B-250-9    1405224   250 V / 9 A, 720 W

WHAT THIS DRIVER COMMANDS
=========================
`:OUTP ON` / `:OUTP OFF`, `:SOUR:VOLT` and `:SOUR:CURR`. All four are reachable
from the Hardware tab, and two of them also fire automatically.

**Automatically** (operator-requested 2026-08-21):

* All four outputs come ON at pre-start and go OFF when a run ends, aborts or
  is stopped. They are NOT touched by plasma events: they stay on across a
  reignite and across the whole run. Zach's reason, worth keeping: the
  collimating coil in particular is what keeps the plasma stable when the beam
  dump is grounded, so cycling it with the beam would be actively harmful.
* The sample-bias unit's VOLTAGE is set from the run's sample-bias field, and
  only when that field is non-zero.

**Manually**, from the Hardware-tab card (operator-requested 2026-08-25): a
voltage field, a current field and an output toggle per supply.

Note this supersedes an earlier rule. Until 2026-08-25 this driver deliberately
never touched a **current limit** - the four supplies were found with Zach's
working setpoints dialled in (20 V/0.5 A, 30.07 V/3.7 A, 100 V/0.2 A,
150 V/2.5 A) and those were his alone to set. He has since asked for current
fields on the Hardware tab, so `set_current` exists. Nothing sets a current
*automatically*; it happens only when the operator submits the field.

TRANSPORT - NOT USBTMC
======================
Unlike the DMM6500, the 2260B's USB port is a **USB CDC virtual COM port**
(`USB\\VID_05E6&PID_2260`, "USB Serial Device"). Windows binds it with the
in-box `usbser` driver, no install needed. It still speaks SCPI, just over a
serial link, so VISA sees it as `ASRL<n>::INSTR` rather than `USB0::...::INSTR`.

**Ports are located by USB SERIAL NUMBER, not by COM number.** The serial is in
the device's hardware ID (`SER=1412016`), so four otherwise-identical supplies
are self-identifying and survive Windows renumbering them. Config carries the
serial; `find_port` resolves it at connect. Pinning a COM number instead is how
you end up biasing the sample from the grid supply after a reboot.

`connect()` also verifies the serial reported in `*IDN?` matches the configured
one and refuses the device if not - four near-identical supplies on one rack is
exactly the situation where a silent mix-up does real damage.

POLLING
=======
One compound query per tick - see POLL_QUERY below for what it asks and what
it costs. Four supplies polled **concurrently** cost ~77 ms wall-clock; polled
sequentially the same queries cost well over 200 ms. `Supervisor._cycle`
gathers them, so the concurrency is already there - do not turn that into a
loop.

NO REMOTE/LOCAL HANDOFF
=======================
The DMM6500 needs a USBTMC go-to-local on disconnect or its front panel sits
frozen. **The 2260B has no equivalent**: `:SYST:LOC`, `:SYST:LOCal`, `:SYST:REM`
and `:SYST:RWL` are all rejected with -113 "Undefined header" (checked on the
hardware). There is nothing to hand back, so `disconnect()` just closes the
port. Do not copy `instrument.return_to_local` here.
"""

from __future__ import annotations

import asyncio
import time

from ..config import PowerSupplyCfg
from .base import Device, Reading

#: One round-trip for everything the UI and the log need: measured V and I, the
#: output state, the questionable-status register, both setpoints, and the
#: CV/CC operating mode.
#:
#: Measured on the hardware, all four supplies polled concurrently: this costs
#: ~77 ms against ~45 ms for the four-value version that omitted the setpoints
#: and mode. Roughly 10 ms per extra value, all of it USB-CDC round-trip
#: latency. That is 15% of the 500 ms budget at site.loop_hz = 2 Hz. The
#: setpoints are in here rather than on a slower refresh so the Hardware-tab
#: readback does not lag behind a Set the operator just made, or behind someone
#: turning the knob on the front panel.
POLL_QUERY = (":MEAS:VOLT?;:MEAS:CURR?;:OUTP?;:STAT:QUES:COND?"
              ";:SOUR:VOLT?;:SOUR:CURR?")

#: How close a measurement has to sit to its setpoint to count as regulating
#: there. Zach: the supplies "sometimes drift a fraction of a %", so 1% is
#: comfortably outside the noise while still well inside the gap between a
#: regulated value and an unregulated one.
MODE_TOLERANCE = 0.01

#: CV/CC is DERIVED, not read from a register.
#:
#: `:OUTP:MODE?` was tried first and is wrong: it returns 0 on all four
#: supplies regardless of state, including coils demonstrably running in
#: constant current, so everything showed "CV" (reported 2026-08-25). It is
#: accepted by the instrument but evidently means something other than the
#: present operating mode. `:STAT:QUES:COND?` reads 0 in every state seen so
#: far too, so its CV/CC bits - if it has any - are not usable either.
#:
#: What IS unambiguous is the supply's own behaviour: a single-quadrant supply
#: regulates whichever limit it has reached. If the measured current is sitting
#: at the current setpoint it is in CC; if the measured voltage is sitting at
#: the voltage setpoint it is in CV. That needs no vendor decoding and is
#: self-evidently right, which a guessed register bit was not.


class KeithleyProtocolError(RuntimeError):
    """A reply arrived but could not be parsed, or the supply reported an error."""


def find_port(usb_serial: str) -> str | None:
    """COM port for the 2260B with this USB serial number, or None.

    Matches on the serial embedded in the hardware ID rather than on the port
    number, which Windows reassigns freely.
    """
    from serial.tools import list_ports

    want = (usb_serial or "").strip().upper()
    if not want:
        return None
    for p in list_ports.comports():
        hwid = (p.hwid or "").upper()
        if "VID:PID=05E6:2260" not in hwid and "VID_05E6&PID_2260" not in hwid:
            continue
        for tok in hwid.split():
            if tok.startswith("SER=") and tok[4:] == want:
                return p.device
        # Some pyserial backends expose it as an attribute instead of in hwid.
        if (getattr(p, "serial_number", "") or "").upper() == want:
            return p.device
    return None


def parse_idn(idn: str) -> tuple[str, str]:
    """('2260B-250-4', '1412016') from a *IDN? string. Empty strings if odd."""
    parts = [f.strip() for f in (idn or "").split(",")]
    if len(parts) < 3:
        return "", ""
    model = parts[1]
    if model.upper().startswith("MODEL "):
        model = model[6:].strip()
    return model, parts[2]


class Keithley2260B(Device):
    """One 2260B on its USB serial port."""

    #: Snapshot/column namespace. The Glassman uses "hv"; these are not high
    #: voltage in the same sense and are logged separately.
    key_prefix = "psu"

    def __init__(self, cfg: PowerSupplyCfg) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self._ser = None
        self.port = ""
        self.identity = ""
        self.model = ""
        self.serial_number = ""
        self.max_voltage: float | None = None
        self.max_current: float | None = None
        #: Lead-orientation bookkeeping for the sample-bias unit: +1 or -1.
        #: The 2260B is single-quadrant and cannot source a negative voltage,
        #: so this records which way Zach ran the leads onto the stage and
        #: signs the LOGGED voltage. It never changes what is commanded.
        self.polarity = 1
        self.last_voltage: float | None = None
        self.last_current: float | None = None
        self.output_on: bool | None = None
        self.questionable = 0
        #: Device-reported setpoints, refreshed every poll so the UI readback
        #: tracks front-panel changes as well as ones this program made.
        self.voltage_setpoint: float | None = None
        self.current_setpoint: float | None = None
        self._lock = asyncio.Lock()

    def mode_label(self) -> str | None:
        """"CV" / "CC" / None - derived from measurement vs setpoint.

        None while the output is off (the mode has no meaning then), and also
        None when neither limit has been reached, which is what an unloaded or
        settling output looks like. Saying nothing is better than guessing: the
        previous register-based version confidently labelled everything CV.
        """
        if not self.output_on:
            return None
        v, i = self.last_voltage, self.last_current
        v_set, i_set = self.voltage_setpoint, self.current_setpoint
        if v is None or i is None or v_set is None or i_set is None:
            return None

        # `last_voltage` carries the sample-bias polarity sign; compare
        # magnitudes, since the supply only ever sources positive.
        def reached(meas: float, setpoint: float) -> float | None:
            """How far below its setpoint a value is sitting, as a fraction.
            None when the setpoint is ~0, where the test is meaningless."""
            if setpoint <= 1e-9:
                return None
            return (setpoint - abs(meas)) / setpoint

        dv, di = reached(v, v_set), reached(i, i_set)
        at_v = dv is not None and dv <= MODE_TOLERANCE
        at_i = di is not None and di <= MODE_TOLERANCE
        if at_v and at_i:
            # Right at the knee - report whichever is regulating more tightly.
            return "CV" if dv <= di else "CC"
        if at_v:
            return "CV"
        if at_i:
            return "CC"
        return None

    def log_channels(self) -> dict[str, str]:
        """{run-export column suffix: snapshot key suffix}."""
        return {"voltage": "voltage", "current": "current"}

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(self) -> None:
        await asyncio.to_thread(self._open)
        self.connected = True
        self.last_error = ""

    def _open(self) -> None:
        import serial

        port = self.cfg.port.strip() or find_port(self.cfg.usb_serial)
        if not port:
            raise ConnectionError(
                f"{self.id}: no 2260B with USB serial "
                f"{self.cfg.usb_serial!r} is connected")

        # write_timeout is mandatory on Windows: pyserial maps an unset value
        # to "wait forever", so a write to an unplugged port would hang the
        # poll loop instead of raising.
        self._ser = serial.Serial(
            port, self.cfg.baud, bytesize=8, parity="N", stopbits=1,
            timeout=self.cfg.timeout_s, write_timeout=self.cfg.timeout_s)
        self.port = port
        time.sleep(0.1)                 # let the CDC port settle
        self._ser.reset_input_buffer()

        self.identity = self._query("*IDN?")
        self.model, self.serial_number = parse_idn(self.identity)

        # Four near-identical supplies on one rack: refuse a mismatch rather
        # than quietly biasing the sample from the grid supply.
        want = (self.cfg.usb_serial or "").strip()
        if want and self.serial_number and self.serial_number != want:
            raise ConnectionError(
                f"{self.id}: expected serial {want} but {port} reports "
                f"{self.serial_number} ({self.model}). Refusing - check which "
                f"supply is on which USB port.")

        # Rated maxima from the instrument, never from the model name: the
        # suffix is not the current ('-4' is 4.5 A, '-1' is 1.44 A), and the
        # supply reports 105% of nameplate as its programmable maximum.
        for attr, q in (("max_voltage", ":SOUR:VOLT? MAX"),
                        ("max_current", ":SOUR:CURR? MAX")):
            try:
                setattr(self, attr, float(self._query(q)))
            except Exception:
                setattr(self, attr, None)

    async def disconnect(self) -> None:
        """Close the port.

        Commands nothing. In particular it does NOT switch the output off -
        that belongs to run teardown (Supervisor.supplies_output_off), not to
        losing a serial handle, and a server restart must not drop the
        collimating coil out from under a running plasma. And there is no
        remote/local handoff to do: see the module docstring.
        """
        await asyncio.to_thread(self._close)
        self.connected = False

    def _close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def _drop(self) -> None:
        self._close()
        self.connected = False

    # -- io ----------------------------------------------------------------- #

    def _query(self, cmd: str) -> str:
        if self._ser is None:
            raise ConnectionError(f"{self.id}: not connected")
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\n").encode())
        raw = self._ser.read_until(b"\n")
        if not raw:
            raise KeithleyProtocolError(f"{self.id}: no reply to {cmd!r}")
        return raw.decode(errors="replace").strip()

    def _write(self, cmd: str) -> None:
        if self._ser is None:
            raise ConnectionError(f"{self.id}: not connected")
        self._ser.write((cmd + "\n").encode())

    def _write_checked(self, cmd: str) -> None:
        """Send a command and read the error queue, so a rejected command is
        not silent. The 2260B answers :SYST:ERR? with '+0,"No error"'."""
        self._write(cmd)
        err = self._query(":SYST:ERR?")
        code = err.split(",")[0].strip().lstrip("+")
        if code not in ("0", ""):
            raise KeithleyProtocolError(f"{self.id}: {cmd!r} rejected: {err}")

    async def _talk(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # -- data --------------------------------------------------------------- #

    async def read(self) -> list[Reading]:
        """One compound poll. Never raises; failures come back ok=False."""
        p = self.key_prefix
        vkey, ikey = f"{p}.{self.id}.voltage", f"{p}.{self.id}.current"

        if not self.connected:
            return [self._bad(vkey, "V", "not connected"),
                    self._bad(ikey, "A", "not connected")]
        try:
            raw = await self._talk(self._query, POLL_QUERY)
        except Exception as exc:
            self._drop()
            return [self._bad(vkey, "V", exc), self._bad(ikey, "A", exc)]

        fields = raw.split(";")
        if len(fields) < 3:
            detail = f"unparseable poll reply {raw!r}"
            return [self._bad(vkey, "V", detail), self._bad(ikey, "A", detail)]
        try:
            volts = float(fields[0])
            amps = float(fields[1])
            self.output_on = fields[2].strip() not in ("0", "OFF", "")
            self.questionable = int(float(fields[3])) if len(fields) > 3 else 0
            if len(fields) > 5:
                self.voltage_setpoint = float(fields[4])
                self.current_setpoint = float(fields[5])
        except ValueError as exc:
            detail = f"unparseable poll reply {raw!r}: {exc}"
            return [self._bad(vkey, "V", detail), self._bad(ikey, "A", detail)]

        # Signed by the lead orientation, so the log records the bias as it is
        # actually applied to the stage. The supply itself only ever sources
        # positive - see self.polarity.
        volts *= (-1 if self.polarity < 0 else 1)
        self.last_voltage, self.last_current = volts, amps
        self.last_error = ""
        return [Reading(key=vkey, value=volts, unit="V"),
                Reading(key=ikey, value=amps, unit="A")]

    # -- control ------------------------------------------------------------ #

    async def set_output(self, on: bool) -> None:
        """Enable or disable the output. Leaves every setpoint alone."""
        await self._talk(self._write_checked, f":OUTP {1 if on else 0}")
        self.output_on = bool(on)

    async def set_voltage(self, volts: float) -> None:
        """Set the voltage program.

        `volts` is a MAGNITUDE - the supply is single-quadrant and cannot source
        a negative voltage. On the sample-bias unit, lead orientation is tracked
        by `self.polarity` and applied to the logged value, never to what is
        commanded here.

        Clamped to the supply's own reported maximum. That clamp is the
        hardware's rating, not a policy limit: this program has no opinion about
        what level Zach should run.
        """
        v = abs(float(volts))
        if self.max_voltage is not None:
            v = min(v, self.max_voltage)
        await self._talk(self._write_checked, f":SOUR:VOLT {v:.3f}")

    async def set_current(self, amps: float) -> None:
        """Set the current limit.

        Only ever called from the operator's Hardware-tab field - nothing sets a
        current automatically. Clamped to the supply's own reported maximum,
        which is the hardware rating rather than a policy limit.
        """
        a = abs(float(amps))
        if self.max_current is not None:
            a = min(a, self.max_current)
        await self._talk(self._write_checked, f":SOUR:CURR {a:.3f}")

    async def read_setpoints(self) -> tuple[float | None, float | None]:
        """(voltage program, current limit). A read; safe at any time."""
        try:
            raw = await self._talk(self._query, ":SOUR:VOLT?;:SOUR:CURR?")
            v, i = raw.split(";")[:2]
            return float(v), float(i)
        except Exception:
            return None, None

    # -- status ------------------------------------------------------------- #

    def status(self) -> dict:
        return {
            **super().status(),
            "driver": self.cfg.driver,
            "kind": "keithley_2260b",
            "model": self.model or self.cfg.model,
            "port": self.port or self.cfg.port,
            "usb_serial": self.serial_number or self.cfg.usb_serial,
            "identity": self.identity,
            "max_voltage": self.max_voltage,
            "max_current": self.max_current,
            "voltage": self.last_voltage,
            "current": self.last_current,
            "voltage_setpoint": self.voltage_setpoint,
            "current_setpoint": self.current_setpoint,
            "output_on": self.output_on,
            # "CV" / "CC" / None, derived from measurement vs setpoint - see
            # mode_label(). None means "not claiming one", which covers the
            # output being off and the output not having reached either limit.
            "mode": self.mode_label(),
            "polarity": self.polarity,
            "is_sample_bias": self.cfg.sample_bias,
            "prestart_output": self.cfg.prestart_output,
            # Non-zero means the supply has flagged something (OVP/OCP/OTP or a
            # fan fault). The bit map is not documented in anything we have, so
            # the raw value is surfaced rather than guessed at - check the front
            # panel when it is non-zero.
            "questionable": self.questionable,
        }
