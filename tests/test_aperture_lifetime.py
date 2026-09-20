"""Aperture lifetime persistence and timing have no hardware side effects."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import tempfile

from reactor.aperture_lifetime import (
    ApertureLifetime,
    ApertureLifetimeConflict,
    ApertureLifetimeError,
)
from reactor.control.clock import Clock
from tests._support import Checker


class FakeTime:
    def __init__(self, elapsed: float = 10.0, wall: float = 1_800_000_000.0):
        self.elapsed = elapsed
        self.wall = wall

    def clock(self) -> Clock:
        return Clock(lambda: self.elapsed, lambda: self.wall)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.wall += seconds


class FailingLifetime(ApertureLifetime):
    fail_writes = False

    def _write(self, document):
        if self.fail_writes:
            raise OSError("injected replacement failure")
        return super()._write(document)


async def main() -> int:
    c = Checker("test_aperture_lifetime")
    with tempfile.TemporaryDirectory(prefix="aperture_lifetime_") as td:
        path = Path(td) / "aperture_lifetime.json"
        time = FakeTime()
        life = ApertureLifetime(path, clock=time.clock(), checkpoint_every_s=10)
        first = life.snapshot()
        c.check("missing state creates a readable versioned model without eager I/O",
                first["available"] and first["schema_version"] == 1
                and not path.exists())
        c.check("new record begins unknown rather than assuming hardware state",
                first["active"] is None and first["current"]["runtime_s"] == 0)

        life.observe(True)
        c.check("the first observation checkpoints the new record", path.exists())
        time.advance(3600)
        c.check("active projected runtime uses monotonic elapsed time",
                math.isclose(life.snapshot()["current"]["runtime_h"], 1.0))
        life.observe(False)
        time.advance(900)
        life.observe(False)
        c.check("inactive time is excluded",
                math.isclose(life.snapshot()["current"]["runtime_s"], 3600.0))

        life.observe(True)
        time.advance(60)
        life.observe(None, reason="Glassman disconnected")
        time.advance(600)
        life.observe(True)
        time.advance(60)
        life.observe(False)
        state = life.snapshot()
        c.check("unknown spans stop timing and surface their reason",
                math.isclose(state["current"]["runtime_s"], 3720.0)
                and state["active"] is False)

        old_id = state["current"]["id"]
        replaced = life.replace(old_id)
        c.check("replacement archives the complete prior lifetime",
                len(replaced["history"]) == 1
                and replaced["history"][0]["id"] == old_id
                and math.isclose(replaced["history"][0]["runtime_s"], 3720.0)
                and replaced["current"]["runtime_s"] == 0)
        replay = life.replace(old_id)
        c.check("duplicate confirmed replacement is idempotent",
                len(replay["history"]) == 1
                and replay["current"]["id"] == replaced["current"]["id"])
        try:
            life.replace("not-the-current-aperture")
        except ApertureLifetimeConflict:
            conflict = True
        else:
            conflict = False
        c.check("stale unknown replacement is rejected", conflict)

        reloaded = ApertureLifetime(path, clock=time.clock())
        restored = reloaded.snapshot()
        c.check("history and current identity survive restart",
                restored["current"]["id"] == replaced["current"]["id"]
                and restored["history"][0]["id"] == old_id
                and restored["active"] is None)

        reloaded.observe(True)
        time.elapsed -= 5
        reloaded.observe(True)
        c.check("backward monotonic movement never subtracts runtime",
                reloaded.snapshot()["current"]["runtime_s"] >= 0
                and reloaded.snapshot()["active"] is True)

        valid_text = path.read_text(encoding="utf-8")
        path.write_text("{damaged", encoding="utf-8")
        damaged = ApertureLifetime(path, clock=time.clock())
        c.check("corrupt input is surfaced without overwrite",
                not damaged.snapshot()["available"]
                and path.read_text(encoding="utf-8") == "{damaged")
        try:
            damaged.replace("anything")
        except ApertureLifetimeError:
            unavailable = True
        else:
            unavailable = False
        c.check("corrupt state disables mutation", unavailable)

        path.write_text(valid_text, encoding="utf-8")
        raw = json.loads(valid_text)
        raw["schema_version"] = 99
        path.write_text(json.dumps(raw), encoding="utf-8")
        future = ApertureLifetime(path, clock=time.clock())
        c.check("future schema is preserved and rejected",
                not future.snapshot()["available"]
                and json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 99)

        path.write_text(valid_text, encoding="utf-8")
        failing = FailingLifetime(path, clock=time.clock())
        current_id = failing.snapshot()["current"]["id"]
        FailingLifetime.fail_writes = True
        before = path.read_text(encoding="utf-8")
        try:
            failing.replace(current_id)
        except ApertureLifetimeError:
            failed = True
        else:
            failed = False
        finally:
            FailingLifetime.fail_writes = False
        c.check("failed atomic replacement preserves prior file and memory",
                failed and path.read_text(encoding="utf-8") == before
                and failing.snapshot()["current"]["id"] == current_id)

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
