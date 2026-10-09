"""Pressure/temperature popup receives compact, permitted read-only history."""

import asyncio
import sys

from reactor.server.app import create_app
from reactor.server.sensor_plot import compact_sensor_history, sensor_plot_specs
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, request


async def main():
    c = Checker("test_sensor_plot")
    async with VirtualReactor() as vr:
        bakeout = next((a for a in vr.sup.cfg.aux_inputs if a.id == "bakeout2"), None)
        c.check("fourth Mod4 input is Bakeout temp 2",
                bakeout is not None and bakeout.channel == "cDAQ1Mod4/ai3"
                and bakeout.kind == "thermocouple" and bakeout.unit == "C")
        c.check("DAQ plan acquires fourth thermocouple as type K",
                any(a.key == "aux.bakeout2" and a.channel == "cDAQ1Mod4/ai3"
                    and a.tc_type == "K" for a in vr.daq._plan.ai))
        vr.daq.raw["aux.bakeout2"] = 67.5
        await vr.tick()
        state = vr.sup.state()
        c.check("Hardware telemetry and run history carry Bakeout temp 2",
                any(a["id"] == "bakeout2" and a["label"] == "Bakeout temp 2"
                    and a["value"] == 67.5 for a in state["aux"])
                and vr.sup.history[-1]["aux_bakeout2"] == 67.5)
        specs = sensor_plot_specs(vr.sup.cfg)
        c.check("pressure and thermocouple sensors are offered",
                {"pressure", "inst.auger_pressure", "stage.temp",
                 "gauge.prec1_dose", "aux.bubbler", "aux.bakeout2"}.issubset(specs))
        c.check("voltage input and controls are excluded",
                "aux.voltage" not in specs and "inst.ammeter" not in specs
                and "mfc.ar.flow" not in specs)
        vr.sup.history.clear()
        rows = [{"t": 1000 + i, "pressure": 1e-8,
                 "inst_auger_pressure": 3e-10, "stage_temp": 20.0}
                for i in range(3000)]
        rows[0]["aux_bakeout2"] = 67.5
        rows[125]["pressure"] = 1e-12
        rows[1300]["pressure"] = 1e-5
        rows[1800]["pressure"] = None
        vr.sup.history.extend(rows)
        app = create_app(supervisor=vr.sup)
        read_seq = vr.sup._read_seq
        status, payload = await request(app, "/api/sensor-trend?key=pressure")
        samples = payload["samples"] if status == 200 else []
        values = [pair[1] for pair in samples]
        c.check("response is compact and keeps both extremes and a gap",
                status == 200 and len(samples) <= 2101 and
                1e-12 in values and 1e-5 in values and None in values)
        c.check("time order and latest sample survive reduction",
                all(a[0] <= b[0] for a, b in zip(samples, samples[1:]))
                and samples[-1] == [3999.0, 1e-8])
        c.check("metadata identifies units and pressure axis",
                payload["key"] == "pressure" and payload["unit"] == "Torr"
                and payload["pressure"] is True)
        c.check("plot request performs no additional device read",
                vr.sup._read_seq == read_seq)
        status, payload = await request(app, "/api/sensor-trend?key=inst.auger_pressure")
        c.check("Auger channel uses its run-history column",
                status == 200 and payload["samples"][0][1] == 3e-10)
        status, payload = await request(app, "/api/sensor-trend?key=stage.temp")
        c.check("temperature defaults to linear scale",
                status == 200 and payload["pressure"] is False and payload["unit"] == "C")
        status, payload = await request(app, "/api/sensor-trend?key=aux.bakeout2")
        c.check("Bakeout temp 2 popup uses its history and Celsius axis",
                status == 200 and payload["label"] == "Bakeout temp 2"
                and payload["pressure"] is False and payload["unit"] == "C"
                and payload["samples"][0][1] == 67.5)
        for key in ("inst.ammeter", "mfc.ar.flow", "aux.not_connected"):
            status, _ = await request(app, f"/api/sensor-trend?key={key}")
            c.check(f"unapproved series {key} is refused", status == 404)
    compact = compact_sensor_history(
        [{"t": 1, "pressure": 2}, {"t": 2, "pressure": None}], "pressure")
    c.check("short history preserves exact samples and missing values",
            compact == [[1.0, 2.0], [2.0, None]])
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
