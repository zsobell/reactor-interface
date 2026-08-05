"""MKS G50 mass flow controllers.

Reads over the device's own HTTP interface; writes the setpoint over Modbus TCP.

--------------------------------------------------------------------------
  WHY HTTP FOR READING
--------------------------------------------------------------------------
These are MKS G50 units (`GM50A...`, product `G_MFC_A_Modbus`). Each serves a
small web UI whose data comes from plain JavaScript files - `iobuf.js`,
`deviceid.js`, `device_html.js`, `mfc.js` - that are just `name = value;` lines.
Fetching them is a GET: read-only, and authoritative in a way a guessed register
is not.

That matters because of what a register sweep got wrong here. Scanning found
`0xA000` (setpoint), `0xC000`, `0xC002` and `0xC006`, and it was tempting to call
`0xC000` "flow". Then the Ar unit was set to 5 sccm and actually flowed 5 sccm -
while `0xC000` and `0xC002` both sat at 0.1. Neither is flow. The Modbus map for
this family is not the one in the generic MKS documentation, and reading alone
could not find flow.

Meanwhile `iobuf.js` states it outright:

    iobuf.flow_sensor  = 5.000144      actual flow, sccm
    iobuf.setpoint     = 5.000000      commanded setpoint, sccm
    iobuf.full_scale   = 29.000000     sccm  <-- differs per device AND per gas

**Full scale must be read, not configured.** It changes when the gas is changed
(Ar 29, H2 10, N2 50 on this system), and every flow number and setpoint limit
scales with it.

--------------------------------------------------------------------------
  WHY MODBUS FOR WRITING
--------------------------------------------------------------------------
`0xA000` holds the setpoint in ENGINEERING UNITS (sccm), not percent of full
scale. Confirmed: with full scale at 29 sccm the register read exactly 5.0 for a
5 sccm setpoint; 5 % of 29 would be 1.45.
"""

from __future__ import annotations

import asyncio
import re
import struct
import urllib.request
from dataclasses import dataclass
from typing import Literal

from ..config import MfcCfg
from .base import Device, Reading


# --------------------------------------------------------------------------- #
#  HTTP data access
# --------------------------------------------------------------------------- #

#: The four JS objects the web UI loads. `iobuf` carries the live process values.
JS_OBJECTS = ("iobuf", "deviceid", "device_html", "mfc")

_ASSIGN = re.compile(r"^(\w+)\.(\w+)\s*=\s*(.+?);\s*$", re.M)


def parse_js_object(text: str) -> dict[str, str]:
    """Turn `obj.key = value;` lines into {key: value}. Ignores arrays."""
    out: dict[str, str] = {}
    for _obj, key, val in _ASSIGN.findall(text):
        val = val.strip()
        if val.startswith("new "):
            continue
        out[key] = val.strip('"').strip()
    return out


def http_fetch_js(host: str, name: str, timeout: float = 5.0) -> dict[str, str]:
    url = f"http://{host}/{name}.js"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return parse_js_object(resp.read().decode("latin-1"))


def _f(d: dict[str, str], key: str) -> float | None:
    try:
        return float(d[key])
    except (KeyError, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
#  Modbus register map (write path only)
# --------------------------------------------------------------------------- #


@dataclass
class MfcRegisters:
    """Holding-register addresses, 32-bit IEEE-754 float over two registers.

    Only the setpoint is used. See the module docstring for why the other
    registers found by scanning are not trusted as flow/valve position.
    """

    setpoint_read: int = 0xA000    # CONFIRMED: reads the setpoint, in sccm
    setpoint_write: int = 0xA000
    word_order: Literal["big", "little"] = "big"   # CONFIRMED by decode sanity

    def decode_float(self, regs: list[int]) -> float:
        hi, lo = (regs[0], regs[1]) if self.word_order == "big" else (regs[1], regs[0])
        return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]

    def encode_float(self, value: float) -> list[int]:
        hi, lo = struct.unpack(">HH", struct.pack(">f", value))
        return [hi, lo] if self.word_order == "big" else [lo, hi]


def _unit_kwarg(client, unit_id: int) -> dict:
    """pymodbus renamed this argument across 3.x (`unit` -> `slave` -> `device_id`)."""
    import inspect

    try:
        params = inspect.signature(client.read_holding_registers).parameters
    except (TypeError, ValueError):
        return {"slave": unit_id}
    for name in ("device_id", "slave", "unit"):
        if name in params:
            return {name: unit_id}
    return {}


# --------------------------------------------------------------------------- #
#  Driver
# --------------------------------------------------------------------------- #


