"""Bench instruments over VISA (USB/LAN/GPIB) or raw serial.

Currently: the Keithley DMM6500 measuring current, connected by USB.

Scope of what this does to an instrument:

* `connect()` opens the session and sends `*IDN?` (a read).
* It then sends the `setup` commands from the config, in order, once. Those are
  the only commands that change instrument state, and they live in
  `config/reactor.yaml` where you can see and edit them - not buried here. Leave
  `setup` empty and this driver becomes strictly read-only: it will just read
  whatever function you dialled in by hand on the front panel.
* Each poll sends `query` and parses one number.

There is no other write path. Nothing resets, self-tests, or zeroes the
instrument.
"""

from __future__ import annotations

import asyncio

from ..config import InstrumentCfg
from .base import Device, Reading

def return_to_local(inst) -> str:
    """Take a VISA instrument out of remote mode. Returns what worked, for logging.

    Under SCPI control a DMM6500 stops free-running: it measures only when the
    host sends a read command, so the front panel sits on the last triggered
    value and a healthy meter looks frozen.

    Do NOT do this with `:SYSTem:LOCal`. That is not a valid SCPI header on the
    DMM6500 over USB - it is rejected with -113 and lights the error annunciator,
    which is worse than the problem it was meant to solve. Going to local is a
    USBTMC/GPIB *interface* message (GTL), not a SCPI command.
    """
    try:
        from pyvisa import constants

        inst.control_ren(constants.RENLineOperation.address_gtl)
        return "GTL"
    except Exception:
        pass
    # Some backends expose REN deassert but not GTL.
    try:
        from pyvisa import constants

        inst.control_ren(constants.RENLineOperation.deassert)
        return "REN deassert"
    except Exception:
        return ""


#: Sensible starting setup for a DMM6500 measuring DC current. Copy into the
#: config's `setup:` list rather than relying on a default, so what gets sent to
#: your instrument is always visible in the config file.
DMM6500_DC_CURRENT = [
    ':SENS:FUNC "CURR:DC"',
    ":SENS:CURR:RANG:AUTO ON",
    ":SENS:CURR:NPLC 1",
    ":SENS:CURR:AZER ON",
]


class ScpiInstrument(Device):
    def __init__(self, cfg: InstrumentCfg) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self._rm = None
        self._inst = None
        self._is_serial = False
        self.identity = ""
        self.setup_sent: list[str] = []
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(self) -> None:
        await asyncio.to_thread(self._open)
        self.connected = True
        self.last_error = ""

    def _open(self) -> None:
        res = self.cfg.resource.strip()
        if not res:
            raise ValueError(f"{self.id}: no resource string configured")

        # A bare COM port with no VISA layer available -> raw serial.
        if res.upper().startswith("COM"):
            try:
                import pyvisa

                self._rm = pyvisa.ResourceManager()
                self._inst = self._rm.open_resource(f"ASRL{res[3:]}::INSTR",
                                                    open_timeout=3000)
            except Exception:
                import serial

                self._inst = serial.Serial(res, self.cfg.baud, timeout=3.0)
                self._is_serial = True
        else:
            import pyvisa

            self._rm = pyvisa.ResourceManager()
            self._inst = self._rm.open_resource(res, open_timeout=3000)

        if not self._is_serial:
            try:
                self._inst.timeout = 5000            # ms; NPLC 1 reads are slow
                self._inst.read_termination = "\n"
                self._inst.write_termination = "\n"
                if res.upper().startswith("ASRL"):
                    self._inst.baud_rate = self.cfg.baud
            except Exception:
                pass

        try:
            self.identity = self._query("*IDN?")
        except Exception as exc:
            self.identity = f"(no *IDN? response: {type(exc).__name__})"

        if self.cfg.driver == "keithley_dmm6500" and "DMM6500" not in self.identity.upper():
            # Not fatal - could be a DMM6500 in TSP mode, or a different model
            # wired the same way. Surface it rather than silently mismeasuring.
            self.last_error = (
                f"expected a DMM6500 but *IDN? said: {self.identity!r}. "
                "If this is a DMM6500, check it is in SCPI command-set mode "
                "(front panel: Menu > System > Settings > Command Set)."
            )

        for cmd in self.cfg.setup:
            self._write(cmd)
            self.setup_sent.append(cmd)
        if self.cfg.setup:
            self._check_errors()

    async def disconnect(self) -> None:
        """Hand the front panel back, then close the session.

        Measurement settings are left exactly as configured - only the
        remote/local state is restored.
        """
        await asyncio.to_thread(self._close)
        self.connected = False

    def _close(self) -> None:
        self._return_to_local()
        self._drop()

    def _drop(self) -> None:
        """Close and discard the session handles without touching remote/local.

        Used when a read fails (the instrument was powered off or unplugged) so
        the next reconnect opens a clean session instead of reusing a dead one.
        """
        for obj in (self._inst, self._rm):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._inst = None
        self._rm = None

    def _return_to_local(self) -> None:
        if self._inst is None or self._is_serial:
            return
        return_to_local(self._inst)

    # -- io ----------------------------------------------------------------- #

    def _write(self, text: str) -> None:
        if self._inst is None:
            raise ConnectionError(f"{self.id}: not connected")
        if self._is_serial:
            self._inst.write((text + "\n").encode())
        else:
            self._inst.write(text)

    def _query(self, text: str) -> str:
        if self._inst is None:
            raise ConnectionError(f"{self.id}: not connected")
        if self._is_serial:
            self._inst.write((text + "\n").encode())
            return self._inst.readline().decode(errors="ignore").strip()
        return str(self._inst.query(text)).strip()

    def _check_errors(self) -> None:
        """Drain the SCPI error queue so a bad setup command is not silent."""
        try:
            for _ in range(8):
                resp = self._query(":SYST:ERR?")
                if not resp:
                    return
                code = resp.split(",")[0].strip().lstrip("+")
                if code in ("0", ""):
                    return
                self.last_error = f"instrument reported: {resp}"
        except Exception:
            pass

    async def read(self) -> list[Reading]:
        key = f"inst.{self.id}"
        if not self.connected or not self.cfg.query:
            return [self._bad(key, self.cfg.unit, "not connected")]
        async with self._lock:
            try:
                raw = await asyncio.to_thread(self._query, self.cfg.query)
            except Exception as exc:
                # The instrument stopped answering (powered off / unplugged).
                # Mark it disconnected and drop the dead session so the
                # supervisor's reconnect loop can reopen it when it returns.
                self.connected = False
                self._drop()
                return [self._bad(key, self.cfg.unit, exc)]

        # DMM6500 can return several comma-separated values depending on the
        # configured read format; the reading is the first.
        try:
            value = float(raw.split(",")[0])
        except ValueError:
            return [self._bad(key, self.cfg.unit, f"unparseable reading {raw!r}")]

        if abs(value) >= self.cfg.overload_above:
            return [self._bad(key, self.cfg.unit,
                              f"overload / no valid reading ({value:g})")]
        self.last_error = ""
        return [Reading(key=key, value=value, unit=self.cfg.unit)]

    def status(self) -> dict:
        return {
            **super().status(),
            "resource": self.cfg.resource,
            "identity": self.identity,
            "query": self.cfg.query,
            "setup_sent": self.setup_sent,
            "unit": self.cfg.unit,
        }
