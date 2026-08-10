"""Film Sense FS-1 ellipsometer - live measurement stream (read-only).

The FS-1 broadcasts every dynamic-mode measurement on TCP ``<host>:4001``,
one record per measurement (~1 Hz), unsolicited, to any connected client. We
only ever *read*: nothing is written to the instrument. (There is a separate
command socket on 4000 and another on 4010 which we deliberately never touch -
those are how the old LabVIEW program *triggered* measurements; we don't need
to, since the tool free-runs.)

Wire format, per measurement, two back-to-back frames on 4001:

  1. a 4-byte sync marker:  ``03 02 01 63``
  2. a record::

        [uint8 field_count]
        field_count x  [uint8 name_len][name ascii][float32 little-endian value]

The eight fields, in order, are: ``n`` (1-based point index), ``Fit_Diff``,
``Thick(nm).1``, ``AlignX``, ``AlignY``, ``Intensity``, ``Time`` (relative
seconds from the start of the acquisition), ``Temp``. The record is
self-describing - the field names are on the wire - so this decoder keys off
the names rather than fixed offsets.

Why this matters: the ``Time`` (or ``n``) field is the join key back to the
file you download and *refit* in the FS-1 software after a run. That download
holds the same points in the same order, so once each streamed point is
stamped with the reactor clock here, every refit thickness inherits an
absolute, reactor-clock timestamp - which is what lets the ellipsometry line
up exactly with the reactor log. The post-run join lives in
``reactor/analysis/ellipsometer_merge.py``.

On stop the socket simply goes silent (no end-of-run marker), so an
acquisition is delimited by the gap: the first point after silence starts one,
and ``idle_gap_s`` with no point ends it.
"""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

#: 4-byte frame marker that precedes every measurement record on port 4001
SYNC = b"\x03\x02\x01\x63"

#: the instrument's live-broadcast port (4000/4010 are command sockets - unused)
STREAM_PORT = 4001


# --------------------------------------------------------------------------- #
#  Pure decoding (no I/O - unit-tested against real captured bytes)
# --------------------------------------------------------------------------- #

def decode_record(buf: bytes, off: int = 0) -> tuple[dict[str, float], int]:
    """Decode one ``[count][name,value]*`` record starting at ``buf[off]``
    (i.e. just past a SYNC marker). Returns ``(fields, next_off)`` where
    ``next_off`` is the index one byte past the record. Raises IndexError /
    struct.error / UnicodeDecodeError on a malformed / truncated record, which
    the stream walkers below treat as "resync"."""
    count = buf[off]
    off += 1
    fields: dict[str, float] = {}
    for _ in range(count):
        name_len = buf[off]
        off += 1
        name = buf[off:off + name_len].decode("ascii")
        off += name_len
        (value,) = struct.unpack_from("<f", buf, off)
        off += 4
        fields[name] = value
    return fields, off


def iter_records(data: bytes) -> Iterator[dict[str, float]]:
    """Walk a captured byte stream, yielding one field-dict per measurement.
    Framing is anchored on SYNC; a record that fails to decode causes a
    one-byte resync rather than aborting the stream. Records are parsed by
    their own length, so a SYNC-looking byte sequence *inside* a float value
    can't derail framing."""
    i = 0
    n = len(data)
    while True:
        j = data.find(SYNC, i)
        if j < 0:
            return
        try:
            fields, nxt = decode_record(data, j + len(SYNC))
        except (IndexError, struct.error, UnicodeDecodeError):
            i = j + 1            # false marker - shift one byte and retry
            continue
        yield fields
        i = nxt


# --------------------------------------------------------------------------- #
#  A decoded point
# --------------------------------------------------------------------------- #

def _first(fields: dict[str, float], *prefixes: str) -> float | None:
    """Value of the first field whose name starts with any of ``prefixes``
    (the thickness field is named e.g. ``Thick(nm).1`` / ``Thick(A).1`` -
    the unit and index suffix vary, so match on the ``Thick`` prefix)."""
    for name, val in fields.items():
        if name.startswith(prefixes):
            return val
    return None


