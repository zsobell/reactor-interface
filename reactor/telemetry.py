"""Read-only reactor state projection and bounded WebSocket fan-out."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from pathlib import Path
from typing import Any


class Telemetry:
    def __init__(self, supervisor, groups):
        self.sup = supervisor
        self.groups = groups
        self.subscribers: set[asyncio.Queue] = set()

    def _ellipsometer_state(self) -> dict[str, Any]:
        cfg = self.sup.cfg.ellipsometer
        st: dict[str, Any] = {
            "enabled": cfg.enabled, "label": cfg.label,
            "host": cfg.host, "port": cfg.port,
        }
        if self.sup.ellipsometer is not None:
            st.update(self.sup.ellipsometer.status())
        # Points banked in the sidecar for the acquisition in progress, which is
        # the count the operator cares about mid-run ("points_seen" is every
        # point since the program started, across all acquisitions).
        ell_log = self.sup.recording.status().get("ellipsometer", {})
        st["capture_active"] = bool(ell_log.get("active"))
        st["capture_rows"] = ell_log.get("rows") or 0
        st["capture_file"] = (Path(ell_log["path"]).name
                              if ell_log.get("path") else None)
        return st

    def _recipe_state(self) -> dict[str, Any]:
        """Recipe progress plus the run-level countdown, computed here rather
        than cached on progress so the number is fresh at the instant it is
        published (the UI shows it to 0.1 s)."""
        d = self.sup.recipes.progress.as_dict()
        d["run_remaining_s"] = self.sup.recipes.run_remaining_s()
        d["run_total_s"] = self.sup.recipes.run_total_s()
        return d

    def state(self) -> dict[str, Any]:
        return copy.deepcopy({
            "t": time.time(),
            "site": self.sup.cfg.site.name,
            "cycle_count": self.sup._cycle_count,
            "loop_hz": self.sup.cfg.site.loop_hz,
            "snapshot": self.sup.snapshot,
            "readings": {k: r.as_dict() for k, r in self.sup.readings.items()},
            "daq": {
                "configured": bool(self.sup.daq and self.sup.daq.input_count),
                "inputs": self.sup.daq.input_count if self.sup.daq else 0,
                "error": self.sup.daq.last_error if self.sup.daq else "not started",
            },
            "stage_temp": {
                "enabled": self.sup.cfg.stage_temp.enabled,
                "label": self.sup.cfg.stage_temp.label,
                "value": self.sup.snapshot.get("stage.temp"),
                "unit": self.sup.cfg.stage_temp.unit,
            },
            "aux": [
                {
                    "id": a.id,
                    "label": a.label or a.id,
                    "value": self.sup.snapshot.get(f"aux.{a.id}"),
                    "volts": self.sup.snapshot.get(f"aux.{a.id}.volts"),
                    "unit": a.unit,
                }
                for a in self.sup.cfg.aux_inputs
            ],
            "gauges": [
                {
                    "id": g.id,
                    "label": self.sup._label("gauge", g.id, g.label or g.id),
                    "value": self.sup.snapshot.get(f"gauge.{g.id}"),
                    "volts": self.sup.snapshot.get(f"gauge.{g.id}.volts"),
                    "unit": g.unit,
                    "channel": g.channel,
                }
                for g in self.sup.cfg.gauges
            ],
            "valve_banks": [
                {"id": b.id, "label": b.label or b.id, "note": b.note}
                for b in self.sup.cfg.valve_banks
            ],
            "valves": [
                {
                    "id": v.id,
                    "label": self.sup._label("valve", v.id, v.label or v.id),
                    "kind": v.kind,
                    "bank": v.bank,
                    "line": v.line,
                    "identified": v.identified,
                    "open": self.sup.valve_state.get(v.id, False),
                }
                for v in self.sup.cfg.valves
            ],
            "mfcs": [
                {**st, "label": self.sup._label("mfc", mid, st.get("label") or mid),
                 "isolation_valve": next(
                     (m.isolation_valve for m in self.sup.cfg.mfcs if m.id == mid), None)}
                for mid, st in ((mid, d.status()) for mid, d in self.sup.mfcs.items())
            ],
            "instruments": [i.status() for i in self.sup.instruments.values()],
            # Read-only. Each status() carries read_only=True, which is what the
            # UI keys off to render a monitor card with no controls on it.
            "power_supplies": [p.status() for p in self.sup.supplies.values()],
            "regulator": self.sup.regulator,
            "prestart": self.sup.prestart,
            "marks": [m for m in self.sup.marks if time.time() - m["t"] <= 900][-500:],
            "run_valves": {"dose": self.sup._run_dose_valve,
                           "plasma": self.sup._run_plasma_switch},
            "valve_id": {
                **self.sup.sweep,
                "groups": self.groups,
                "unidentified_valves": [
                    {"id": v.id, "label": v.label or v.id, "bank": v.bank}
                    for v in self.sup.cfg.valves if not v.identified
                ],
            },
            "recipe": self._recipe_state(),
            "logging": self.sup.recording.status(),
            "ellipsometer": self._ellipsometer_state(),
            # Deliberately only the live TAIL, not the whole buffer. This
            # payload goes out on every telemetry frame (5 Hz), so shipping
            # thousands of events would be ~1 MB/s of pure repetition - painful
            # over Tailscale, which is exactly where the UI is used remotely.
            # The browser seeds its scrollback once from /api/events and then
            # appends whatever is new here. 200 is far more than one frame's
            # worth, so nothing can slip through the gap.
            "events": list(self.sup.events)[-200:],
        })

    def trend(self, limit: int = 1800) -> list[dict[str, Any]]:
        return list(self.sup.history)[-limit:]

    # -- websocket fan-out -------------------------------------------------- #

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    async def _publish(self) -> None:
        if not self.subscribers:
            return
        payload = self.state()
        for q in list(self.subscribers):
            if q.full():                       # slow client: drop the old frame
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)
