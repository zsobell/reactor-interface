"""XP Glassman FL-series high-voltage power supply, over its serial interface.

The plasma supply. On this reactor it is an **FL1.5F1.0** (1500 V, 1.0 A),
connected by USB to the rear-panel J3 socket.

WHAT THIS DRIVER DOES TO THE SUPPLY
===================================
**It reads, and it turns HV off. That is the whole list.**

`connect()` sends `V` (software-version request) and `read()` sends `Q`
(request monitors). Both are read commands: the FL manual is explicit that a
Query works "while still in LOCAL control mode ... at any time", and neither
command can change a setpoint or enable high voltage.

`hv_off()` is the one write, added 2026-08-21 at Zach's request so a run that
ends - completed, aborted, or crashed - cannot leave HV energised. It asserts
the HV Off control bit and echoes the supply's own last-read levels back with
the frame, because the FL's Set packet always carries a voltage and a current
program and sending zeros would wipe what Zach dialled in at the front panel.
Note that ANY Set command moves the supply into REMOTE until LOC/REM is
pressed. It is called by `Supervisor.hv_off`, from run teardown and the
pre-start abort - nowhere else, and never on `disconnect()`.

The rest of the set-command support (`set_levels`, `set_hv`, `reset`) is
implemented below because Zach asked for the full protocol to be in place for
later. **Nothing calls it.** There is still no way to set a level or turn HV
*on* from the supervisor, the HTTP API or the UI. Zach operates this supply by
hand from its front panel; adding setpoint control is a separate, explicitly-
requested change (see docs/CONTROL_MODEL.md).

Two consequences of that, both deliberate:

* `disconnect()` does NOT send HV OFF. A driver for this supply written
  elsewhere in the group does exactly that, after an incident where their
  application exited leaving HV energised. That is the right call *for a
  program that turns HV on*. This one never does, so an HV-OFF on shutdown
  would be an unrequested automatic action that could switch off a supply Zach
  had set by hand at the front panel - stopping the server would silently kill
  his plasma. Closing the port is all that happens here.
* There is no watchdog, no clamp and no limit. An earlier plan capped the
  voltage setpoint at 1000 V; Zach withdrew that once this became read-only,
  since with no way to set a voltage there is nothing to clamp. **If remote
  control is ever added, revisit that decision before wiring up `set_levels`.**

PROTOCOL
========
Source: XP Glassman "Series FL" instruction manual, doc 102002-168 Rev H,
26 Aug 2021 - Figures 31-37. Verified against this supply on 2026-08-21.
Full transcription in docs/GLASSMAN_FL.md.

Serial: 8 data bits, no parity, 1 stop bit. The manual allows 2400, 4800,
9600 or 19200 baud and calls 9600 the default. The supply is a pure slave: it
never transmits unless asked.

Every frame is uppercase ASCII::

    SOH(0x01)  addr  <body>  <checksum>  CR(0x0D)

`addr` is one ASCII hex digit, 0-7 (0x30-0x37). The checksum is a modulo-256
sum rendered as two uppercase hex ASCII characters, and it covers the command
body only - **never** the SOH or the address byte.

    Query    6 bytes    SOH addr 'Q' cs cs CR
    Version  6 bytes    SOH addr 'V' cs cs CR
    Set     21 bytes    SOH addr 'S' V[3] I[3] "FFF" "000" ctrl "FF" cs cs CR

    Response  16 bytes  'R' V[3] I[3] arc[2] status[2] fault[2] cs cs CR
    Ack        2 bytes  'A' CR
    Version    6 bytes  'B' rev[2] cs cs CR
    Error      5 bytes  'E' code cs cs CR

Analog values are 12-bit throughout: 0x000-0xFFF spans 0 to full scale, for
both the setpoints and the monitors. (The older EJ/FJ-series manual specifies
10-bit 0x3FF monitors. That does NOT apply to the FL - do not use that manual
for this supply; see the note in docs/GLASSMAN_FL.md.)

ADDRESS AND BAUD ARE NOT GUESSABLE
==================================
This supply answers at **19200 baud, address 1** - not the documented 9600 /
address 0 default, and not what its DIP switches appear to say. Bringing it up
cost a day precisely because those were assumed rather than measured: a sweep
of addresses 0-F at 9600, plus baud 1200-115200 at addresses 0 and F only,
missed the one cell that mattered. Both values live in `config/reactor.yaml`.
If a supply ever goes quiet, sweep the full cross-product of the four
supported baud rates against addresses 0-7 before concluding anything is
broken; `tools/probe_glassman.py` does exactly that.
"""

