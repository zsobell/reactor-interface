"""A failed Keithley handshake releases the COM handle before retry."""
import asyncio
from unittest.mock import patch

from reactor.config import PowerSupplyCfg
from reactor.devices.keithley_2260b import Keithley2260B
from tests._support import Checker


class Port:
    def __init__(self, port, *args, **kwargs):
        self.port = port
        self.closed = False
        self.last_command = ""
        self.fail_idn = port == "COM7"

    def close(self):
        self.closed = True

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.last_command = data.decode().strip()

    def read_until(self, _terminator):
        if self.fail_idn:
            return b""
        if self.last_command == "*IDN?":
            return b"Keithley Instruments Inc.,Model 2260B-250-4,1412016,1.0\n"
        return b"250\n"


async def main():
    c = Checker("test_keithley_reconnect")
    ports = []
    names = iter(("COM7", "COM9"))

    def serial_factory(name, *args, **kwargs):
        port = Port(name, *args, **kwargs)
        ports.append(port)
        return port

    cfg = PowerSupplyCfg(id="stage_bias", driver="keithley_2260b",
                         usb_serial="1412016")
    device = Keithley2260B(cfg)
    with patch("reactor.devices.keithley_2260b.find_port", side_effect=lambda _: next(names)), \
         patch("serial.Serial", side_effect=serial_factory), \
         patch("reactor.devices.keithley_2260b.time.sleep"):
        try:
            await device.connect()
        except Exception:
            pass
        c.check("failed handshake closes first COM handle",
                ports[0].closed and device._ser is None and not device.connected)
        await device.connect()
        c.check("retry rediscovers renamed COM port",
                device.connected and device.port == "COM9" and not ports[1].closed)
        ports[1].malformed_poll = True
        # A parse failure is a lost session too; leaving connected=True would
        # prevent the supervisor's retry loop from ever running.
        original_read = ports[1].read_until
        ports[1].read_until = lambda end: b"garbled\n" if ports[1].last_command.startswith(":MEAS") else original_read(end)
        readings = await device.read()
        c.check("malformed poll drops session for supervisor retry",
                not device.connected and ports[1].closed and all(not r.ok for r in readings))
        await device.disconnect()
        c.check("disconnect never sends an output command",
                ports[1].closed and all(not p.last_command.startswith(":OUTP") for p in ports))
    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
