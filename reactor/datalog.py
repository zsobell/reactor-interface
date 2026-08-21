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
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ReactorConfig

#: column value that means "seconds since logging started"
ELAPSED_KEY = "_elapsed"

#: A run name ending in digits, e.g. "Mo-014" -> ("Mo-", "014"). The digit run is
#: what increments; its width is preserved so Mo-009 advances to Mo-010, not
#: Mo-10.
_RUN_NAME_TAIL = re.compile(r"^(.*?)(\d+)$")


def sanitize_run_name(name: str) -> str:
    """Reduce an operator-typed run name to something safe as a filename stem.

    Keeps letters, digits, dash, dot and underscore (so "Mo-014" survives
    untouched); anything else becomes an underscore. Deliberately not lowercased
    - "Mo" is an element symbol, not a style choice.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-._") else "_" for c in (name or ""))
    return cleaned.strip("_. ")[:64]


def next_run_name(previous: str) -> str:
    """The name after `previous`, incrementing its trailing number.

    "Mo-014" -> "Mo-015", "Mo-009" -> "Mo-010" (width kept), "Mo-099" ->
    "Mo-100" (width grows only when it has to). A name with no trailing digits
    gets "-001" appended, and an empty/absent previous run starts at "run-001".
    """
    prev = sanitize_run_name(previous)
    if not prev:
        return "run-001"
    m = _RUN_NAME_TAIL.match(prev)
    if not m:
        return f"{prev}-001"
    head, digits = m.group(1), m.group(2)
    return f"{head}{str(int(digits) + 1).zfill(len(digits))}"


def _describe_recipe(recipe) -> str:
    """Plain-English summary of a built EE-ALD / EE-CVD Recipe - cycle
    architecture and the gas-schedule timeline, for a human skimming the run
    snapshot file.

    The gas window is anchored differently per mode, and this mirrors exactly
    what the runner measures against (see control/recipe.py's GasSchedule):
    EE-ALD divides up the beam step's exposure, EE-CVD the whole cycle. Both
    handoffs move the incoming gas earlier by the single Recipe.gas_overlap_s.
    """
    lines = [f"{recipe.name} — {recipe.cycles} cycles"]
    for s in recipe.steps:
        lines.append(f"  {s.describe()}")
    if not recipe.gas_schedules:
        return "\n".join(lines)

    cvd = recipe.mode == "cvd"
    if cvd:
        span, anchor = recipe.cycle_seconds(), "cycle"
    else:
        beam = next((s for s in recipe.steps if s.op == "electron_beam"), None)
        span, anchor = ((beam.seconds or 0.0) if beam else 0.0), "beam"
    if span <= 0:
        return "\n".join(lines)

    ov = recipe.gas_overlap_s
    first = next((g for g in recipe.gas_schedules if g.order == "first"), None)
    second = next((g for g in recipe.gas_schedules if g.order == "second"), None)
    handoff = (first.pct / 100.0 * span) if first else 0.0
    second_off = min(span, handoff + (second.pct / 100.0 * span if second else 0.0))

    lines.append(f"  gas schedule (relative to {anchor} start, "
                 f"{ov:g}s handoff overlap):")
    if first:
        # EE-CVD has no run-up before a cycle, so "first" re-arms before this
        # cycle ends rather than leading a beam step (RecipeRunner._build_gas_plan).
        on = (f"on at {anchor}+0s, re-arms at {anchor}+{max(0.0, second_off - ov):g}s"
              if cvd else f"on at {anchor}-{ov:g}s")
        lines.append(f"    {first.mfc}: {on}, off at {anchor}+{handoff:g}s "
                     f"({first.pct:g}% @ {first.flow_sccm:g} sccm)")
    if second:
        lines.append(
            f"    {second.mfc}: on at {anchor}+{max(0.0, handoff - ov):g}s, "
            f"off at {anchor}+{second_off:g}s "
            f"({second.pct:g}% @ {second.flow_sccm:g} sccm)")
    return "\n".join(lines)


#: Report line separator, kept as a name so the literal never has to survive
#: a round-trip through a shell heredoc.
NL = chr(10)


def _fmt_value(v) -> str:
    """One parameter value, as an operator would want to read it."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    if v is None or v == "":
        return "-"
    return str(v)


def _param_block(items, indent: str = "  ") -> list[str]:
    """Key/value lines with the values column-aligned."""
    items = [(str(k), _fmt_value(v)) for k, v in items]
    if not items:
        return [f"{indent}(none)"]
    width = max(len(k) for k, _ in items)
    return [f"{indent}{k.ljust(width)}   {v}" for k, v in items]


def _step_lines(steps) -> list[str]:
    """Numbered recipe steps. Step.describe() already carries the duration for
    every step that has one, so there is no separate duration column."""
    if not steps:
        return ["  (none)"]
    out = []
    for i, s in enumerate(steps, start=1):
        gated = "   [freezes while the plasma is out]" if s.lit_gated else ""
        out.append(f"  {i:>2}. {s.describe()}{gated}")
    return out


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def format_run_params(params: dict, recipe, run_name: str = "",
                      recorded_at: datetime | None = None) -> str:
    """The run's settings as a plain-text report.

    This used to be a JSON dump. Zach could not open it ("I dont know how to
    open them"), which is fair: a .json has no default handler on this machine
    and the nesting made it unreadable anyway. A .txt double-clicks into
    Notepad and reads like the settings sheet it is. Nothing is dropped -
    every UI parameter and every recipe step is still here, just laid out.
    """
    when = recorded_at or datetime.now()
    cycle_s = recipe.cycle_seconds()
    total_s = cycle_s * recipe.cycles
    mode = "EE-CVD" if recipe.mode == "cvd" else "EE-ALD"

    L: list[str] = []
    L.append("RUN PARAMETERS")
    L.append("=" * 60)
    L.append("")
    L += _param_block([
        ("Run", run_name or "(unnamed)"),
        ("Recorded", when.strftime("%Y-%m-%d %H:%M:%S")),
        ("Recipe", recipe.name),
        ("Mode", mode),
        ("Cycles", recipe.cycles),
        ("Cycle length", f"{cycle_s:g} s"),
        ("Nominal run time", f"{_fmt_duration(total_s)}  ({total_s:g} s)"),
    ])
    L.append("")
    L.append("Nominal run time is the cycling phase only - the pre-start, the")
    L.append("setup and the end-of-run steps below are not counted, and a")
    L.append("reignite makes the real run longer.")
    L.append("")

    L.append("SUMMARY")
    L.append("-" * 60)
    L += [f"  {ln}" for ln in _describe_recipe(recipe).splitlines()]
    L.append("")

    L.append("PARAMETERS SET IN THE UI")
    L.append("-" * 60)
    L += _param_block(sorted(params.items()))
    L.append("")

    L.append("RECIPE STEPS")
    L.append("-" * 60)
    L.append("Setup (once, before the first cycle)")
    L += _step_lines(recipe.setup)
    L.append("")
    L.append(f"Cycle (repeated {recipe.cycles} times)")
    L += _step_lines(recipe.steps)
    L.append("")
    L.append("End of run")
    L += _step_lines(recipe.teardown)
    L.append("")

    L.append("GAS SCHEDULE")
    L.append("-" * 60)
    if recipe.gas_schedules:
        L += _param_block(
            [(g.mfc, f"{g.order}, {g.pct:g}% of the window @ {g.flow_sccm:g} sccm")
             for g in recipe.gas_schedules])
        L.append(f"  handoff overlap   {recipe.gas_overlap_s:g} s "
                 f"(the incoming gas starts this early)")
    else:
        L.append("  (no scheduled gas)")
    L.append("")
    return NL.join(L) + NL


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
        # Operator's name for the current run (e.g. "Mo-014"); prefixes every
        # file the run writes. Empty = historical timestamp-first naming.
        self.run_name: str = ""
        # One folder per run (data/Mo-015/), holding every file that run writes:
        # trace, by-cycle, parameters, ellipsometer sidecar, and later the
        # merged file. Requested 2026-08-21 - a flat data/ with five files per
        # run became unusable. Set by start_run_export, cleared when it stops;
        # None means "no run in progress", and files fall back to data/.
        self.run_dir: Path | None = None
        self.run_stem: str = ""

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

    def write_run_params(self, params: dict, recipe) -> Path:
        """Write a one-time readable report of a UI-built run's settings at
        start: flow rates, phase timings, cycle count, gas schedule, every step.
        Covers both EE-ALD and EE-CVD.

        Plain text, not JSON, and named exactly like the run's other files -
        `<run>_<stamp>_run_params.txt`, in the run's own folder. It previously
        wrote JSON under a stem of its own (`..._ald_run_params.json`) with a
        timestamp taken at the moment it ran, so it could land a second off the
        rest of the set and sorted apart from it.

        Independent of whether tab-delimited logging (Start/Stop) is active - a
        run's parameters are recorded even if the operator never clicked Start
        on the Data Logging card.
        """
        # Falls back to data/ and its own stamp only if called outside a run,
        # which the supervisor never does (start_run_export runs first).
        target_dir = self.run_dir or self.dir
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = self.run_stem or self._stem(
            datetime.now().strftime("%y%m%d_%H%M%S"))
        path = target_dir / f"{stem}_run_params.txt"
        # utf-8-sig: the report contains an em-dash (from the recipe summary),
        # and the BOM is what makes Notepad and Excel render it rather than
        # guessing the codepage. Harmless everywhere else.
        path.write_text(
            format_run_params(params, recipe, self.run_name),
            encoding="utf-8-sig")
        return path

    def set_run_name(self, run_name: str) -> str:
        """Set the operator's name for the run now starting (e.g. "Mo-014").

        Every file this run produces is prefixed with it, so a run's trace,
        by-cycle, params and ellipsometer sidecar are all identifiable at a
        glance in the data folder instead of only by timestamp. Empty restores
        the historical timestamp-first naming.
        """
        self.run_name = sanitize_run_name(run_name)
        return self.run_name

    def _adopt_open_sidecar(self) -> None:
        """Pull an already-open ellipsometer sidecar into the run now starting:
        rename it with the run's name and move it into the run's folder.

        The FS-1 streams continuously, so its capture almost always starts
        BEFORE Start run - the operator watches the live fit settle first. Its
        name and location were therefore fixed on disk minutes before the run
        existed, which is why the run named "Mo-015" produced a bare
        `260821_131256_ellipsometer.csv` sitting loose in data/ while every
        other file that run wrote carried the name.

        Windows will not rename a file that has an open handle, so this closes,
        moves, and reopens in append mode. The acquisition continues into the
        same file at its new path and nothing already written is lost.
        Best-effort throughout: a failed move leaves the capture running where
        it is rather than costing the operator the acquisition.
        """
        if self._ell_fh is None or self.ell_path is None:
            return
        # The timestamp is the run-name-independent part of the name; matching
        # it explicitly means adopting twice replaces the prefix instead of
        # stacking, whatever the run is called (including an all-digit name).
        m = re.search(r"(\d{6}_\d{6})_ellipsometer\.csv$", self.ell_path.name)
        if not m:
            return
        stamp = m.group(1)
        name = f"{self.run_name}_{stamp}" if self.run_name else stamp
        target = (self.run_dir or self.dir) / f"{name}_ellipsometer.csv"
        if target == self.ell_path:
            return

        with contextlib.suppress(Exception):
            self._ell_fh.flush()
            self._ell_fh.close()
        self._ell_fh = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.ell_path.replace(target)       # replace: works across a move
            self.ell_path = target
        except OSError:
            pass                        # stay where we are, reopen it below
        try:
            self._ell_fh = self.ell_path.open("a", encoding="utf-8", newline="")
        except OSError:
            self._ell_fh = None         # capture stops rather than raising mid-run

    def _run_folder(self, stamp: str) -> Path:
        """The directory one run's files live in.

        Named for the RUN, not for the run's filename stem: every attempt at
        Mo-015 lands in `data/Mo-015/` and the attempts are told apart by the
        timestamp already in each filename, which is what the operator asked
        for ("a new folder with the run name, i.e. Mo-xxx").

        An unnamed run falls back to the bare timestamp (`data/260821_131320/`)
        rather than the full stem, which would drag the recipe slug into the
        folder name and give you `data/260821_131320_ALD___e_beam__precursor_1/`.
        """
        return self.dir / (self.run_name or stamp)

    def _stem(self, stamp: str, slug: str = "") -> str:
        """Filename stem: `<run name>_<stamp>` when named, else the historical
        `<stamp>_<recipe slug>`. Run name leads so one experiment's files sort
        together; the stamp still disambiguates repeats of the same name."""
        if self.run_name:
            return f"{self.run_name}_{stamp}"
        return f"{stamp}_{slug}" if slug else stamp

    def start_run_export(self, recipe_name: str, started_at: float) -> Path:
        """Open a per-run CSV, named on the run's own start time so it lines up
        with what the browser would otherwise have downloaded (and survives a
        closed browser, which the client-side download does not).

        Independent of the operator-toggled logger above and of
        write_run_params' one-time settings report - this is the actual
        time-series trace, at whatever rate _current_cycle samples (5 Hz
        default), covering every recipe (ALD, CVD, and file recipes alike),
        not just ALD/CVD.

        This is also where the run's FOLDER is established, because it is the
        first moment both halves of the name are known: the run name (set just
        before, by the supervisor) and the run's start timestamp. Everything
        else the run writes is placed relative to `self.run_dir`.
        """
        self.stop_run_export()          # a stale handle must never linger
        stamp = datetime.fromtimestamp(started_at).strftime("%y%m%d_%H%M%S")
        slug = "".join(c if c.isalnum() else "_" for c in recipe_name).strip("_") or "run"
        stem = self._stem(stamp, slug)
        self.run_stem = stem
        self.run_dir = self._run_folder(stamp)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_path = self.run_dir / f"{stem}_run.csv"
        self._run_fh = self.run_path.open("w", encoding="utf-8", newline="")
        self.bycycle_path = self.run_dir / f"{stem}_bycycle.csv"
        self._bycycle_fh = self.bycycle_path.open("w", encoding="utf-8", newline="")
        self.run_started_at = started_at
        self.run_rows = 0
        self.bycycle_rows = 0
        self._run_keys = []             # headers written with the first sample
        self._bycycle_keys = []
        # An FS-1 acquisition almost always opened before this run did; pull it
        # in so the whole set lives together under the run's name.
        self._adopt_open_sidecar()
        return self.run_path

    def write_run_sample(self, sample: dict[str, Any], progress=None,
                         blank: set[str] | None = None) -> None:
        """Append one row from the sample dict _current_cycle already builds
        every telemetry tick - no extra device I/O, just a write if a run is
        active. No-op if no run export is open. `progress` (the recipe's
        RecipeProgress) adds cycle/step context, mirroring the manual
        extended log's `extra` fields.

        `blank` is the set of columns NOT measured since the previous row; they
        are written as empty cells. The channels run on independent loops at
        different rates (DAQ ~2 Hz, instruments ~5 Hz, MFCs ~6 Hz), so without
        this a row would repeat whichever reading happened to be sitting in the
        snapshot and the file would claim measurements that never happened. The
        caller decides membership, so commanded state (the valve flags) is simply
        never put in the set - it is always current, never stale."""
        if self._run_fh is None:
            return
        elapsed = sample.get("t", 0.0) - (self.run_started_at or 0.0)
        cyc_num = getattr(progress, "cycle_fraction", None)
        paused = bool(getattr(progress, "paused", False))
        # No separate `paused` column: a reignite and an operator pause name
        # themselves in `recipe_step` (RecipeProgress.log_step). One descriptive
        # column instead of a step plus a 0/1 flag sitting beside it.
        step = (progress.log_step() if hasattr(progress, "log_step")
                else getattr(progress, "step_desc", ""))
        extra = {
            "recipe_cycle": getattr(progress, "cycle", ""),
            "cycle_number": cyc_num,
            "recipe_step": step,
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
        skip = blank or ()
        row = [f"{elapsed:.3f}", iso,
               *("" if k in skip else self._fmt(sample.get(k))
                 for k in self._run_keys),
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
                    *("" if k in skip else self._fmt(sample.get(k))
                      for k in self._bycycle_keys),
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
        # No run in progress: anything opened from here on goes to data/ again.
        # Paths already handed out (run_path, bycycle_path) stay valid.
        self.run_dir = None
        self.run_stem = ""

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
        # Inside a run this belongs in the run's folder; outside one it starts
        # loose in data/ and _adopt_open_sidecar moves it when a run begins.
        target_dir = self.run_dir or self.dir
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(started_at).strftime("%y%m%d_%H%M%S")
        self.ell_path = target_dir / f"{self._stem(stamp)}_ellipsometer.csv"
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
