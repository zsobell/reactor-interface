"""Supervisor.stop() must finish even when a device will not let go.

Reported 2026-08-27: pressing "Shut down server" greyed the button and did
nothing. The process stayed up holding the DAQ and COM8-COM12, so the next
server started could not reach a single device ("The specified resource is
reserved", "Access is denied"), and it took a SECOND shutdown from the new
server to taskkill the old one. server.log shows the pattern three times over,
always the same shape: first press "other instances killed: none", then a new
server failing to configure the DAQ, then a second press killing two.

Two things made that possible:

  * every step in stop() was an unbounded await against real hardware - serial
    closes, a VISA close, Modbus disconnects, DAQmx task closes - so one wedged
    device stopped the whole teardown;
  * the first press can never taskkill this instance anyway. The killer skips
    its own PID and its parent's, and the parent IS the other `-m reactor`
    process: the venv's pythonw.exe is a launcher shim that re-execs the real
    interpreter as a child. So the graceful path is the only way out, and it
    must not be able to hang.

This pins down the first half. The second half - the hard deadline that ends
the process if stop() somehow still overruns - lives in reactor/__main__.py and
cannot be tested in-process without killing the test run.

Run directly: python -m tests.test_shutdown_teardown
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


class Wedged:
    """A device whose disconnect() never returns - a serial port on a USB
    adapter that has been pulled, a VISA session to a switched-off DMM."""

    def __init__(self, dev):
        self._dev = dev
        self.id = dev.id
        self.hits = 0

    def __getattr__(self, name):
        return getattr(self._dev, name)

    async def disconnect(self):
        self.hits += 1
        await asyncio.sleep(3600)


async def main() -> int:
    c = Checker("test_shutdown_teardown")
    # Inside main(), not under __main__: run_all imports this module and calls
    # main() directly, so a capture installed at import time would be missing
    # exactly when the whole suite runs.
    _install_log_capture()

    async with VirtualReactor() as vr:
        sup = vr.sup

        c.section("1. a device that never disconnects cannot stop the teardown")
        # Wedge one of each kind, so no single loop can be the only one bounded.
        wedged = []
        for pool in (sup.mfcs, sup.instruments, sup.supplies):
            key = next(iter(pool))
            pool[key] = Wedged(pool[key])
            wedged.append(pool[key])

        t0 = time.time()
        receipt = await asyncio.wait_for(sup.stop(), timeout=60)
        dt = time.time() - t0

        c.check("every wedged device was actually reached",
                all(w.hits == 1 for w in wedged),
                str([(w.id, w.hits) for w in wedged]))
        # Three wedged devices at a 3 s deadline each, plus the DAQ close and
        # the loop cancels. Comfortably under the 20 s hard deadline in
        # __main__ - which is the point: the graceful path has to WIN, not get
        # rescued by the process being killed.
        c.check("stop() returned instead of hanging", dt < 18.0, f"{dt:.1f}s")
        c.check("...and it took roughly one deadline per wedged device, "
                "not one long one", dt > 2.0, f"{dt:.1f}s")

        c.section("2. it still got through everything after the wedged ones")
        # The DAQ is the one that mattered on the day: a zombie holding it is
        # what stopped the next server from starting.
        c.check("DAQ tasks closed", vr.daq.closed is True, str(vr.daq.closed))
        for pool, what in ((sup.mfcs, "MFC"), (sup.instruments, "instrument"),
                           (sup.supplies, "supply")):
            for dev_id, dev in pool.items():
                if isinstance(dev, Wedged):
                    continue
                c.check(f"{what} {dev_id} disconnected",
                        dev.connected is False, str(dev.connected))

        c.section("3. the receipt says what was released, and what was not")
        # This is what the Shut down button shows. Zach, 2026-09-10: "I need
        # some confirmation things are shut down and ready to be booted again."
        # "Port 8000 stopped answering" is not that - uvicorn releases the
        # socket BEFORE the teardown - so the teardown has to report itself.
        c.check("stop() returned a receipt", isinstance(receipt, dict),
                type(receipt).__name__)
        c.check("it is marked NOT ok - three devices were wedged",
                receipt["ok"] is False, str(receipt["ok"]))
        c.check("every wedged device is named in `failed`",
                all(any(w.id in f["what"] for f in receipt["failed"])
                    for w in wedged),
                str([f["what"] for f in receipt["failed"]]))
        c.check("the DAQ is listed as released",
                any("DAQ" in r for r in receipt["released"]),
                str(receipt["released"]))
        c.check("a wedged device is NOT listed as released",
                not any(w.id in r for w in wedged for r in receipt["released"]),
                str(receipt["released"]))
        c.check("it carries an elapsed time", receipt["elapsed_s"] > 0,
                f"{receipt['elapsed_s']}s")

        c.section("4. stop() is idempotent - the endpoint and the lifespan "
                  "both call it")
        for w in wedged:
            w.hits = 0
        t1 = time.time()
        again = await asyncio.wait_for(sup.stop(), timeout=10)
        c.check("the second stop() returned the same receipt", again is receipt)
        c.check("...without touching a device again",
                all(w.hits == 0 for w in wedged),
                str([(w.id, w.hits) for w in wedged]))
        c.check("...and returned immediately", time.time() - t1 < 0.5,
                f"{time.time() - t1:.2f}s")

        c.section("5. the log names the step, so a real hang is diagnosable")
        # Not an event-log assertion - these go to `log`, which is server.log on
        # the real machine, and that is where the operator has to look.
        named = [ln for ln in _teardown_log_lines() if ln.startswith("shutdown: ")]
        c.check("every teardown step announced itself", len(named) >= 6,
                f"{len(named)} steps: {named[:3]}")

    return c.summary()


_LINES: list[str] = []


def _teardown_log_lines() -> list[str]:
    return _LINES


def _install_log_capture() -> None:
    import logging

    class Grab(logging.Handler):
        def emit(self, record):
            _LINES.append(record.getMessage())

    logging.getLogger("reactor.supervisor").addHandler(Grab())
    logging.getLogger("reactor.supervisor").setLevel(logging.INFO)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
