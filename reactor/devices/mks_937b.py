"""Read-only MKS 937B pressure channel over RS232.

The 937B protocol distinguishes queries (``?``) from setters (``!``). This
driver emits only queries, never gauge power, degas, relay or setup commands.
"""

from __future__ import annotations

import asyncio
import math
import re

from ..config import InstrumentCfg
from .base import Device, Reading


_RESPONSE = re.compile(r"@(?P<address>\d{3})(?P<kind>ACK|NAK)(?P<body>[^;]*);FF")


def parse_response(raw: bytes, address: int) -> str:
    """Require a complete, addressed ACK; status text is handled by read()."""
    match = _RESPONSE.fullmatch(raw.decode("ascii", errors="replace"))
    if not match or int(match["address"]) != address:
        raise ValueError(f"invalid 937B response {raw!r}")
    if match["kind"] != "ACK":
        raise ValueError(f"937B rejected query: {match['body']}")
    return match["body"].strip()


def parse_pressure(body: str) -> float:
    """The manual's OFF/WAIT/LO/etc. tokens are not pressure measurements."""
    try:
        value = float(body)
    except ValueError as exc:
        raise ValueError(f"937B channel status: {body or 'empty response'}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"invalid 937B pressure: {body}")
    return value


class Mks937B(Device):
    def __init__(self, cfg: InstrumentCfg) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self.identity = "MKS 937B"
        self.setup_sent: list[str] = []
        self._ser = None
        self._lock = asyncio.Lock()

    def _drop(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def _query(self, command: str) -> str:
        if self._ser is None:
            raise ConnectionError("937B serial port is closed")
        # No CR/LF: the manual uses ;FF as the frame terminator.
        frame = f"@{self.cfg.address:03d}{command}?;FF".encode("ascii")
        self._ser.reset_input_buffer()
        self._ser.write(frame)
        self._ser.flush()
        raw = self._ser.read_until(b";FF", 128)
        if not raw:
            raise TimeoutError(f"937B did not answer {command} query")
        return parse_response(raw, self.cfg.address)

    def _open(self) -> None:
        import serial

        # Construct unopened so RTS/DTR can be kept low from the first open.
        ser = serial.Serial(port=None, baudrate=self.cfg.baud, timeout=0.25,
                            write_timeout=0.25)
        ser.rts = False
        ser.dtr = False
        ser.port = self.cfg.resource
        try:
            ser.open()
            self._ser = ser
            units = self._query("U").upper()
            if units != "TORR":
                raise ValueError(f"937B reports {units!r}, expected Torr")
        except BaseException:
            self._drop()
            raise

    async def connect(self) -> None:
        await asyncio.to_thread(self._open)
        self.connected = True
        self.last_error = ""

    async def disconnect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._drop)
            self.connected = False

    async def read(self) -> list[Reading]:
        key = f"inst.{self.id}"
        if not self.connected:
            return [self._bad(key, self.cfg.unit, "not connected")]
        async with self._lock:
            try:
                body = await asyncio.to_thread(self._query, self.cfg.query)
            except (OSError, ConnectionError, TimeoutError) as exc:
                self.connected = False
                await asyncio.to_thread(self._drop)
                return [self._bad(key, self.cfg.unit, exc)]
            except ValueError as exc:
                return [self._bad(key, self.cfg.unit, exc)]
        try:
            value = parse_pressure(body)
        except ValueError as exc:
            return [self._bad(key, self.cfg.unit, exc)]
        self.last_error = ""
        return [Reading(key=key, value=value, unit=self.cfg.unit)]

    def status(self) -> dict:
        return {**super().status(), "driver": self.cfg.driver,
                "resource": self.cfg.resource, "identity": self.identity,
                "query": self.cfg.query, "unit": self.cfg.unit,
                "setup_sent": self.setup_sent}
