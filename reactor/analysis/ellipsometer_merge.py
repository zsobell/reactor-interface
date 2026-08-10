"""Put a refit FS-1 dynamic file onto the reactor clock.

The FS-1 dynamic file you download *after refitting* has the correct thickness
trace, but its time axis is relative seconds from the start of the acquisition
- there is no absolute timestamp in it. The reactor, meanwhile, captured every
streamed point live and wrote a sidecar of ``(fs_time -> reactor_clock)`` pairs
(see reactor/devices/ellipsometer.py and DataLogger.*_ellipsometer_*). This
module joins the two so every refit thickness gets an absolute, reactor-clock
timestamp, then optionally interleaves the reactor's own channels.

The join is deliberately *not* a fragile row-for-row index match. We fit a
straight line ``reactor_epoch = a + b * fs_time`` through the sidecar pairs
(b is ~1.0 - both axes are real seconds) and apply it to every row of the
refit file. That averages out per-point network jitter and still works if the
live capture happened to miss a sample or two.

Everything here is pure text/number munging - no numpy, no hardware, no I/O
beyond the caller handing us file contents.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone

DYN_MAGIC = "Film_Sense_Dyn_Data"


# --------------------------------------------------------------------------- #
#  FS-1 dynamic file
# --------------------------------------------------------------------------- #

@dataclass
class DynData:
    names: list[str]                       # column headings, in file order
    columns: dict[str, list[float]]        # heading -> values (raw, file units)
    wavelengths: list[float]               # nm, from the header block
    time: list[float]                      # relative time, ALWAYS in seconds
    thickness_col: str                     # e.g. "Thick(A).1" / "Thick(nm).1"
    thickness_unit: str                    # "A" / "nm"
    time_unit: str = "s"                   # the file's own Time unit ("min"/"s")

    @property
    def n_points(self) -> int:
        return len(self.time)


def parse_dyn_file(text: str) -> DynData:
    """Parse a refit FS-1 dynamic file into named columns. Two shapes accepted:

    * The **refit export you download** (FS-1 dynamic-measurements screen ->
      "show stats" -> "Click to download data") - the normal input to the merge.
      A plain tab-delimited table whose FIRST line is the column header, led by
      "Time"::

          Time <tab> Thick(A).1 [<tab> rho(uOhm*cm)] [<tab> ...]
          <tab-separated data rows>

      Any extra columns (rho, etc.) are carried through as-is, so a file with a
      resistivity column keeps it and one without simply doesn't have it. NOTE:
      this export's Time column is in MINUTES; it is normalized to seconds so it
      lines up with the seconds-based live stream / sidecar.

    * The richer internal **``Film_Sense_Dyn_Data``** file (as saved under
      ``dyndata/``): a magic line, a counts line, per-wavelength rows, then the
      same tab-delimited header + data.

    The format is chosen by the first line, so callers just hand over whatever
    the user dropped in.
    """
    # FS-1 files can use "\r\r\n" (double CR) line endings, which
    # str.splitlines() turns into a blank line between every real line.
    # Normalize all CR/LF variants and drop the artifact blanks.
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in norm.split("\n") if ln.strip() != ""]
    if not lines:
        raise ValueError("empty ellipsometer file")
    if lines[0].strip().startswith(DYN_MAGIC):
        return _parse_rich(lines)
    return _parse_columnar(lines)


def _thickness_col(names: list[str]) -> tuple[str, str]:
    """The thickness column and its unit, e.g. ("Thick(A).1", "A")."""
    col = next((n for n in names if n.startswith("Thick")), "")
    unit = ""
    if "(" in col and ")" in col:
        unit = col[col.index("(") + 1:col.index(")")]
    return col, unit


def _read_columns(names: list[str], data_lines: list[str]) -> dict[str, list[float]]:
    columns: dict[str, list[float]] = {n: [] for n in names}
    for raw in data_lines:
        cells = raw.split("\t")
        if len(cells) < len(names):
            continue                       # trailing/partial line
        for name, cell in zip(names, cells):
            try:
                columns[name].append(float(cell))
            except ValueError:
                columns[name].append(float("nan"))
    return columns


def _parse_columnar(lines: list[str]) -> DynData:
    """The downloaded refit export: header row led by 'Time', then tab data.

    IMPORTANT: the download's Time column is in MINUTES (confirmed by Zach),
    whereas the live 4001 stream - and therefore the sidecar's fs_time_s - is
    in SECONDS. We normalize Time to seconds here so the merge's time-map lines
    the two up correctly instead of squashing an 18-minute run into 18 seconds.
    """
    names = [h.strip() for h in lines[0].split("\t") if h.strip() != ""]
    if "Time" not in names:
        raise ValueError(
            "not a recognized ellipsometer file: expected a tab-delimited "
            f"header with a 'Time' column, got {names[:6]}")
    columns = _read_columns(names, lines[1:])
    thickness_col, unit = _thickness_col(names)
    time_s = [v * 60.0 for v in columns["Time"]]        # minutes -> seconds
    return DynData(names=names, columns=columns, wavelengths=[],
                   time=time_s, thickness_col=thickness_col,
                   thickness_unit=unit, time_unit="min")


def _parse_rich(lines: list[str]) -> DynData:
    """The internal Film_Sense_Dyn_Data file: magic / counts / wavelengths /
    header / data."""
    counts = lines[1].split()
    try:
        n_wavelengths = int(float(counts[0]))
    except (IndexError, ValueError) as exc:
        raise ValueError(f"bad header count line {lines[1]!r}: {exc}") from exc

    wl_rows = lines[2:2 + n_wavelengths]
    wavelengths: list[float] = []
    for row in wl_rows:
        tok = row.split()
        if tok:
            try:
                wavelengths.append(float(tok[0]))
            except ValueError:
                pass

    header_idx = 2 + n_wavelengths
    if header_idx >= len(lines):
        raise ValueError("file ends before the column header row")
    names = [h.strip() for h in lines[header_idx].split("\t") if h.strip() != ""]
    if not names or names[0] != "Time":
        raise ValueError(f"expected a Time-led column header, got {names[:4]}")

    columns = _read_columns(names, lines[header_idx + 1:])
    thickness_col, unit = _thickness_col(names)
    return DynData(names=names, columns=columns, wavelengths=wavelengths,
                   time=columns["Time"], thickness_col=thickness_col,
                   thickness_unit=unit, time_unit="s")   # internal file: seconds


# --------------------------------------------------------------------------- #
#  Reactor-side sidecar (written live from the 4001 stream)
# --------------------------------------------------------------------------- #

@dataclass
class Sidecar:
    fs_time: list[float]                   # FS-1 relative seconds, per point
    reactor_epoch: list[float]             # reactor wall-clock (Unix s), per point
    index: list[int] = field(default_factory=list)


def parse_sidecar(text: str) -> Sidecar:
    """Parse the reactor's ellipsometer sidecar CSV. Requires columns
    ``fs_time_s`` and ``reactor_epoch`` (``point_index`` is used if present)."""
    reader = csv.DictReader(io.StringIO(text))
    fs, ep, idx = [], [], []
    for row in reader:
        try:
            fs.append(float(row["fs_time_s"]))
            ep.append(float(row["reactor_epoch"]))
        except (KeyError, ValueError):
            continue
        try:
            idx.append(int(float(row.get("point_index", "nan"))))
        except ValueError:
            idx.append(-1)
    if not fs:
        raise ValueError("sidecar has no usable (fs_time_s, reactor_epoch) rows")
    return Sidecar(fs_time=fs, reactor_epoch=ep, index=idx)


# --------------------------------------------------------------------------- #
#  The time map:  reactor_epoch = a + b * fs_time
# --------------------------------------------------------------------------- #

@dataclass
class TimeMap:
    a: float                               # intercept (reactor epoch at fs_time 0)
    b: float                               # slope (s per s, ~1.0)
    n: int                                 # sidecar pairs used
    max_residual_s: float                  # worst |fit - actual|, a jitter gauge

    def to_epoch(self, fs_time: float) -> float:
        return self.a + self.b * fs_time


def fit_time_map(sidecar: Sidecar) -> TimeMap:
    """Ordinary least-squares line through the sidecar pairs. With one pair we
    fall back to slope 1.0 (pure offset); the reactor and instrument both count
    real seconds, so b is expected to sit within a hair of 1.0."""
    xs, ys = sidecar.fs_time, sidecar.reactor_epoch
    n = len(xs)
    if n == 1:
        return TimeMap(a=ys[0] - xs[0], b=1.0, n=1, max_residual_s=0.0)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx if sxx else 1.0
    a = my - b * mx
    max_res = max(abs(y - (a + b * x)) for x, y in zip(xs, ys))
    return TimeMap(a=a, b=b, n=n, max_residual_s=max_res)


# --------------------------------------------------------------------------- #
#  Optional reactor telemetry (the extended log carries an absolute iso_time)
# --------------------------------------------------------------------------- #

def _parse_iso(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


@dataclass
class ReactorLog:
    epoch: list[float]                     # per-row Unix seconds (from iso_time)
    rows: list[dict[str, str]]
    channels: list[str]


def parse_reactor_extended(text: str) -> ReactorLog:
    """Parse a reactor extended CSV (has an ``iso_time`` column). Rows without a
    parseable iso_time are dropped."""
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    skip = {"iso_time", "elapsed_s"}
    channels = [c for c in cols if c not in skip]
    epoch, rows = [], []
    for row in reader:
        e = _parse_iso(row.get("iso_time", ""))
        if e is None:
            continue
        epoch.append(e)
        rows.append(row)
    return ReactorLog(epoch=epoch, rows=rows, channels=channels)


def _nearest(sorted_epochs: list[float], target: float) -> int:
    """Index of the nearest value in an ascending list (binary search)."""
    import bisect
    if not sorted_epochs:
        return -1
    j = bisect.bisect_left(sorted_epochs, target)
    if j <= 0:
        return 0
    if j >= len(sorted_epochs):
        return len(sorted_epochs) - 1
    before, after = sorted_epochs[j - 1], sorted_epochs[j]
    return j if (after - target) < (target - before) else j - 1


# --------------------------------------------------------------------------- #
#  Merge
# --------------------------------------------------------------------------- #

@dataclass
class MergeResult:
    rows: list[dict[str, object]]
    header: list[str]
    time_map: TimeMap
    n_points: int
    warnings: list[str]
    mode: str = "combined"                 # "combined" | "ellipsometer_only"
    reactor_channels: list[str] = field(default_factory=list)
    ellipsometry_columns: list[str] = field(default_factory=list)

    def to_csv(self) -> str:
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=self.header, extrasaction="ignore")
        w.writeheader()
        w.writerows(self.rows)
        return out.getvalue()


# --------------------------------------------------------------------------- #
#  Reactor run export (the backbone): iso_time + cycle_number + paused + channels
# --------------------------------------------------------------------------- #

_RUN_META = {"elapsed_s", "iso_time", "recipe_cycle", "cycle_number", "paused",
             "recipe_step"}


@dataclass
class ReactorRun:
    epoch: list[float]                     # per-row Unix seconds (from iso_time)
    cycle_number: list[float | None]       # fractional cycle; None off-cycle
    paused: list[bool]                     # reignite / operator pause
    elapsed_s: list[float]
    iso: list[str]
    recipe_step: list[str]
    channels: list[str]                    # data-channel column names
    rows: list[dict[str, str]]
    has_cycle_number: bool = True          # False for logs predating the feature
    has_iso_time: bool = True


def parse_reactor_run(text: str) -> ReactorRun:
    """Parse the automatic reactor run export (``<stamp>_<slug>_run.csv``):
    absolute iso_time, fractional cycle_number, a paused flag, and every data
    channel. This is the backbone of the combined plot-ready merge."""
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    channels = [c for c in cols if c not in _RUN_META]
    has_cycle = "cycle_number" in cols
    has_iso = "iso_time" in cols
    ep, cyc, pau, el, iso, step, rows = [], [], [], [], [], [], []
    for row in reader:
        e = _parse_iso(row.get("iso_time", ""))
        if e is None:
            continue
        ep.append(e)
        try:
            cyc.append(float(row["cycle_number"]))
        except (KeyError, ValueError):
            cyc.append(None)
        pau.append(str(row.get("paused", "")).strip() in ("1", "True", "true"))
        try:
            el.append(float(row.get("elapsed_s", "nan")))
        except ValueError:
            el.append(float("nan"))
        iso.append(row.get("iso_time", ""))
        step.append(row.get("recipe_step", ""))
        rows.append(row)
    return ReactorRun(epoch=ep, cycle_number=cyc, paused=pau, elapsed_s=el,
                      iso=iso, recipe_step=step, channels=channels, rows=rows,
                      has_cycle_number=has_cycle, has_iso_time=has_iso)


def _interp(xs: list[float], ys: list[float], x: float) -> float | None:
    """Linear interpolation of ys at x over ascending xs; None outside range."""
    import bisect
    if not xs or x < xs[0] or x > xs[-1]:
        return None
    j = bisect.bisect_left(xs, x)
    if j < len(xs) and xs[j] == x:
        return ys[j]
    if j == 0:
        return ys[0]
    x0, x1, y0, y1 = xs[j - 1], xs[j], ys[j - 1], ys[j]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _num(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return "" if v != v else f"{v:.6g}"        # NaN -> ""
    return str(v)


def _shared_warnings(dyn: DynData, sidecar: Sidecar, tmap: TimeMap) -> list[str]:
    warnings: list[str] = []
    if not (0.98 <= tmap.b <= 1.02):
        warnings.append(
            f"time-map slope {tmap.b:.4f} is not ~1.0 - the reactor and FS-1 "
            f"clocks may disagree on rate, or the sidecar is suspect")
    if dyn.time and sidecar.fs_time:
        refit_span = max(dyn.time) - min(dyn.time)
        side_span = max(sidecar.fs_time) - min(sidecar.fs_time)
        if refit_span > 1.0 and side_span > 1.0:
            ratio = max(refit_span, side_span) / min(refit_span, side_span)
            if ratio > 10:
                warnings.append(
                    f"refit span {refit_span:.0f}s vs sidecar span {side_span:.0f}s "
                    f"differ by {ratio:.0f}x - check the refit file's time units "
                    f"(the download is in minutes, the live stream in seconds)")
    return warnings


def merge(
    dyn: DynData,
    sidecar: Sidecar,
    reactor_run: ReactorRun | None = None,
    *,
    reactor_channels: list[str] | None = None,
) -> MergeResult:
    """Produce a single plot-ready table.

    With a ``reactor_run`` (the normal case) the output is the COMBINED file:
    the reactor run is the backbone (every logged channel, keyed by fractional
    cycle_number), reignite/operator-paused samples are dropped, and the
    ellipsometry columns (thickness, rho, ...) are linearly interpolated onto
    each reactor sample's time. One row per real reactor sample - ready to plot
    any property, reactor or ellipsometer, against cycle number.

    Without a ``reactor_run`` it falls back to the ellipsometry alone, on the
    reactor clock (no cycle number, nothing dropped)."""
    tmap = fit_time_map(sidecar)
    warnings = _shared_warnings(dyn, sidecar, tmap)
    ell_cols = [n for n in dyn.names if n != "Time"]
    ell_epochs = [tmap.to_epoch(t) for t in dyn.time]

    if reactor_run is None:
        return _merge_ellipsometer_only(dyn, tmap, ell_cols, warnings)

    # A run export from before the cycle-numbering / iso_time columns were added
    # would silently produce a headers-only file (every row skipped for lack of a
    # cycle number). Say so plainly instead.
    if not reactor_run.has_cycle_number or not reactor_run.has_iso_time:
        missing = " and ".join(
            m for m, ok in (("cycle_number", reactor_run.has_cycle_number),
                            ("iso_time", reactor_run.has_iso_time)) if not ok)
        warnings.insert(0,
            f"the selected reactor run is missing its {missing} column(s) - it "
            f"predates the cycle-numbering feature. Re-run the recipe to get a "
            f"compatible run export; this one can't be merged.")

    chans = reactor_channels or reactor_run.channels
    header = (["cycle_number", "reactor_elapsed_s", "reactor_iso"]
              + ell_cols + chans + ["recipe_step"])
    rows: list[dict[str, object]] = []
    dropped_paused = 0
    for i, ep in enumerate(reactor_run.epoch):
        cyc = reactor_run.cycle_number[i]
        if cyc is None:
            continue                       # setup/teardown - no cycle number
        if reactor_run.paused[i]:
            dropped_paused += 1
            continue                       # reignite / operator pause
        row: dict[str, object] = {
            "cycle_number": f"{cyc:.6f}",
            "reactor_elapsed_s": _num(reactor_run.elapsed_s[i]),
            "reactor_iso": reactor_run.iso[i],
            "recipe_step": reactor_run.recipe_step[i],
        }
        for col in ell_cols:
            row[col] = _num(_interp(ell_epochs, dyn.columns[col], ep))
        for ch in chans:
            row[ch] = reactor_run.rows[i].get(ch, "")
        rows.append(row)

    if not rows:
        warnings.append("no in-cycle, non-paused reactor samples to merge")
    if dropped_paused:
        warnings.append(f"dropped {dropped_paused} paused (reignite) sample(s)")
    if ell_epochs and reactor_run.epoch and (
            ell_epochs[0] > reactor_run.epoch[0] + 5
            or ell_epochs[-1] < reactor_run.epoch[-1] - 5):
        warnings.append(
            "ellipsometry does not span the whole run - reactor samples outside "
            "its time range have blank thickness")

    return MergeResult(rows=rows, header=header, time_map=tmap, n_points=len(rows),
                       warnings=warnings, mode="combined",
                       reactor_channels=chans, ellipsometry_columns=ell_cols)


def _merge_ellipsometer_only(dyn: DynData, tmap: TimeMap, ell_cols: list[str],
                             warnings: list[str]) -> MergeResult:
    header = ["reactor_iso", "reactor_epoch", "reactor_elapsed_s", "fs_time_s"] + ell_cols
    t0 = tmap.to_epoch(dyn.time[0]) if dyn.time else 0.0
    rows: list[dict[str, object]] = []
    for i, ft in enumerate(dyn.time):
        ep = tmap.to_epoch(ft)
        row: dict[str, object] = {
            "reactor_iso": datetime.fromtimestamp(ep).isoformat(timespec="milliseconds"),
            "reactor_epoch": f"{ep:.3f}",
            "reactor_elapsed_s": f"{ep - t0:.3f}",
            "fs_time_s": f"{ft:.4f}",
        }
        for col in ell_cols:
            row[col] = _num(dyn.columns[col][i])
        rows.append(row)
    return MergeResult(rows=rows, header=header, time_map=tmap, n_points=len(rows),
                       warnings=warnings, mode="ellipsometer_only",
                       reactor_channels=[], ellipsometry_columns=ell_cols)


def merge_text(
    dyn_text: str,
    sidecar_text: str,
    reactor_run_text: str | None = None,
    **kw,
) -> MergeResult:
    """Convenience wrapper: parse raw file contents and merge. ``reactor_run_text``
    is the automatic reactor run export (``<stamp>_<slug>_run.csv``)."""
    dyn = parse_dyn_file(dyn_text)
    side = parse_sidecar(sidecar_text)
    run = parse_reactor_run(reactor_run_text) if reactor_run_text else None
    return merge(dyn, side, run, **kw)