from __future__ import annotations

import asyncio
import time

from ..config import PowerSupplyCfg
from .base import Device, Reading

SOH = 0x01
CR = 0x0D

#: Full scale for every 12-bit analog field, per manual Figures 31 and 33.
FULL_SCALE_COUNTS = 0xFFF

#: Manual Figure 37. Returned in a 5-byte 'E' packet.
ERROR_CODES = {
    1: "unidentified command code (not S, Q or V)",
    2: "checksum error",
    3: "extra byte(s) received",
    4: "illegal digital control byte (only one of HV On / HV Off / Reset)",
    5: "illegal set command with a fault active",
    6: "processing error",
}


class GlassmanProtocolError(RuntimeError):
    """A reply arrived but was malformed, or the supply returned an 'E' packet."""


def checksum(body: bytes) -> bytes:
    """Modulo-256 sum of *body*, as two uppercase hex ASCII characters.

    `body` is the command body only. The manual is explicit that the checksum
    "does not include the SOH or Address characters".
    """
    return f"{sum(body) % 256:02X}".encode()


def build_frame(address: int, body: bytes) -> bytes:
    """SOH + address + body + checksum(body) + CR."""
    if not 0 <= address <= 7:
        raise ValueError(f"address must be 0-7, got {address}")
    return bytes([SOH]) + f"{address:01X}".encode() + body + checksum(body) + bytes([CR])


def counts_to_units(counts: int, full_scale: float) -> float:
    """12-bit count -> engineering units."""
    return counts / FULL_SCALE_COUNTS * full_scale


def units_to_counts(value: float, full_scale: float) -> int:
    """Engineering units -> 12-bit count, clamped to the hardware's range.

    The clamp is the ADC's own 0..0xFFF range, not a policy limit - there is no
    software voltage limit in this program (see the module docstring).

    **Truncates rather than rounds**, for two reasons. It matches the manual's
    own worked example - Figure 31 renders 25% of full scale as 0x3FF, and
    0.25 * 0xFFF is 1023.75, so the vendor is truncating (they note it is
    "within an error of 1 lsb"). And on a supply that can put out 1500 V it is
    the safer direction: a requested level can come out a fraction of a count
    low, never high.
    """
    if full_scale <= 0:
        return 0
    frac = max(0.0, min(1.0, value / full_scale))
    return int(frac * FULL_SCALE_COUNTS)


class GlassmanReply:
    """Decoded 16-byte Query response (manual Figure 33).

    Byte numbering below is the manual's, 1-based::

        1      'R'
        2-4    voltage monitor   0-FFF = 0..Vmax
        5-7    current monitor   0-FFF = 0..Imax
        8-9    arc monitor       0-FF  = 0..255 arcs
        10-11  status signals
        12-13  fault monitors
        14-15  modulo-256 checksum of bytes 2-13
        16     CR

    Status byte 10:  bit0 fault, bit1 local/remote (HI = remote),
                     bit2 current-trip select, bit3 HV on.
    Status byte 11:  bit1 V/I mode (HI = voltage mode).
    Fault byte 12:   bit0 interlock, bit1 over-temperature, bit2 input fault.
    Fault byte 13:   bit2 arc fault, bit3 current trip.
    """

    __slots__ = ("raw", "voltage_counts", "current_counts", "arc_count",
                 "fault", "remote", "current_trip_enabled", "hv_on",
                 "voltage_mode", "interlock_fault", "over_temperature",
                 "input_fault", "arc_fault", "current_trip")

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.voltage_counts = int(raw[1:4], 16)
        self.current_counts = int(raw[4:7], 16)
        self.arc_count = int(raw[7:9], 16)

        status_a = int(raw[9:10], 16)
        status_b = int(raw[10:11], 16)
        fault_a = int(raw[11:12], 16)
        fault_b = int(raw[12:13], 16)

        self.fault = bool(status_a & 0x1)
        self.remote = bool(status_a & 0x2)
        self.current_trip_enabled = bool(status_a & 0x4)
        self.hv_on = bool(status_a & 0x8)
        self.voltage_mode = bool(status_b & 0x2)

        self.interlock_fault = bool(fault_a & 0x1)
        self.over_temperature = bool(fault_a & 0x2)
        self.input_fault = bool(fault_a & 0x4)
        self.arc_fault = bool(fault_b & 0x4)
        self.current_trip = bool(fault_b & 0x8)

    @property
    def any_fault(self) -> bool:
        return (self.fault or self.interlock_fault or self.over_temperature
                or self.input_fault or self.arc_fault or self.current_trip)

    def fault_names(self) -> list[str]:
        """Human-readable list of whatever is currently asserted."""
        named = (
            (self.interlock_fault, "interlock"),
            (self.over_temperature, "over temperature"),
            (self.input_fault, "input fault"),
            (self.arc_fault, "arc fault"),
            (self.current_trip, "current trip"),
        )
        out = [name for flag, name in named if flag]
        # The summary bit can be set without a specific cause being decodable,
        # so report it rather than showing "faulted" with an empty cause list.
        if self.fault and not out:
            out.append("fault (unspecified)")
        return out


