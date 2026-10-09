"""Read-only sensor plot catalog and compact projection of live history."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from ..config import ReactorConfig


@dataclass(frozen=True)
class SensorPlotSpec:
    key: str
    column: str
    label: str
    unit: str
    pressure: bool


def sensor_plot_specs(cfg: ReactorConfig) -> dict[str, SensorPlotSpec]:
    specs = {
        "pressure": SensorPlotSpec("pressure", "pressure", "Chamber pressure",
                                   cfg.pressure.unit, True),
    }
    if cfg.stage_temp.enabled:
        specs["stage.temp"] = SensorPlotSpec(
            "stage.temp", "stage_temp", cfg.stage_temp.label,
            cfg.stage_temp.unit, False)
    for gauge in cfg.gauges:
        key = f"gauge.{gauge.id}"
        specs[key] = SensorPlotSpec(key, f"gauge_{gauge.id}",
                                    gauge.label or gauge.id, gauge.unit, True)
    for aux in cfg.aux_inputs:
        if aux.kind == "thermocouple":
            key = f"aux.{aux.id}"
            specs[key] = SensorPlotSpec(key, f"aux_{aux.id}",
                                        aux.label or aux.id, aux.unit, False)
    for instrument in cfg.instruments:
        if instrument.enabled and instrument.driver == "mks_937b":
            key = f"inst.{instrument.id}"
            specs[key] = SensorPlotSpec(key, f"inst_{instrument.id}",
                                        instrument.label or instrument.id,
                                        instrument.unit, True)
    return specs


def compact_sensor_history(rows: Iterable[dict[str, Any]], column: str,
                           buckets: int = 700) -> list[list[float | None]]:
    """Keep extrema and one unavailable marker per bucket, in time order.

    Input is the existing carried-forward trend. A missing/invalid value is a
    gap, never zero. At most three points per bucket cross the wire.
    """
    source = []
    for row in rows:
        t = row.get("t")
        if not isinstance(t, (float, int)) or not math.isfinite(t):
            continue
        value = row.get(column)
        y = (float(value) if isinstance(value, (float, int))
             and not isinstance(value, bool) and math.isfinite(value) else None)
        source.append((float(t), y))
    if len(source) <= buckets * 2:
        return [[t, y] for t, y in source]
    size = math.ceil(len(source) / buckets)
    result = []
    for start in range(0, len(source), size):
        chunk = source[start:start + size]
        valid = [(index, value) for index, (_, value) in enumerate(chunk)
                 if value is not None]
        chosen: set[int] = set()
        if valid:
            chosen.add(min(valid, key=lambda pair: pair[1])[0])
            chosen.add(max(valid, key=lambda pair: pair[1])[0])
        missing = next((index for index, (_, value) in enumerate(chunk)
                        if value is None), None)
        if missing is not None:
            chosen.add(missing)
        result.extend([*chunk[index]] for index in sorted(chosen))
    if result and result[-1][0] != source[-1][0]:
        result.append([*source[-1]])
    return result
