"""Published frames are stable and slow viewers do not block control."""
import asyncio
import sys

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


async def main():
    c = Checker("test_telemetry")
    async with VirtualReactor() as vr:
        sup = vr.sup
        q = sup.subscribe()
        sup.snapshot["pressure"] = 1
        sup.prestart["phase"] = "before"
        await sup._publish()
        frame = q.get_nowait()
        sup.snapshot["pressure"] = 2
        sup.prestart["phase"] = "after"
        c.check("snapshot is frozen per frame", frame["snapshot"]["pressure"] == 1)
        c.check("controller progress is frozen per frame", frame["prestart"]["phase"] == "before")
        for n in range(10):
            sup.snapshot["pressure"] = n
            await asyncio.wait_for(sup._publish(), 0.1)
        c.check("slow viewers have a bounded queue", q.qsize() == 4)
        pressures = [q.get_nowait()["snapshot"]["pressure"] for _ in range(4)]
        c.check("only the newest frames survive", pressures == [6, 7, 8, 9])
        sup.unsubscribe(q)
        await sup._publish()
        c.check("unsubscribed viewers receive no frames", q.empty())
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