@dataclass(frozen=True)
class EllipsometerPoint:
    """One streamed measurement, plus the reactor-clock time it arrived."""
    index: int                       # the "n" counter, 1-based
    time_s: float | None             # FS-1 relative seconds ("Time")
    thickness: float | None          # model thickness (wrong until refit!)
    thickness_unit: str              # "nm" / "A", parsed from the field name
    fit_diff: float | None
    intensity: float | None
    temp: float | None
    align_x: float | None
    align_y: float | None
    t_recv: float                    # reactor wall-clock at arrival (time.time)
    raw: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_fields(cls, fields: dict[str, float], t_recv: float) -> "EllipsometerPoint":
        thick_name = next((k for k in fields if k.startswith("Thick")), "")
        unit = ""
        if "(" in thick_name and ")" in thick_name:
            unit = thick_name[thick_name.index("(") + 1:thick_name.index(")")]
        n = fields.get("n")
        return cls(
            index=int(round(n)) if n is not None else -1,
            time_s=fields.get("Time"),
            thickness=_first(fields, "Thick"),
            thickness_unit=unit,
            fit_diff=fields.get("Fit_Diff"),
            intensity=fields.get("Intensity"),
            temp=fields.get("Temp"),
            align_x=fields.get("AlignX"),
            align_y=fields.get("AlignY"),
            t_recv=t_recv,
            raw=dict(fields),
        )


# --------------------------------------------------------------------------- #
#  Async client (read-only subscriber)
# --------------------------------------------------------------------------- #

class EllipsometerClient:
    """Holds a persistent read-only connection to the FS-1 stream port and
    calls ``on_point`` for every measurement, stamped with the reactor clock
    the instant the record finishes arriving. Reconnects on drop. Sends
    nothing, ever.

    ``on_point(point)`` and ``on_state(connected, detail)`` are plain
    callbacks invoked from the reader task; keep them cheap and non-blocking
    (the supervisor's just append a sidecar row / update a status field)."""

    def __init__(
        self,
        host: str,
        port: int = STREAM_PORT,
        *,
        on_point: Callable[[EllipsometerPoint], None] | None = None,
        on_state: Callable[[bool, str], None] | None = None,
        reconnect_s: float = 2.0,
    ) -> None:
        self.host = host
        self.port = port
        self.on_point = on_point
        self.on_state = on_state
        self.reconnect_s = reconnect_s
        self.connected = False
        self.last_error = ""
        self.last_point: EllipsometerPoint | None = None
        self.points_seen = 0
        self._task: asyncio.Task | None = None
        self._stop = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="ellipsometer")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._set_state(False, "stopped")

    def status(self) -> dict[str, Any]:
        p = self.last_point
        return {
            "host": self.host,
            "port": self.port,
            "connected": self.connected,
            "running": self.running,
            "error": self.last_error,
            "points_seen": self.points_seen,
            "last_index": p.index if p else None,
            "last_time_s": p.time_s if p else None,
            "last_thickness": p.thickness if p else None,
            "last_thickness_unit": p.thickness_unit if p else "",
            "last_recv": p.t_recv if p else None,
        }

    # -- internals ---------------------------------------------------------- #

    def _set_state(self, connected: bool, detail: str = "") -> None:
        self.connected = connected
        if detail:
            self.last_error = "" if connected else detail
        if self.on_state is not None:
            try:
                self.on_state(connected, detail)
            except Exception:
                pass

    async def _run(self) -> None:
        while not self._stop:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
            except OSError as exc:
                self._set_state(False, f"connect: {exc}")
                await asyncio.sleep(self.reconnect_s)
                continue
            self._set_state(True, "")
            try:
                await self._read_loop(reader)
            except (asyncio.IncompleteReadError, OSError) as exc:
                self._set_state(False, f"stream: {exc}")
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, asyncio.CancelledError):
                    pass
            if not self._stop:
                await asyncio.sleep(self.reconnect_s)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._stop:
            await self._resync(reader)
            count = (await reader.readexactly(1))[0]
            fields: dict[str, float] = {}
            for _ in range(count):
                name_len = (await reader.readexactly(1))[0]
                name = (await reader.readexactly(name_len)).decode("ascii", "replace")
                (value,) = struct.unpack("<f", await reader.readexactly(4))
                fields[name] = value
            t_recv = time.time()
            point = EllipsometerPoint.from_fields(fields, t_recv)
            self.last_point = point
            self.points_seen += 1
            if self.on_point is not None:
                try:
                    self.on_point(point)
                except Exception:
                    pass

    @staticmethod
    async def _resync(reader: asyncio.StreamReader) -> None:
        """Read bytes until the last four seen are the SYNC marker. On a clean
        connection this consumes exactly the next 4 bytes; after a mid-stream
        reconnect it skips a partial record."""
        window = bytearray()
        while True:
            window += await reader.readexactly(1)
            if len(window) > len(SYNC):
                del window[0]
            if window == SYNC:
                return