class MksMfc(Device):
    #: refresh identity/health every N polls (they change rarely)
    SLOW_EVERY = 20

    def __init__(self, cfg: MfcCfg, regs: MfcRegisters | None = None) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self.regs = regs or MfcRegisters()
        self._client = None
        self._kw: dict = {}
        self._lock = asyncio.Lock()
        self._tick = 0

        #: read live from the device - NOT from config, because it moves with gas
        self.full_scale_sccm: float | None = None
        self.gas: str = cfg.gas
        self.model: str = ""
        self.serial: str = ""
        self.valve_type: str = ""
        self.device_mode: str = ""
        self.health: dict[str, str] = {}
        self.commanded_sccm: float | None = None

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(self) -> None:
        """Read identity over HTTP, then open the Modbus session. Writes nothing."""
        await self._refresh_identity()
        if self.full_scale_sccm is None:
            raise ConnectionError(
                f"{self.id}: could not read full scale from http://{self.cfg.host}/ - "
                "flow cannot be scaled without it"
            )

        from pymodbus.client import AsyncModbusTcpClient

        self._client = AsyncModbusTcpClient(self.cfg.host, port=self.cfg.port, timeout=2.0)
        if not await self._client.connect():
            self._client = None
            # Reads still work over HTTP, so this is degraded, not fatal.
            self.last_error = (
                f"HTTP reads OK, but no Modbus at {self.cfg.host}:{self.cfg.port} - "
                "setpoints cannot be written"
            )
        else:
            self._kw = _unit_kwarg(self._client, self.cfg.unit_id)
        self.connected = True

    async def disconnect(self) -> None:
        """Close the Modbus socket. Deliberately does not zero the setpoint."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self.connected = False

    async def _refresh_identity(self) -> None:
        try:
            dev = await asyncio.to_thread(http_fetch_js, self.cfg.host, "deviceid")
            self.model = dev.get("model_number", "")
            self.serial = dev.get("serial", "")
            self.valve_type = dev.get("v_type", "")
            self.device_mode = dev.get("mode", "")
            self.health = {
                k: dev.get(k, "?")
                for k in ("over_temp", "open_circuit", "interface_error")
            }
        except Exception as exc:
            self.last_error = f"deviceid.js: {type(exc).__name__}: {exc}"

        try:
            gas = await asyncio.to_thread(http_fetch_js, self.cfg.host, "device_html")
            self.gas = gas.get("selected_gas", self.cfg.gas).strip()
            fs = _f(gas, "full_scale_amount")
            if fs:
                self.full_scale_sccm = fs
        except Exception as exc:
            self.last_error = f"device_html.js: {type(exc).__name__}: {exc}"

    # -- reads -------------------------------------------------------------- #

    async def read(self) -> list[Reading]:
        p = f"mfc.{self.id}"
        self._tick += 1
        if self._tick % self.SLOW_EVERY == 1:
            await self._refresh_identity()

        try:
            io = await asyncio.to_thread(http_fetch_js, self.cfg.host, "iobuf")
        except Exception as exc:
            return [
                self._bad(f"{p}.flow", "sccm", exc),
                self._bad(f"{p}.setpoint", "sccm", exc),
                self._bad(f"{p}.valve", "%", exc),
            ]

        fs = _f(io, "full_scale")
        if fs:
            self.full_scale_sccm = fs

        flow = _f(io, "flow_sensor")
        setpoint = _f(io, "setpoint")
        temp = _f(io, "temp_sensor")
        # valve_command is a raw drive number, not a percentage. Normalise it
        # against the observed full-open command so the UI shows something
        # comparable; the raw value is kept alongside.
        valve_raw = _f(io, "valve_command")

        self.last_error = ""
        out = [
            Reading(key=f"{p}.flow", value=flow, unit="sccm", ok=flow is not None),
            Reading(key=f"{p}.setpoint", value=setpoint, unit="sccm",
                    ok=setpoint is not None),
            Reading(key=f"{p}.valve_raw", value=valve_raw, unit="",
                    ok=valve_raw is not None),
            Reading(key=f"{p}.temp", value=temp, unit="C", ok=temp is not None),
            Reading(key=f"{p}.full_scale", value=self.full_scale_sccm, unit="sccm"),
        ]
        if flow is not None and self.full_scale_sccm:
            out.append(Reading(key=f"{p}.flow_pct",
                               value=100.0 * flow / self.full_scale_sccm, unit="%"))
        return out

    # -- writes ------------------------------------------------------------- #

    async def set_setpoint_sccm(self, sccm: float) -> float:
        """Write a flow setpoint in sccm. Returns the value read back."""
        if self._client is None:
            raise ConnectionError(f"{self.id}: no Modbus session for writing")

        sccm = float(sccm)
        async with self._lock:
            # The register is in engineering units (sccm), NOT percent.
            words = self.regs.encode_float(sccm)
            wr = await self._client.write_registers(
                self.regs.setpoint_write, words, **self._kw
            )
            if wr.isError():
                raise IOError(f"{self.id}: Modbus error writing setpoint: {wr}")
            self.commanded_sccm = sccm
            rr = await self._client.read_holding_registers(
                self.regs.setpoint_read, count=2, **self._kw
            )
            if rr.isError():
                raise IOError(f"{self.id}: wrote setpoint but could not read it back")
            readback = self.regs.decode_float(list(rr.registers))
        return readback

    def status(self) -> dict:
        return {
            **super().status(),
            "host": f"{self.cfg.host}:{self.cfg.port}",
            "unit_id": self.cfg.unit_id,
            "gas": self.gas,
            "full_scale_sccm": self.full_scale_sccm,
            "model": self.model,
            "serial": self.serial,
            "valve_type": self.valve_type,
            "device_mode": self.device_mode,
            "health": self.health,
            "device_info": (
                f"{self.model}  s/n {self.serial}  {self.gas}  "
                f"FS {self.full_scale_sccm:g} sccm" if self.full_scale_sccm else ""
            ),
            "commanded_sccm": self.commanded_sccm,
        }


# --------------------------------------------------------------------------- #
#  Read-only diagnostics
# --------------------------------------------------------------------------- #


def probe_mfc(host: str, port: int, unit_id: int) -> list[str]:
    """Read everything interesting about one MFC. HTTP GETs plus one Modbus read."""
    lines: list[str] = []
    try:
        io = http_fetch_js(host, "iobuf")
        dev = http_fetch_js(host, "deviceid")
        dh = http_fetch_js(host, "device_html")
        mfc = http_fetch_js(host, "mfc")
    except Exception as exc:
        return [f"http://{host}/ unreachable: {type(exc).__name__}: {exc}"]

    lines.append(f"model        : {dev.get('model_number','?')}  "
                 f"s/n {dev.get('serial','?')}")
    lines.append(f"product      : {dev.get('product','?')}")
    lines.append(f"mode         : {dev.get('mode','?')}")
    lines.append(f"valve type   : {dev.get('v_type','?')}")
    lines.append(f"gas          : {dh.get('selected_gas','?').strip()}"
                 f"   (calibrated on {mfc.get('mfc_calibration','?')},"
                 f" gcf {io.get('gcf','?')})")
    lines.append(f"FULL SCALE   : {io.get('full_scale','?')} "
                 f"{dh.get('full_scale_unit_name','')}"
                 f"   [range {dh.get('min_full_scale_amount','?')}"
                 f" - {dh.get('max_full_scale_amount','?')}]")
    lines.append(f"setpoint     : {io.get('setpoint','?')} sccm")
    lines.append(f"flow         : {io.get('flow_sensor','?')} sccm")
    lines.append(f"body temp    : {io.get('temp_sensor','?')} C")
    lines.append(f"health       : over_temp={dev.get('over_temp','?')}  "
                 f"open_circuit={dev.get('open_circuit','?')}  "
                 f"interface_error={dev.get('interface_error','?')}")
    lines.append(f"clients      : {dev.get('conn_list','?')}")

    async def _modbus() -> str:
        from pymodbus.client import AsyncModbusTcpClient

        client = AsyncModbusTcpClient(host, port=port, timeout=2.0)
        if not await client.connect():
            return "no Modbus TCP session"
        try:
            kw = _unit_kwarg(client, unit_id)
            rr = await client.read_holding_registers(0xA000, count=2, **kw)
            if rr.isError():
                return f"0xA000 error: {rr}"
            return (f"0xA000 = {MfcRegisters().decode_float(list(rr.registers)):g}"
                    " sccm  (setpoint, engineering units)")
        finally:
            client.close()

    lines.append(f"modbus       : {asyncio.run(_modbus())}")
    return lines


def scan_registers(host: str, port: int, unit_id: int,
                   pages: list[int] | None = None) -> list[str]:
    """Read-only sweep of the Modbus holding registers.

    Kept for diagnostics. Reading a register cannot change the device, and asking
    for one that does not exist returns a harmless exception - so sweeping is a
    safe way to explore an undocumented map. Note that on this family the sweep
    does NOT find flow; use the HTTP interface for readings.
    """

    async def _run() -> list[str]:
        from pymodbus.client import AsyncModbusTcpClient

        lines: list[str] = []
        client = AsyncModbusTcpClient(host, port=port, timeout=2.0)
        if not await client.connect():
            return [f"could not connect to {host}:{port}"]
        kw = _unit_kwarg(client, unit_id)
        regs_dec = MfcRegisters()

        async def read2(addr: int):
            try:
                rr = await client.read_holding_registers(addr, count=2, **kw)
                return None if rr.isError() else list(rr.registers)
            except Exception:
                return None

        try:
            candidates = pages if pages is not None else list(range(0, 0x10000, 0x1000))
            live = [b for b in candidates if await read2(b) is not None]
            lines.append(f"pages responding: {', '.join(f'0x{b:04X}' for b in live)}")
            for base in live:
                lines.append("")
                lines.append(f"page 0x{base:04X} (non-zero only):")
                found = False
                for off in range(0, 0x100, 2):
                    r = await read2(base + off)
                    if r is None or (r[0] == 0 and r[1] == 0):
                        continue
                    found = True
                    lines.append(f"    0x{base + off:04X}  {r[0]:5d},{r[1]:5d}"
                                 f"  float={regs_dec.decode_float(r):g}")
                if not found:
                    lines.append("    (all zero)")
        finally:
            client.close()
        return lines

    return asyncio.run(_run())
