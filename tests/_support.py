"""Shared helpers for tests/test_*.py. Not a test framework - this project
deliberately has none (see tests/README.md) - just enough structure to keep
every test script's output uniform. Run each test file directly with
python; nothing here requires pytest or any other dependency.
"""

from __future__ import annotations

import asyncio


class Checker:
    """Tracks pass/fail lines for one test file and prints as it goes."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.fails: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")

    def check(self, label: str, cond: bool, detail: str = "") -> None:
        mark = "PASS" if cond else "FAIL"
        print(f"  {mark}  {label}{'  ' + detail if detail else ''}")
        if not cond:
            self.fails.append(label)

    def summary(self) -> int:
        """Print a final verdict and return a process exit code."""
        if self.fails:
            print(f"\n{self.name}: FAILURES ({len(self.fails)}): {self.fails}")
            return 1
        print(f"\n{self.name}: ALL PASS")
        return 0


async def autotick(vr, period: float = 0.1) -> asyncio.Task:
    """Start a background task calling `vr.tick()` on a fixed period, standing
    in for the real _control_loop/_current_loop that a live Supervisor would
    be running. Without this, self.sup.snapshot never picks up changes made
    to vr.daq.raw / vr.instruments[x].value / vr.mfcs[x].flow_sccm - exactly
    as on the real reactor, a device's value doesn't reach the recipe engine
    until something polls it and copies it into the snapshot.

    Returns the task; cancel it (and await the CancelledError) when done:

        task = await autotick(vr)
        try:
            ...
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    """
    async def _loop():
        while True:
            await vr.tick()
            await asyncio.sleep(period)

    return asyncio.create_task(_loop(), name="autotick")
