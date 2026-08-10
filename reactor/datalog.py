"""Run logging.

Tab-delimited, one file per run named `YYMMDD_HHMMSS_<suffix>`, following the
LabVIEW logger's convention. The column layout is config, not code:
`logging.columns` maps a column heading to a snapshot key, so adding a channel to
the file is a one-line YAML edit.

The old logger wrote `Time / Pressure / QCM Mass / A / B / C / D`. There is no
QCM on this tool and A-D have not been identified, so they are not written -
emitting a column of meaningless zeros is worse than not having it.

A richer parallel CSV carries every channel plus recipe position, which is what
you actually want when diagnosing a run.

Logging starts on demand, not at launch, so idle time does not fill the disk.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ReactorConfig

#: column value that means "seconds since logging started"
ELAPSED_KEY = "_elapsed"


def _describe_ald_recipe(recipe) -> str:
    """Plain-English summary of a built ALD Recipe - cycle architecture and the
    gas-schedule timeline, for a human skimming the run snapshot file."""
    lines = [f"{recipe.name} — {recipe.cycles} cycles"]
    for s in recipe.steps:
        lines.append(f"  {s.describe()}")
    if recipe.gas_schedules:
        beam = next((s for s in recipe.steps if s.op == "electron_beam"), None)
        beam_s = beam.seconds if beam else None
        first = next((g for g in recipe.gas_schedules if g.order == "first"), None)
        second = next((g for g in recipe.gas_schedules if g.order == "second"), None)
        if beam_s:
            handoff = (first.pct / 100 * beam_s) if first else 0.0
            lines.append("  gas schedule (relative to beam step start):")
            if first:
                lines.append(
                    f"    {first.mfc}: on at beam-{first.lead_s:g}s, "
                    f"off at beam+{handoff:g}s "
                    f"({first.pct:g}% @ {first.flow_sccm:g} sccm)")
            if second:
                on_at = max(0.0, handoff - second.lead_s)
                off_at = min(beam_s, handoff + second.pct / 100 * beam_s)
                lines.append(
                    f"    {second.mfc}: on at beam+{on_at:g}s, "
                    f"off at beam+{off_at:g}s "
                    f"({second.pct:g}% @ {second.flow_sccm:g} sccm)")
    return "\n".join(lines)


class DataLogger:
    def __init__(self, cfg: ReactorConfig) -> None:
        self.cfg = cfg
        self.dir = Path(cfg.site.data_dir).expanduser().resolve()
        self._fh = None
        self._ext_fh = None
        self.path: Path | None = None
        self.ext_path: Path | None = None
        self.started_at: float | None = None
        self.rows = 0
        self._ext_keys: list[str] = []
        # Automatic per-run export - independent of the operator-toggled
        # logger above. Opened the moment any recipe starts (ALD, CVD, or a
        # file recipe) and closed whenever the run ends, so a run's trace
        # survives a closed browser without the operator doing anything.
        self._run_fh = None
        self.run_path: Path | None = None
        self.run_started_at: float | None = None
        self.run_rows = 0
        self._run_keys: list[str] = []
        # Plot-ready "by cycle" export, opened alongside the run export: the same
        # channels keyed by fractional cycle number instead of time, with paused
        # (reignite / operator-pause) samples left out so a property-vs-cycle
        # plot is clean. See RecipeRunner.cycle_fraction.
        self._bycycle_fh = None
        self.bycycle_path: Path | None = None
        self.bycycle_rows = 0
        self._bycycle_keys: list[str] = []
        # Per-acquisition ellipsometer sidecar - the (fs_time -> reactor_clock)
        # record captured live from the FS-1 stream, so a refit file can later
        # be put back onto the reactor clock. One file per acquisition; opened
        # by the supervisor on the first streamed point of a run (see
        # start_ellipsometer_capture) and independent of everything above.
        self._ell_fh = None
        self.ell_path: Path | None = None
        self.ell_started_at: float | None = None
        self.ell_rows = 0

    @property
    def active(self) -> bool:
        return self._fh is not None

    @property
    def run_export_active(self) -> bool:
        return self._run_fh is not None

    # -- lifecycle ---------------------------------------------------------- #

    def start(self, label: str | None = None) -> Path:
        if self.active:
            return self.path            # type: ignore[return-value]
        self.dir.mkdir(parents=True, exist_ok=True)
        suffix = label or self.cfg.logging.filename_suffix
        stamp = datetime.now().strftime("%y%m%d_%H%M%S")
        self.path = self.dir / f"{stamp}_{suffix}"
        self._fh = self.path.open("w", encoding="utf-8", newline="")
        self._fh.write("\t".join(self.cfg.logging.columns.keys()) + "\n")
        self._fh.flush()

        if self.cfg.logging.extended_log:
            self.ext_path = self.dir / f"{stamp}_{suffix}_extended.csv"
            self._ext_fh = self.ext_path.open("w", encoding="utf-8", newline="")
            self._ext_keys = []          # header written with the first sample

        self.started_at = time.time()
        self.rows = 0
        return self.path

    def write_ald_snapshot(self, params: dict, recipe) -> Path:
        """Write a one-time JSON snapshot of an ALD run's settings at start:
        flow rates, phase timings, cycle count, gas schedule, everything.

        Independent of whether tab-delimited logging (Start/Stop) is active -
        a run's parameters are recorded even if the operator never clicked
        Start on the Data Logging card. Named on the same timestamp
        convention as the other log files so it's easy to correlate by eye.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%y%m%d_%H%M%S")
        path = self.dir / f"{stamp}_ald_run_params.json"
        snapshot = {
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "recipe": recipe.model_dump(mode="json"),
            "ui_params": params,
            "summary": _describe_ald_recipe(recipe),
        }
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return path

    def start_run_export(self, recipe_name: str, started_at: float) -> Path:
        """Open a per-run CSV, named on the run's own start time so it lines up
        with what the browser would otherwise have downloaded (and survives a
        closed browser, which the client-side download does not).

        Independent of the operator-toggled logger above and of
        write_ald_snapshot's one-time params JSON - this is the actual
        time-series trace, at whatever rate _current_cycle samples (5 Hz
        default), covering every recipe (ALD, CVD, and file recipes alike),
        not just ALD/CVD.
        """
        self.stop_run_export()          # a stale handle must never linger
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(started_at).strftime("%y%m%d_%H%M%S")
        slug = "".join(c if c.isalnum() else "_" for c in recipe_name).strip("_") or "run"
        self.run_path = self.dir / f"{stamp}_{slug}_run.csv"
        self._run_fh = self.run_path.open("w", encoding="utf-8", newline="")
        self.bycycle_path = self.dir / f"{stamp}_{slug}_bycycle.csv"
        self._bycycle_fh = self.bycycle_path.open("w", encoding="utf-8", newline="")
        self.run_started_at = started_at
        self.run_rows = 0
        self.bycycle_rows = 0
        self._run_keys = []             # headers written with the first sample
        self._bycycle_keys = []
        return self.run_path

    def write_run_sample(self, sample: dict[str, Any], progress=None) -> None:
        """Append one row from the sample dict _current_cycle already builds
        every telemetry tick - no extra device I/O, just a write if a run is
        active. No-op if no run export is open. `progress` (the recipe's
        RecipeProgress) adds cycle/step context, mirroring the manual
        extended log's `extra` fields."""
        if self._run_fh is None:
            return
        elapsed = sample.get("t", 0.0) - (self.run_started_at or 0.0)
        cyc_num = getattr(progress, "cycle_fraction", None)
        paused = bool(getattr(progress, "paused", False))
        extra = {
            "recipe_cycle": getattr(progress, "cycle", ""),
            "cycle_number": cyc_num,
            "paused": paused,
            "recipe_step": getattr(progress, "step_desc", ""),
        }
        if not self._run_keys:
            self._run_keys = sorted(k for k in sample if k != "t")
            header = ["elapsed_s", "iso_time", *self._run_keys, *extra]
            self._run_fh.write(",".join(header) + "\n")

        def csv(v: Any) -> str:
            s = v if isinstance(v, str) else self._fmt(v)
            return f'"{s}"' if ("," in s or '"' in s) else s

        # Absolute wall-clock, so the ellipsometer merge can join this run's
        # channels to the ellipsometry (which is anchored to the same reactor
        # clock via its sidecar). elapsed_s stays first for back-compat.
        iso = datetime.fromtimestamp(
            sample.get("t") or time.time()).isoformat(timespec="milliseconds")
        row = [f"{elapsed:.3f}", iso,
               *(self._fmt(sample.get(k)) for k in self._run_keys),
               *(csv(v) for v in extra.values())]
        try:
            self._run_fh.write(",".join(row) + "\n")
            self._run_fh.flush()
            self.run_rows += 1
        except Exception:
            pass

        # Plot-ready by-cycle row: same channels keyed by fractional cycle
        # number, only for real (non-paused) in-cycle samples. Written in time
        # order, and cycle_number is monotonic across kept samples, so the file
        # is already sorted by cycle - no post-processing needed.
        if (self._bycycle_fh is not None and not paused
                and isinstance(cyc_num, (int, float))):
            if not self._bycycle_keys:
                self._bycycle_keys = self._run_keys
                self._bycycle_fh.write(
                    ",".join(["cycle_number", *self._bycycle_keys, "recipe_step"]) + "\n")
            brow = [f"{cyc_num:.6f}",
                    *(self._fmt(sample.get(k)) for k in self._bycycle_keys),
                    csv(getattr(progress, "step_desc", ""))]
            try:
                self._bycycle_fh.write(",".join(brow) + "\n")
                self._bycycle_fh.flush()
                self.bycycle_rows += 1
            except Exception:
                pass

    def stop_run_export(self) -> None:
        for fh in (self._run_fh, self._bycycle_fh):
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.flush()
                    fh.close()
        self._run_fh = None
        self._bycycle_fh = None
        self.run_started_at = None

    # -- ellipsometer sidecar ---------------------------------------------- #

    @property
    def ellipsometer_active(self) -> bool:
        return self._ell_fh is not None

    def start_ellipsometer_capture(self, started_at: float) -> Path:
        """Open a new per-acquisition ellipsometer sidecar, named on the
        reactor-clock time of the acquisition's first streamed point (closing
        any previous one). Each row is a live FS-1 measurement paired with the
        reactor clock; reactor/analysis/ellipsometer_merge.py fits the
        fs_time -> reactor_epoch line from these and applies it to a downloaded
        refit file."""
        self.stop_ellipsometer_capture()
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(started_at).strftime("%y%m%d_%H%M%S")
        self.ell_path = self.dir / f"{stamp}_ellipsometer.csv"
        self._ell_fh = self.ell_path.open("w", encoding="utf-8", newline="")
        self._ell_fh.write("point_index,fs_time_s,reactor_epoch,reactor_iso,"
                           "thickness_live,thickness_unit,fit_diff,intensity,"
                           "temp,align_x,align_y\n")
        self._ell_fh.flush()
        self.ell_started_at = started_at
        self.ell_rows = 0
        return self.ell_path

    def write_ellipsometer_point(self, point: Any) -> None:
        """Append one streamed measurement. `point` is duck-typed on
        EllipsometerPoint (index, time_s, thickness, thickness_unit, fit_diff,
        intensity, temp, align_x, align_y, t_recv). No-op if no sidecar is open.
        `thickness_live` is the instrument's uncalibrated fit - a cross-check,
        never the answer."""
        if self._ell_fh is None:
            return
        iso = datetime.fromtimestamp(point.t_recv).isoformat(timespec="milliseconds")
        row = [
            self._fmt(getattr(point, "index", None)),
            self._fmt(getattr(point, "time_s", None)),
            f"{point.t_recv:.3f}",
            iso,
            self._fmt(getattr(point, "thickness", None)),
            getattr(point, "thickness_unit", "") or "",
            self._fmt(getattr(point, "fit_diff", None)),
            self._fmt(getattr(point, "intensity", None)),
            self._fmt(getattr(point, "temp", None)),
            self._fmt(getattr(point, "align_x", None)),
            self._fmt(getattr(point, "align_y", None)),
        ]
        try:
            self._ell_fh.write(",".join(row) + "\n")
            self._ell_fh.flush()
            self.ell_rows += 1
        except Exception:
            pass

    def stop_ellipsometer_capture(self) -> None:
        if self._ell_fh is not None:
            with contextlib.suppress(Exception):
                self._ell_fh.flush()
                self._ell_fh.close()
        self._ell_fh = None
        self.ell_started_at = None

    def stop(self) -> None:
        for fh in (self._fh, self._ext_fh):
            if fh is not None:
                try:
                    fh.flush()
                    fh.close()
                except Exception:
                    pass
        self._fh = None
        self._ext_fh = None
        self.started_at = None

    def close(self) -> None:
        self.stop()
        self.stop_run_export()
        self.stop_ellipsometer_capture()

    # -- writing ------------------------------------------------------------ #

    @staticmethod
    def _fmt(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float):
            return f"{v:.6g}"
        return str(v)

    def write_sample(self, snapshot: dict[str, Any], progress=None) -> None:
        if self._fh is None:
            return

        elapsed = time.time() - (self.started_at or time.time())
        row = [
            f"{elapsed:.3f}" if key == ELAPSED_KEY else self._fmt(snapshot.get(key))
            for key in self.cfg.logging.columns.values()
        ]
        try:
            self._fh.write("\t".join(row) + "\n")
            self._fh.flush()
            self.rows += 1
        except Exception:
            pass

        if self._ext_fh is not None:
            self._write_extended(snapshot, elapsed, progress)

    def _write_extended(self, snapshot, elapsed, progress) -> None:
        extra = {
            "recipe_state": getattr(progress, "state", ""),
            "recipe_cycle": getattr(progress, "cycle", ""),
            "recipe_step": getattr(progress, "step_desc", ""),
        }
        if not self._ext_keys:
            self._ext_keys = sorted(k for k in snapshot if not k.endswith(".volts"))
            header = ["iso_time", "elapsed_s", *self._ext_keys, *extra]
            self._ext_fh.write(",".join(header) + "\n")

        def csv(v: Any) -> str:
            s = v if isinstance(v, str) else self._fmt(v)
            return f'"{s}"' if ("," in s or '"' in s) else s

        row = [
            datetime.now().isoformat(timespec="milliseconds"),
            f"{elapsed:.3f}",
            *[self._fmt(snapshot.get(k)) for k in self._ext_keys],
            *[csv(v) for v in extra.values()],
        ]
        try:
            self._ext_fh.write(",".join(row) + "\n")
            self._ext_fh.flush()
        except Exception:
            pass

    def status(self) -> dict:
        return {
            "active": self.active,
            "path": str(self.path) if self.path else None,
            "extended_path": str(self.ext_path) if self.ext_path else None,
            "rows": self.rows,
            "started_at": self.started_at,
            "dir": str(self.dir),
            "run_export": {
                "active": self.run_export_active,
                "path": str(self.run_path) if self.run_path else None,
                "rows": self.run_rows,
                "bycycle_path": str(self.bycycle_path) if self.bycycle_path else None,
                "bycycle_rows": self.bycycle_rows,
            },
            "ellipsometer": {
                "active": self.ellipsometer_active,
                "path": str(self.ell_path) if self.ell_path else None,
                "rows": self.ell_rows,
            },
        }
