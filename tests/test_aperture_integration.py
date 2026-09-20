"""Supervisor integrates aperture time from existing observations only."""
from __future__ import annotations

import asyncio
import json

from reactor.control.clock import Clock
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


class FakeTime:
    def __init__(self):
        self.elapsed = 100.0
        self.wall = 1_800_000_000.0

    def clock(self):
        return Clock(lambda: self.elapsed, lambda: self.wall)

    def advance(self, seconds):
        self.elapsed += seconds
        self.wall += seconds


async def main() -> int:
    c = Checker("test_aperture_integration")
    time = FakeTime()
    async with VirtualReactor(clock=time.clock()) as vr:
        sup = vr.sup
        hv = vr.supplies["hv"]
        initial = sup.aperture_lifetime.snapshot()
        c.check("startup does not infer beam state before a supply observation",
                initial["active"] is None)

        await vr.tick()
        c.check("first observed HV-off state is inactive",
                sup.aperture_lifetime.snapshot()["active"] is False)

        hv.hv_on = True
        await vr.tick()
        c.check("HV on plus released plasma ground starts timing",
                sup.valve_state["plasma_ground"] is False
                and sup.aperture_lifetime.snapshot()["active"] is True)
        time.advance(120.0)
        c.check("live state projects elapsed runtime without a disk write per frame",
                sup.aperture_lifetime.snapshot()["current"]["runtime_s"] == 120.0)
        await vr.tick()
        c.check("sampled telemetry publishes the same lifetime in seconds",
                sup.snapshot["aperture_lifetime_s"] == 120.0
                and sup.history[-1]["aperture_lifetime_s"] == 120.0)

        await sup.set_valve("plasma_ground", True, reason="test grounded pause")
        grounded = sup.aperture_lifetime.snapshot()
        c.check("grounding stops timing on the successful relay command",
                grounded["active"] is False
                and grounded["current"]["runtime_s"] == 120.0)
        time.advance(300.0)
        await sup.set_valve("plasma_ground", False, reason="test resume")
        time.advance(20.0)
        c.check("released relay resumes timing while observed HV remains on",
                sup.aperture_lifetime.snapshot()["current"]["runtime_s"] == 140.0)

        receipts = await sup.hv_off(reason="aperture integration test")
        stopped = sup.aperture_lifetime.snapshot()
        c.check("acknowledged HV-off stops timing before the next supply poll",
                receipts and all(row["ok"] for row in receipts)
                and stopped["active"] is False
                and stopped["current"]["runtime_s"] == 140.0)
        time.advance(600.0)
        await vr.tick()
        c.check("resting ungrounded relay does not accumulate after HV off",
                sup.valve_state["plasma_ground"] is False
                and sup.aperture_lifetime.snapshot()["current"]["runtime_s"] == 140.0)

        hv.hv_on = True
        hv.ok = False
        await vr.tick()
        unknown = sup.aperture_lifetime.snapshot()
        c.check("failed current HV observation creates a visible gap",
                unknown["active"] is None
                and "unknown" in unknown["current"]["observation_gap_reason"])
        time.advance(500.0)
        hv.ok = True
        await vr.tick()
        hv.connected = False
        await sup.hv_off(reason="disconnected no-op must stay unknown")
        c.check("a disconnected HV-off no-op is not treated as acknowledgement",
                sup.aperture_lifetime.snapshot()["active"] is None)
        hv.connected = True
        hv.hv_on = False
        await vr.tick()
        c.check("unknown interval is never backfilled as runtime",
                sup.aperture_lifetime.snapshot()["current"]["runtime_s"] == 140.0)

        before_writes = list(vr.daq.do_writes)
        current_id = sup.aperture_lifetime.snapshot()["current"]["id"]
        replaced = sup.aperture_lifetime.replace(current_id)
        c.check("replacement archives once and performs no DAQ write",
                len(replaced["history"]) == 1
                and vr.daq.do_writes == before_writes)
        disk = json.loads(sup.paths.aperture_lifetime.read_text(encoding="utf-8"))
        c.check("checkpoint is a readable durable file",
                disk["schema_version"] == 1 and len(disk["history"]) == 1)

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
