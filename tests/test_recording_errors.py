"""Disk failures are observable, do not stop control, and do not leak handles."""
import asyncio
import io
import sys
from types import SimpleNamespace

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


class BrokenFile(io.StringIO):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure
        self.close_attempted = False

    def write(self, text):
        if self.failure == "write":
            raise OSError("disk full")
        return super().write(text)

    def flush(self):
        if self.failure == "flush":
            raise OSError("flush failed")
        return super().flush()

    def close(self):
        self.close_attempted = True
        super().close()
        if self.failure == "close":
            raise OSError("close failed")


async def main():
    c = Checker("test_recording_errors")
    for failure in ("write", "flush", "close"):
        async with VirtualReactor() as vr:
            log = vr.sup.logger
            broken = BrokenFile(failure)
            sibling = io.StringIO()
            log._run_fh, log._bycycle_fh = broken, sibling
            progress = SimpleNamespace(cycle_fraction=0.5, paused=False, step_desc="dose")
            log.write_run_sample({"t": 1, "pressure": 2}, progress)
            if failure != "close":
                c.check("failed row isn't counted", log.run_rows == 0)
                c.check("sibling stream still records", log.bycycle_rows == 1)
                count = len(vr.sup.events)
                log.write_run_sample({"t": 2}, progress)
                c.check("repeated failure does not flood events", len(vr.sup.events) == count)
            log.stop_run_export()
            c.check(f"{failure} failure visible in telemetry",
                    bool(vr.sup.state()["logging"]["errors"]))
            c.check("recording failure emits an event",
                    any("Recording failure" in e["message"] for e in vr.sup.events))
            c.check("both handles closed despite errors",
                    broken.close_attempted and sibling.closed)
            await vr.sup.set_valve("prec1", True)
            c.check("control remains usable", vr.daq.do_state["prec1"])
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