def parse_query_response(raw: bytes) -> GlassmanReply:
    """Validate and decode a Query response, or raise GlassmanProtocolError.

    Also recognises the 5-byte 'E' error packet, which the supply returns for
    any malformed command - so a framing mistake surfaces as a real message
    rather than as silence.
    """
    if not raw:
        raise GlassmanProtocolError("no reply (timeout)")
    if raw[0:1] == b"E":
        # 'E' + 1-byte code + 2-byte checksum + CR. Checksum covers the code only.
        try:
            code = int(raw[1:2])
        except ValueError:
            raise GlassmanProtocolError(f"malformed error packet {raw!r}") from None
        raise GlassmanProtocolError(
            f"supply returned error {code}: {ERROR_CODES.get(code, 'unknown')}")
    if len(raw) != 16 or raw[0:1] != b"R":
        raise GlassmanProtocolError(
            f"expected a 16-byte 'R' packet, got {len(raw)} bytes: {raw!r}")

    # Manual: "Modulo 256 checksum of all previous bytes except first",
    # i.e. bytes 2-13 -> raw[1:13].
    want = checksum(raw[1:13])
    got = raw[13:15]
    if want != got:
        raise GlassmanProtocolError(
            f"checksum mismatch: computed {want.decode()}, received {got.decode()}"
            f" on {raw!r}")
    try:
        return GlassmanReply(raw)
    except ValueError as exc:
        raise GlassmanProtocolError(f"undecodable packet {raw!r}: {exc}") from None


def parse_version_response(raw: bytes) -> str:
    """Decode the 6-byte 'B' version packet -> the revision string (e.g. "02")."""
    if len(raw) != 6 or raw[0:1] != b"B":
        raise GlassmanProtocolError(
            f"expected a 6-byte 'B' packet, got {len(raw)} bytes: {raw!r}")
    want = checksum(raw[1:3])          # checksum covers the revision bytes only
    got = raw[3:5]
    if want != got:
        raise GlassmanProtocolError(
            f"version checksum mismatch: computed {want.decode()}, "
            f"received {got.decode()}")
    return raw[1:3].decode()


