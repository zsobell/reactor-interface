"""Device abstraction.

Every measurement is a `Reading` with a dotted key ("pressure", "stage.temp",
"mfc.mfc1.flow", "inst.ammeter"). The UI and the logger consume the same flat
dict, so adding an instrument does not require touching either of them.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Reading:
    key: str
    value: float | bool | None
    unit: str = ""
    t: float = field(default_factory=time.time)
    ok: bool = True
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "unit": self.unit,
            "t": self.t,
            "ok": self.ok,
            "detail": self.detail,
        }


class Device(abc.ABC):
    """Base for anything with a connection lifecycle and readable channels."""

    #: dotted prefix for this device's reading keys
    prefix: str = ""

    def __init__(self, dev_id: str, label: str = "") -> None:
        self.id = dev_id
        self.label = label or dev_id
        self.connected = False
        self.last_error: str = ""

    # -- lifecycle ---------------------------------------------------------- #

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the session. MUST NOT write anything to the instrument."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close the session. MUST NOT change instrument state."""

    # -- data --------------------------------------------------------------- #

    @abc.abstractmethod
    async def read(self) -> list[Reading]:
        """Poll every channel. Never raises: failures come back ok=False."""

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "connected": self.connected,
            "error": self.last_error,
        }

    # -- helpers ------------------------------------------------------------ #

    def _bad(self, key: str, unit: str, exc: BaseException | str) -> Reading:
        detail = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        self.last_error = detail
        return Reading(key=key, value=None, unit=unit, ok=False, detail=detail)
