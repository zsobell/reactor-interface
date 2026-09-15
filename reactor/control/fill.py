"""Fill regulation owns its task and progress; commands belong to its host."""
import asyncio
import contextlib
from typing import Any, Mapping, Protocol


class FillHost(Protocol):
    snapshot: Mapping[str, Any]
    def has_valve(self, valve_id: str) -> bool: ...
    async def drive_fill_valve(self, valve_id: str, state: bool) -> None: ...
    def report_event(self, kind: str, message: str) -> None: ...


class FillController:
    def __init__(self, host: FillHost):
        self.host = host
        self.task = None
        self.stop_event = asyncio.Event()
        self.state = {"running": False}

    async def start(
        self, *, valve: str, gauge: str, target_torr: float,
        pulse_on_s: float = 0.1, pulse_off_s: float = 0.3,
        tolerance_frac: float = 0.2,
    ) -> None:
        """Pulse `valve` to hold `gauge` (a snapshot key) at `target_torr`.

        Runs in the background until stop. Emits a gentle "flag"
        event when the pressure drifts more than tolerance_frac off setpoint, and
        another when it comes back - it never stops the run.
        """
        await self.stop()
        if not self.host.has_valve(valve):
            raise KeyError(f"unknown valve '{valve}'")
        self.stop_event.clear()
        self.state = {
            "running": True, "valve": valve, "gauge": gauge,
            "target_torr": target_torr, "tolerance_frac": tolerance_frac,
            "pulse_on_s": pulse_on_s, "pulse_off_s": pulse_off_s,
            "pressure": None, "in_bounds": True, "duty": False,
        }
        self.task = asyncio.create_task(
            self._run(valve, gauge, target_torr, pulse_on_s,
                                 pulse_off_s, tolerance_frac),
            name="fill-regulation",
        )
        self.host.report_event("fill",
                    f"regulating {gauge} to {target_torr:g} Torr via {valve} "
                    f"(flag beyond +/-{tolerance_frac*100:.0f}%)")

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.stop_event.set()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(self.task, timeout=5.0)
            if not self.task.done():
                self.task.cancel()
        self.task = None
        if self.state.get("running"):
            self.host.report_event("fill", "fill regulation stopped")
        self.state = {"running": False}

    async def _run(self, valve, gauge, target, on_s, off_s, tol) -> None:
        async def nap(dur: float) -> bool:      # returns True if asked to stop
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=dur)
                return True
            except asyncio.TimeoutError:
                return False

        try:
            while not self.stop_event.is_set():
                target = self.state["target_torr"]
                on_s, off_s = self.state["pulse_on_s"], self.state["pulse_off_s"]
                tol = self.state["tolerance_frac"]
                p = self.host.snapshot.get(gauge)
                self.state["pressure"] = p
                if isinstance(p, (int, float)) and target > 0:
                    off = abs(p - target) / target
                    inb = off <= tol
                    if inb != self.state.get("in_bounds", True):
                        self.state["in_bounds"] = inb
                        if not inb:
                            self.host.report_event("flag",
                                        f"{gauge} {p:.3g} Torr is {off*100:.0f}% off "
                                        f"setpoint {target:.3g} Torr")
                        else:
                            self.host.report_event("fill", f"{gauge} back within +/-{tol*100:.0f}%")
                    if p < target:
                        self.state["duty"] = True
                        await self.host.drive_fill_valve(valve, True)
                        stop = await nap(on_s)
                        await self.host.drive_fill_valve(valve, False)
                        if stop:
                            break
                        if await nap(off_s):
                            break
                        continue
                self.state["duty"] = False
                if await nap(off_s):
                    break
        finally:
            with contextlib.suppress(Exception):
                await self.host.drive_fill_valve(valve, False)