class GlassmanFL(Device):
    """One FL-series supply on a serial (or USB virtual-COM) port."""

    def __init__(self, cfg: PowerSupplyCfg) -> None:
        super().__init__(cfg.id, cfg.label or cfg.id)
        self.cfg = cfg
        self._ser = None
        self.firmware = ""
        self.last_reply: GlassmanReply | None = None
        # One serial exchange at a time. Only the poll loop uses the port today,
        # but reset_input_buffer() in one exchange would eat another's reply.
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(self) -> None:
        """Open the port and read the firmware revision. Writes no settings."""
        await asyncio.to_thread(self._open)
        self.connected = True
        self.last_error = ""

    def _open(self) -> None:
        import serial

        # write_timeout is NOT optional on Windows: pyserial maps an unset
        # (None) write_timeout to WriteTotalTimeoutConstant=0, which Win32 reads
        # as "wait forever". Without it a write to an unplugged USB-serial port
        # blocks the poll loop indefinitely instead of raising.
        self._ser = serial.Serial(
            self.cfg.port,
            self.cfg.baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.cfg.timeout_s,
            write_timeout=self.cfg.timeout_s,
        )
        # The USB bridge needs a moment after open before it will pass traffic,
        # and may hand over stale bytes from a previous session. Blocking sleep
        # is fine: _open only ever runs inside asyncio.to_thread.
        time.sleep(0.1)
        self._ser.reset_input_buffer()

        raw = self._exchange(b"V")
        self.firmware = parse_version_response(raw)

    async def disconnect(self) -> None:
        """Close the port.

        Deliberately does not command anything - see the module docstring. The
        supply keeps doing whatever the front panel told it to.
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
        """Discard a dead session so the reconnect loop opens a clean one."""
        self._close()
        self.connected = False

    # -- io ----------------------------------------------------------------- #

    def _exchange(self, body: bytes) -> bytes:
        """Send one frame, return the raw reply. Blocking; call via to_thread.

        Reads to the CR terminator rather than a fixed byte count: replies are
        16 bytes for a query, 6 for a version, 5 for an error and 2 for an
        acknowledge, so no single length fits. A fixed-length read would wait
        out the full timeout on every short reply. The byte cap is only a guard
        against a babbling port.
        """
        if self._ser is None:
            raise ConnectionError(f"{self.id}: not connected")
        frame = build_frame(self.cfg.address, body)
        self._ser.reset_input_buffer()
        self._ser.write(frame)
        return self._ser.read_until(bytes([CR]), 32)

    async def _talk(self, body: bytes) -> bytes:
        async with self._lock:
            return await asyncio.to_thread(self._exchange, body)

    # -- data --------------------------------------------------------------- #

    async def read(self) -> list[Reading]:
        """One Query. Returns voltage, current and arc count; never raises."""
        vkey = f"hv.{self.id}.voltage"
        ikey = f"hv.{self.id}.current"
        akey = f"hv.{self.id}.arc_count"
        vunit, iunit = self.cfg.unit_v, self.cfg.unit_i

        if not self.connected:
            return [self._bad(vkey, vunit, "not connected"),
                    self._bad(ikey, iunit, "not connected"),
                    self._bad(akey, "", "not connected")]
        try:
            raw = await self._talk(b"Q")
        except Exception as exc:
            # Port died (unplugged, supply powered down). Drop the handle so
            # the supervisor's reconnect loop reopens it rather than reusing a
            # dead one.
            self._drop()
            return [self._bad(vkey, vunit, exc),
                    self._bad(ikey, iunit, exc),
                    self._bad(akey, "", exc)]

        try:
            reply = parse_query_response(raw)
        except GlassmanProtocolError as exc:
            # A bad frame is not a dead port: keep the session and let the next
            # poll try again, but surface the reason.
            return [self._bad(vkey, vunit, exc),
                    self._bad(ikey, iunit, exc),
                    self._bad(akey, "", exc)]

        self.last_reply = reply
        self.last_error = ""
        return [
            Reading(key=vkey,
                    value=counts_to_units(reply.voltage_counts, self.cfg.full_scale_v),
                    unit=vunit),
            Reading(key=ikey,
                    value=counts_to_units(reply.current_counts, self.cfg.full_scale_i),
                    unit=iunit),
            Reading(key=akey, value=float(reply.arc_count), unit=""),
        ]

    async def read_version(self) -> str:
        """Re-read the firmware revision. A read; safe at any time."""
        return parse_version_response(await self._talk(b"V"))

    # -- control ------------------------------------------------------------ #
    #
    # ONE of these is wired up: `hv_off`, called by the supervisor when a run
    # ends or is aborted (operator request, 2026-08-21). Everything else below
    # is still implemented-but-unused, per Zach's instruction to "add everything
    # now anyway" while keeping voltage/current control off the UI.
    #
    # Before exposing any of the rest, read docs/CONTROL_MODEL.md: adding a
    # remote *energising* path to a 1.5 kV supply is a change to reactor
    # behaviour and needs Zach's explicit go-ahead, not a code review. The
    # withdrawn 1000 V cap should be reconsidered at the same time.

    def _set_body(self, voltage: float, current: float, *,
                  hv_on: bool | None = None, reset: bool = False,
                  counts: tuple[int, int] | None = None) -> bytes:
        """Body of a Set command - everything between the address and the
        checksum (manual Figure 31). `_exchange` wraps it into the 21-byte frame.

        Reserved fields carry the literal values the manual specifies: bytes
        10-12 are "FFF" (0x46), bytes 13-15 are "000" (0x30), bytes 17-18 are
        "FF". They are not free bytes - the supply rejects other values.

        The digital control nibble is byte 16: bit0 HV Off, bit1 HV On, bit2
        Reset. Only ONE may be asserted per packet or the supply answers with
        error 4. `hv_on=None` asserts none of them, which is legal and is how
        you change setpoints while leaving the output state alone.
        """
        if counts is not None:
            # Raw 12-bit programs, used by hv_off to echo the supply's own last
            # reading back at it without a units round-trip.
            v_counts, i_counts = counts
        else:
            v_counts = units_to_counts(voltage, self.cfg.full_scale_v)
            i_counts = units_to_counts(current, self.cfg.full_scale_i)

        ctrl = 0
        if reset:
            ctrl = 0x4
        elif hv_on is True:
            ctrl = 0x2
        elif hv_on is False:
            ctrl = 0x1

        return (b"S"
                + f"{v_counts:03X}".encode()
                + f"{i_counts:03X}".encode()
                + b"FFF" + b"000"
                + f"{ctrl:01X}".encode()
                + b"FF")

    async def _send_set(self, voltage: float, current: float, *,
                        hv_on: bool | None = None, reset: bool = False,
                        counts: tuple[int, int] | None = None) -> None:
        """Send a Set frame and require the 'A' acknowledge."""
        body = self._set_body(voltage, current, hv_on=hv_on, reset=reset,
                              counts=counts)
        async with self._lock:
            raw = await asyncio.to_thread(self._exchange, body)
        if raw[0:1] == b"E":
            parse_query_response(raw)          # raises with the decoded code
        if raw[0:1] != b"A":
            raise GlassmanProtocolError(f"expected 'A' acknowledge, got {raw!r}")

    async def set_levels(self, voltage: float, current: float) -> None:
        """Set the voltage and current programs, leaving HV state alone. NOT CALLED."""
        await self._send_set(voltage, current, hv_on=None)

    async def set_hv(self, on: bool, voltage: float, current: float) -> None:
        """Assert HV On or HV Off alongside the setpoints. NOT CALLED."""
        await self._send_set(voltage, current, hv_on=on)

    async def hv_off(self) -> None:
        """Turn the output off, leaving the voltage and current programs alone.

        THE ONE COMMAND THIS PROGRAM SENDS. Called when a run ends or is
        aborted (operator request, 2026-08-21) so a finished run cannot leave
        HV energised.

        Two protocol facts shape this:

        * The Set frame ALWAYS carries a V and an I program - there is no
          "HV off only" packet. Sending zeros would wipe the levels Zach
          dialled in on the front panel, so instead the supply's own last
          reading goes back out with the frame: the programs it lands on are
          the ones it was already delivering, not 0/0.
        * Any Set command puts the supply in REMOTE. The front-panel LOC/REM
          button switches it back (it only ever switches *to* local, which is
          why it looks dead while already local - see docs/GLASSMAN_FL.md).

        No-op when the port is closed: an unreachable supply is not an error
        worth failing a run teardown over.
        """
        if not self.connected:
            return
        r = self.last_reply
        counts = (r.voltage_counts, r.current_counts) if r else None
        await self._send_set(0.0, 0.0, hv_on=False, counts=counts)

    async def reset(self) -> None:
        """Power-supply reset: zeroes V, I and the arc count, HV off. NOT CALLED."""
        await self._send_set(0.0, 0.0, reset=True)

    # -- status ------------------------------------------------------------- #

    def status(self) -> dict:
        r = self.last_reply
        return {
            **super().status(),
            "driver": self.cfg.driver,
            "model": self.cfg.model,
            "port": self.cfg.port,
            "baud": self.cfg.baud,
            "address": self.cfg.address,
            "firmware": self.firmware,
            "full_scale_v": self.cfg.full_scale_v,
            "full_scale_i": self.cfg.full_scale_i,
            "unit_v": self.cfg.unit_v,
            "unit_i": self.cfg.unit_i,
            # "No setpoint controls" - the UI reads this to decide not to draw
            # any. Still true after hv_off was wired up: that is a teardown
            # action, not something the operator drives from this card.
            "read_only": True,
            "voltage": (counts_to_units(r.voltage_counts, self.cfg.full_scale_v)
                        if r else None),
            "current": (counts_to_units(r.current_counts, self.cfg.full_scale_i)
                        if r else None),
            "arc_count": r.arc_count if r else None,
            "hv_on": r.hv_on if r else None,
            "remote": r.remote if r else None,
            "voltage_mode": r.voltage_mode if r else None,
            "current_trip_enabled": r.current_trip_enabled if r else None,
            "faulted": r.any_fault if r else None,
            "faults": r.fault_names() if r else [],
        }
