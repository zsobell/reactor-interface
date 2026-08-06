"""The Ar MFC / ar_pneumatic isolation interlock (Supervisor.set_mfc_setpoint
/ set_valve, config/reactor.yaml's isolation_valve field) - one of the two
operator-requested exceptions to "no software interlocks" (see
docs/CONTROL_MODEL.md).

Worth calling out why this test exists at all: it targets logic that lives
IN Supervisor itself, not in RecipeRunner or recipe.py. Every fake used
earlier in this project replaced Supervisor wholesale with a hand-written
stand-in, so this exact check was never actually exercised by any
automated test - the virtual reactor is what makes it possible, because it
runs the real Supervisor.set_mfc_setpoint/set_valve against fake devices
instead of replacing them.

Run directly: python -m tests.test_mfc_interlock
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


async def main() -> int:
    c = Checker("test_mfc_interlock")

    async with VirtualReactor() as vr:
        c.section("1. setpoint refused above 0 while the isolation valve is closed")
        c.check("ar_pneumatic starts closed", vr.sup.valve_state["ar_pneumatic"] is False)
        try:
            await vr.sup.set_mfc_setpoint("ar", 5.0)
            c.check("refused with the valve closed", False)
        except ValueError as exc:
            c.check("refused with the valve closed", True, str(exc))
        c.check("MFC never actually commanded", vr.mfcs["ar"].commanded_sccm == 0.0)

        c.section("2. 0 sccm is always allowed, closed valve or not")
        try:
            await vr.sup.set_mfc_setpoint("ar", 0.0)
            c.check("0 sccm accepted while closed", True)
        except ValueError as exc:
            c.check("0 sccm accepted while closed", False, str(exc))

        c.section("3. opening the valve allows a nonzero setpoint")
        await vr.sup.set_valve("ar_pneumatic", True, reason="test")
        await vr.sup.set_mfc_setpoint("ar", 5.0)
        c.check("setpoint accepted once open", vr.mfcs["ar"].commanded_sccm == 5.0,
                str(vr.mfcs["ar"].commanded_sccm))

        c.section("4. closing the valve zeroes the setpoint (the mirror-image check)")
        await vr.sup.set_valve("ar_pneumatic", False, reason="test")
        c.check("closing the valve auto-zeroed Ar",
                vr.mfcs["ar"].commanded_sccm == 0.0, str(vr.mfcs["ar"].commanded_sccm))
        c.check("a later nonzero setpoint is refused again",
                True)
        try:
            await vr.sup.set_mfc_setpoint("ar", 3.0)
            c.check("refused again after re-closing", False)
        except ValueError:
            c.check("refused again after re-closing", True)

        c.section("5. the interlock is per-MFC, not global - H2/N2 have no isolation_valve")
        c.check("H2 has no isolation valve configured",
                next(m for m in vr.sup.cfg.mfcs if m.id == "h2").isolation_valve is None)
        await vr.sup.set_mfc_setpoint("h2", 5.0)   # must NOT raise
        c.check("H2 setpoint accepted with no valve gating it",
                vr.mfcs["h2"].commanded_sccm == 5.0)

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
