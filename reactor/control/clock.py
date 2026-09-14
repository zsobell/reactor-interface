"""Named time sources: elapsed durations and wall timestamps are distinct."""
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Clock:
    elapsed: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
