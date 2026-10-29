"""MFC flow excursions create temporary Diagnostics flags through fake devices."""
import asyncio
import sys

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


async def main():
    c = Checker("test_mfc_flow_flags")
    async with VirtualReactor() as vr:
        sup = vr.sup
        await sup.set_mfc_setpoint("mfc2", 5.0)
        vr.mfcs["mfc2"].flow_sccm = 0.0
        await vr.tick()
        c.check("settling flow is not flagged", not sup.setpoint_flags()
                and not sup.errors)

        sup._setpoint_changed["mfc.mfc2"] = sup.clock.elapsed() - 1.9
        await vr.tick()
        c.check("grace lasts through 1.9 seconds", not sup.setpoint_flags())

        sup._setpoint_changed["mfc.mfc2"] = sup.clock.elapsed() - 2.1
        await vr.tick()
        flags = [f for f in sup.setpoint_flags() if f["id"] == "mfc2"]
        c.check("flow mismatch is live after two seconds", len(flags) == 1)
        c.check("one temporary error is recorded", len(sup.errors) == 1
                and sup.errors[-1]["kind"] == "flag"
                and "MFC 2 flow 0" in sup.errors[-1]["message"],
                str(list(sup.errors)))
        await vr.tick()
        c.check("repeated polls do not duplicate the error", len(sup.errors) == 1)

        vr.mfcs["mfc2"].flow_sccm = 5.0
        await vr.tick()
        c.check("recovery clears the live flag", not sup.setpoint_flags())
        c.check("recovery is recorded once", sum(
            "flow mismatch cleared" in e["message"] for e in sup.events) == 1)
        c.check("history retains the original error", len(sup.errors) == 1)

        vr.mfcs["mfc2"].flow_sccm = 0.0
        await vr.tick()
        c.check("a new excursion creates a new error", len(sup.errors) == 2)
        await sup.set_mfc_setpoint("mfc2", 0.0)
        await vr.tick()
        c.check("zero target clears the live flag", not sup.setpoint_flags())
        c.check("zero target records recovery", sum(
            "flow mismatch cleared" in e["message"] for e in sup.events) == 2)
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
