"""Construction isolation and real lifecycle with fake devices."""
import asyncio
import json
import tempfile
from pathlib import Path
from reactor.testing.virtual_reactor import (
    VirtualReactor, FakeDaq, FakeMfc, FakeInstrument, FakeSupply, FakeKeithley,
    DEFAULT_CONFIG_PATH,
)
from reactor.config import load_config
from reactor.dependencies import DeviceFactory, StatePaths
from reactor.devices.ellipsometer import EllipsometerPoint
from reactor.devices.instrument import parse_scpi_reading
from reactor import supervisor
from tests._support import Checker, wait_for


class FakeEllipsometer:
    def __init__(self, config, *, on_point, on_state):
        self.config, self.on_point, self.on_state = config, on_point, on_state
        self.starts = self.stops = self.points_seen = 0
        self.connected = False

    def start(self):
        self.starts += 1
        self.connected = True
        self.on_state(True, "fake connected")

    async def stop(self):
        await asyncio.sleep(0)  # proves Supervisor awaits shutdown completion
        self.connected = False
        self.stops += 1
        self.on_state(False, "fake stopped")

    def emit(self, point):
        self.points_seen += 1
        self.on_point(point)

    def status(self):
        return {"connected": self.connected, "points_seen": self.points_seen}


class MissingMfc(FakeMfc):
    async def connect(self):
        if self.id == "ar":
            raise OSError("fake device unplugged")
        await super().connect()


async def ellipsometer_lifecycle(c, missing_device):
    with tempfile.TemporaryDirectory(prefix="injected_devices_") as directory:
        root = Path(directory)
        config = load_config(DEFAULT_CONFIG_PATH)
        config.site.data_dir = str(root / "data")
        config.ellipsometer.enabled = True
        config.ellipsometer.host = "fake.invalid"
        clients = []

        def ellipsometer_factory(cfg, **callbacks):
            client = FakeEllipsometer(cfg, **callbacks)
            clients.append(client)
            return client

        factory = DeviceFactory(
            daq=FakeDaq, mfc=MissingMfc if missing_device else FakeMfc,
            instrument=FakeInstrument,
            supply=lambda cfg: FakeSupply(cfg) if cfg.driver == "glassman_fl" else FakeKeithley(cfg),
            ellipsometer=ellipsometer_factory)
        sup = supervisor.Supervisor(config, devices=factory, paths=StatePaths.in_directory(root))
        client, = clients
        c.check("ellipsometer factory receives config and remains disconnected at construction",
                client.config is config.ellipsometer and client.starts == 0)
        try:
            await sup.start(background_tasks=False)
            c.check("production startup starts injected subscriber exactly once",
                    client.starts == 1 and client.connected)
            c.check("ellipsometer state callback reaches operator events",
                    any(e["kind"] == "ellipsometer" and e["message"] == "stream connected"
                        for e in sup.events))
            client.emit(EllipsometerPoint.from_fields(
                {"n": 1, "Time": 0, "Thick(nm).1": 12.5}, t_recv=1700000000.0))
            await sup.recording.drain()
            status = sup.state()["ellipsometer"]
            path = Path(sup.recording.status()["ellipsometer"]["path"])
            c.check("injected point callback reaches recording and telemetry",
                    status["connected"] and status["points_seen"] == 1
                    and status["capture_rows"] == 1 and status["capture_active"]
                    and path.is_relative_to(root.resolve()) and "12.5" in path.read_text())
            if missing_device:
                c.check("failed device connection is visible while available devices connect",
                        not sup.mfcs["ar"].connected and "unplugged" in sup.mfcs["ar"].last_error
                        and all(d.connected for key, d in sup.mfcs.items() if key != "ar")
                        and all(d.connected for d in sup.instruments.values())
                        and all(d.connected for d in sup.supplies.values())
                        and any(e["kind"] == "error" and "unplugged" in e["message"] for e in sup.events))
            c.check("connection lifecycle emits no output writes", sup.daq.do_writes == [])
        finally:
            await sup.stop()
        c.check("shutdown awaits subscriber stop and records disconnect callback",
                client.stops == 1 and not client.connected
                and any(e["message"] == "stream disconnected (fake stopped)" for e in sup.events))
        c.check("subscriber capture is closed during shutdown", not sup.recording.status()["ellipsometer"]["active"])
        c.check("all injected device families disconnect and DAQ closes",
                all(not d.connected for d in [*sup.mfcs.values(), *sup.instruments.values(), *sup.supplies.values()])
                and sup.daq.close_count == 1)


async def main():
    c = Checker('test_dependencies')
    c.section('DMM readings stay in base amperes across display prefixes')
    c.check('plain DMM6500 READ value is already amperes',
            parse_scpi_reading('+1.250000E-03', 'A') == 0.00125)
    c.check('explicit mA and uA formats normalize to amperes',
            parse_scpi_reading('1.25mA', 'A') == 0.00125
            and parse_scpi_reading('1250,uA', 'A') == 0.00125)
    try:
        parse_scpi_reading('1.25V', 'A')
        wrong_unit_rejected = False
    except ValueError:
        wrong_unit_rejected = True
    c.check('a non-current unit cannot silently enter the current channel',
            wrong_unit_rejected)

    defaults = (supervisor.LABELS_PATH, supervisor.VALVE_STATE_PATH, supervisor.RUN_NAME_PATH)
    async with VirtualReactor() as first, VirtualReactor() as second:
        c.check('startup does not drive outputs', not first.daq.do_writes and not second.daq.do_writes)
        c.check('both devices connected through startup', all(d.connected for d in first.mfcs.values()))
        await first.sup.set_valve('prec1', True)
        await second.sup.set_valve('prec1', False)
        first.sup.set_label('valve', 'prec1', 'first reactor')
        c.check('paths belong to each instance', first.sup.paths != second.sup.paths)
        c.check('files isolate simultaneous valve commands',
                json.loads(first.sup.paths.valves.read_text())['prec1'] is True
                and json.loads(second.sup.paths.valves.read_text())['prec1'] is False)
        c.check('labels do not leak', not second.sup.paths.labels.exists())
        c.check('module defaults unchanged', defaults == (
            supervisor.LABELS_PATH, supervisor.VALVE_STATE_PATH, supervisor.RUN_NAME_PATH))
        first_devices = [*first.mfcs.values(), *first.instruments.values(), *first.supplies.values()]
        c.check('all power supplies connect through startup',
                len(first.supplies) == sum(ps.enabled for ps in first.sup.cfg.power_supplies)
                and all(d.connected for d in first.supplies.values()))
    c.check('shutdown disconnects all fake device families', all(not d.connected for d in first_devices))
    c.check('shutdown closes both DAQ adapters exactly once', first.daq.close_count == second.daq.close_count == 1)
    c.check('temporary files removed', not first.sup.paths.valves.exists())
    async with VirtualReactor(background_tasks=True) as running:
        c.check('production polling produces telemetry', await wait_for(lambda: bool(running.sup.history)))
        tasks = (running.sup._loop_task, running.sup._current_task,
                 running.sup._mfc_task, running.sup._reconnect_task)
        c.check('production tasks are running', all(t and not t.done() for t in tasks))
    c.check('shutdown awaits production tasks', all(t.done() for t in tasks))
    await ellipsometer_lifecycle(c, missing_device=False)
    await ellipsometer_lifecycle(c, missing_device=True)
    return c.summary()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
