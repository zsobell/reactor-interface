"""The Auger pressure path uses only 937B queries and rejects status as data."""

import asyncio
import csv
import sys

from reactor.config import InstrumentCfg, load_config
from reactor.dependencies import make_instrument
from reactor.devices.mks_937b import Mks937B, parse_pressure, parse_response
from reactor.testing.virtual_reactor import DEFAULT_CONFIG_PATH, VirtualReactor
from tests._support import Checker


class FakeSerial:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.writes = []
        self.closed = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass

    def read_until(self, terminator, limit):
        return next(self.replies)

    def close(self):
        self.closed = True


async def main():
    c = Checker("test_mks_937b")
    cfg = load_config(DEFAULT_CONFIG_PATH)
    gauge = next(i for i in cfg.instruments if i.id == "auger_pressure")
    c.check("configured 937B uses observed COM3 channel 3 in Torr",
            gauge.resource == "COM3" and gauge.query == "PR3"
            and gauge.address == 253 and gauge.unit == "Torr")
    c.check("production factory selects dedicated 937B driver",
            isinstance(make_instrument(gauge), Mks937B))
    c.check("pressure frame parses with address and ACK",
            parse_pressure(parse_response(b"@253ACK3.00E-10;FF", 253)) == 3e-10)
    for bad in (b"@252ACK3.00E-10;FF", b"@253NAK151;FF",
                b"@253ACK3.00E-10", b"junk"):
        try:
            parse_response(bad, 253)
            c.check(f"reject invalid frame {bad!r}", False)
        except ValueError:
            c.check(f"reject invalid frame {bad!r}", True)
    for bad in ("OFF", "WAIT", "NOGAUGE", "LO<E-10", "0", "NaN"):
        try:
            parse_pressure(bad)
            c.check(f"reject nonmeasurement {bad}", False)
        except ValueError:
            c.check(f"reject nonmeasurement {bad}", True)
    try:
        InstrumentCfg(id="bad", driver="mks_937b", resource="COM3",
                      query="HC3", unit="Torr", setup=["BAD!"])
        c.check("937B config forbids control and nonpressure commands", False)
    except ValueError:
        c.check("937B config forbids control and nonpressure commands", True)

    instrument = Mks937B(gauge)
    wire = FakeSerial([b"@253ACK3.00E-10;FF", b"@253ACKOFF;FF", b""])
    instrument._ser = wire
    instrument.connected = True
    good = (await instrument.read())[0]
    off = (await instrument.read())[0]
    lost = (await instrument.read())[0]
    c.check("valid pressure becomes Torr reading", good.ok and good.value == 3e-10)
    c.check("OFF is unavailable rather than zero or last pressure",
            not off.ok and off.value is None and "OFF" in off.detail)
    c.check("timeout disconnects for supervisor retry",
            not lost.ok and lost.value is None and not instrument.connected
            and wire.closed)
    c.check("wire contains only read-only channel queries",
            wire.writes == [b"@253PR3?;FF"] * 3)

    async with VirtualReactor() as vr:
        vr.instruments["auger_pressure"].value = 3e-10
        log_path = await vr.sup.recording.start_manual_log("auger-check.tsv")
        await vr.tick()
        await vr.tick()  # the first slow sample precedes the first instrument poll
        await vr.sup.recording.stop_manual_log()
        state = vr.sup.state()
        c.check("fake Auger pressure reaches telemetry and trend",
                state["snapshot"]["inst.auger_pressure"] == 3e-10
                and vr.sup.trend(1)[0]["inst_auger_pressure"] == 3e-10)
        with log_path.open(encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        c.check("manual log records measured Auger pressure",
                vr.sup.cfg.logging.columns["Auger pressure"] == "inst.auger_pressure"
                and any(row["Auger pressure"] and float(row["Auger pressure"]) == 3e-10
                        for row in rows))
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
