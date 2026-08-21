"""Post-run merge (reactor/analysis/ellipsometer_merge.py): parse an FS-1
refit file, fit the reactor<->FS-1 time map from the live sidecar, and build the
COMBINED plot-ready file - reactor run channels keyed by cycle number, paused
samples dropped, with the ellipsometry columns interpolated onto them.

Synthetic data gives exact, deterministic assertions; a real on-disk FS-1
dynamic file (if present) is parsed as a smoke check of the header/column
handling against genuine formatting.

Run directly: python -m tests.test_ellipsometer_merge
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

from reactor.analysis.ellipsometer_merge import (
    merge, merge_text, parse_dyn_file, parse_sidecar, fit_time_map,
    parse_reactor_run,
)
from tests._support import Checker

# A tiny but structurally-real FS-1 dynamic file: 2 wavelengths, 5 points at
# 1 s cadence (first at 0.5 s), a growing Thick(A) column plus a couple of raw
# columns. Tab-separated data, exactly like the instrument writes.
DYN = "\n".join([
    "Film_Sense_Dyn_Data",
    "2\t5\t9.9\t2",
    "465.0\t25.0\t6.9\t12.0\t1.0\t1.0\t0.0",
    "635.0\t20.0\t10.0\t4.8\t1.0\t1.0\t0.0",
    "Time\tBlue_N\tAveInt\tFit_Diff\tThick(A).1",
    "0.5\t0.44\t1.78\t0.0021\t2.0",
    "1.5\t0.45\t1.79\t0.0022\t4.0",
    "2.5\t0.46\t1.80\t0.0023\t6.5",
    "3.5\t0.47\t1.79\t0.0024\t9.0",
    "4.5\t0.48\t1.81\t0.0025\t12.0",
    "",
])

# The reactor's live sidecar for that same acquisition: fs_time -> reactor
# epoch, offset by exactly 1_000_000 s, with +/-30 ms of arrival jitter that
# the least-squares fit should average away (slope stays ~1.0).
BASE = 1_000_000.0
SIDE = "\n".join([
    "point_index,fs_time_s,reactor_epoch,reactor_iso",
    f"1,0.5,{BASE + 0.5 + 0.03},x",
    f"2,1.5,{BASE + 1.5 - 0.02},x",
    f"3,2.5,{BASE + 2.5 + 0.01},x",
    f"4,3.5,{BASE + 3.5 - 0.03},x",
    f"5,4.5,{BASE + 4.5 + 0.02},x",
    "",
])


def _iso(ep: float) -> str:
    return datetime.fromtimestamp(ep).isoformat(timespec="milliseconds")


#: Newline, as a name: this file's literals travel through tooling that
#: mangles a bare backslash-n.
LF = chr(10)


async def main() -> int:
    c = Checker("test_ellipsometer_merge")

    # -- FS-1 file parse ---------------------------------------------------- #
    c.section("parse_dyn_file (synthetic)")
    dyn = parse_dyn_file(DYN)
    c.check("5 points", dyn.n_points == 5, f"{dyn.n_points}")
    c.check("2 wavelengths", dyn.wavelengths == [465.0, 635.0], f"{dyn.wavelengths}")
    c.check("thickness col detected", dyn.thickness_col == "Thick(A).1",
            dyn.thickness_col)
    c.check("thickness unit 'A'", dyn.thickness_unit == "A")
    c.check("time column read", dyn.time == [0.5, 1.5, 2.5, 3.5, 4.5], f"{dyn.time}")
    c.check("raw column carried", dyn.columns["AveInt"][2] == 1.80)

    # -- time map fit ------------------------------------------------------- #
    c.section("fit_time_map")
    side = parse_sidecar(SIDE)
    tmap = fit_time_map(side)
    # 5 points over 4 s with +/-30 ms jitter: slope lands within ~1% of 1.0;
    # a real 60 s / 62-point run fits far tighter. (The merge code itself only
    # warns outside 0.98-1.02.)
    c.check("slope ~ 1.0", abs(tmap.b - 1.0) < 1e-2, f"b={tmap.b:.5f}")
    c.check("intercept ~ 1e6", abs(tmap.a - BASE) < 0.05, f"a={tmap.a:.3f}")
    c.check("jitter reported < 40 ms", tmap.max_residual_s < 0.04,
            f"{tmap.max_residual_s*1000:.0f} ms")

    # -- ellipsometry-only fallback (no reactor run) ----------------------- #
    c.section("merge: ellipsometry alone on the reactor clock (fallback)")
    res = merge(dyn, side)
    c.check("mode is ellipsometer_only", res.mode == "ellipsometer_only", res.mode)
    c.check("one row per point", res.n_points == 5)
    r0, r4 = res.rows[0], res.rows[4]
    ep0 = float(r0["reactor_epoch"])
    c.check("row0 reactor_epoch ~ 1e6+0.5", abs(ep0 - (BASE + 0.5)) < 0.05, f"{ep0:.3f}")
    c.check("row0 reactor_iso matches epoch", r0["reactor_iso"] == _iso(ep0), f"{r0['reactor_iso']}")
    c.check("row0 elapsed 0", abs(float(r0["reactor_elapsed_s"])) < 0.01)
    c.check("thickness carried onto row", abs(float(r4["Thick(A).1"]) - 12.0) < 1e-6)
    c.check("no spurious warnings", res.warnings == [], f"{res.warnings}")

    # -- combined merge: reactor run backbone + interpolated ellipsometry --- #
    c.section("merge: combined plot-ready (cycle number, paused dropped)")
    # clean sidecar (no jitter) so the fit is exact: reactor epoch == BASE + fs_time
    CLEANSIDE = "\n".join(["point_index,fs_time_s,reactor_epoch,reactor_iso",
                           f"1,0.5,{BASE + 0.5},x", f"5,4.5,{BASE + 4.5},x"])
    cleanside = parse_sidecar(CLEANSIDE)
    # a reactor run at 0.5 s spacing over BASE+0.5..3.0 with one PAUSED sample
    # (a reignite) mid-cycle; channels pressure + inst_ammeter, fractional cycle.
    RUN = "\n".join([
        "elapsed_s,iso_time,pressure,inst_ammeter,recipe_cycle,cycle_number,paused,recipe_step",
        f"0.0,{_iso(BASE+0.5)},0.010,0.0005,1,0.10,0,dose",
        f"0.5,{_iso(BASE+1.0)},0.020,0.0005,1,0.30,0,wait",
        f"1.0,{_iso(BASE+1.5)},0.020,0.0000,1,0.30,1,electron_beam",     # PAUSED
        f"1.5,{_iso(BASE+2.0)},0.030,0.0005,1,0.60,0,electron_beam",
        f"2.0,{_iso(BASE+2.5)},0.030,0.0005,2,1.10,0,dose",
        f"2.5,{_iso(BASE+3.0)},0.020,0.0005,2,1.40,0,wait",
    ])
    run = parse_reactor_run(RUN)
    resC = merge(dyn, cleanside, run)
    c.check("mode is combined", resC.mode == "combined", resC.mode)
    # UNION of instants: 5 kept reactor samples (1 of 6 paused, dropped) plus
    # the ellipsometry points that land inside the cycling window on a
    # non-paused instant. Of the 5 FS-1 points (BASE+0.5 .. +4.5): +1.5 is
    # nearest the paused sample, and +3.5/+4.5 are past the run's last sample
    # (BASE+3.0) - so 2 survive.
    reactor_rows = [r for r in resC.rows if r["source"] == "reactor"]
    ell_rows = [r for r in resC.rows if r["source"] == "ellipsometer"]
    c.check("paused reactor sample dropped (6 -> 5)", len(reactor_rows) == 5,
            f"{len(reactor_rows)}")
    c.check("ellipsometry points kept as their own rows", len(ell_rows) == 2,
            f"{len(ell_rows)}")
    c.check("total rows are the union", resC.n_points == 7, f"{resC.n_points}")
    c.check("warns it dropped a frozen sample",
            any("reignite" in w or "pause" in w for w in resC.warnings),
            str(resC.warnings))
    c.check("warns it dropped out-of-window ellipsometry",
            any("ellipsometry point" in w for w in resC.warnings), str(resC.warnings))
    c.check("header keyed by cycle_number", resC.header[0] == "cycle_number", str(resC.header[:4]))
    for col in ("cycle_number", "source", "Thick(A).1", "pressure", "inst_ammeter",
                "recipe_step"):
        c.check(f"header has {col}", col in resC.header)
    cyc = [float(r["cycle_number"]) for r in resC.rows]
    c.check("cycle numbers interleave both sources in time order",
            cyc == [0.10, 0.10, 0.30, 0.60, 1.10, 1.10, 1.40], f"{cyc}")
    c.check("cycle_number monotonic", all(cyc[i] <= cyc[i + 1] for i in range(len(cyc) - 1)))
    # Nothing is resampled: a reactor row carries no thickness and an
    # ellipsometry row carries no reactor channel. Blank means "not measured
    # here", which is the whole point of the union.
    c.check("reactor rows have blank thickness",
            all(r["Thick(A).1"] == "" for r in reactor_rows),
            str([r["Thick(A).1"] for r in reactor_rows]))
    c.check("ellipsometry rows have blank reactor channels",
            all(r["pressure"] == "" and r["inst_ammeter"] == "" for r in ell_rows))
    c.check("ellipsometry row carries its own measured thickness (2.0 at +0.5s)",
            abs(float(ell_rows[0]["Thick(A).1"]) - 2.0) < 1e-6, ell_rows[0]["Thick(A).1"])
    c.check("reactor channel carried (pressure)",
            abs(float(reactor_rows[0]["pressure"]) - 0.01) < 1e-6, reactor_rows[0]["pressure"])

    # -- merge_text combined end-to-end ------------------------------------ #
    c.section("merge: the same run in both export formats merges identically")
    # Since 2026-08-21 the run export has no `paused` column - a reignite or an
    # operator pause names itself in `recipe_step` instead. Files written before
    # that still have the column, so the parser honours BOTH and must reach the
    # same answer either way, or older runs stop merging correctly.
    RUN_NEW = LF.join([
        "elapsed_s,iso_time,pressure,inst_ammeter,recipe_cycle,cycle_number,recipe_step",
        f"0.0,{_iso(BASE+0.5)},0.010,0.0005,1,0.10,dose",
        f"0.5,{_iso(BASE+1.0)},0.020,0.0005,1,0.30,wait",
        f"1.0,{_iso(BASE+1.5)},0.020,0.0000,1,0.30,reignite",       # was paused=1
        f"1.5,{_iso(BASE+2.0)},0.030,0.0005,1,0.60,electron_beam",
        f"2.0,{_iso(BASE+2.5)},0.030,0.0005,2,1.10,dose",
        f"2.5,{_iso(BASE+3.0)},0.020,0.0005,2,1.40,wait",
    ])
    run_new = parse_reactor_run(RUN_NEW)
    c.check("new format flags the same rows frozen", run_new.paused == run.paused,
            f"{run_new.paused}")
    c.check("no 'paused' column, so it is not mistaken for a data channel",
            "paused" not in run_new.channels, str(run_new.channels))
    c.check("same data channels either way", run_new.channels == run.channels,
            str(run_new.channels))
    resN = merge(dyn, cleanside, run_new)
    c.check("merged row count matches the old format",
            resN.n_points == resC.n_points, f"{resN.n_points} vs {resC.n_points}")
    c.check("merged cycle numbers match the old format",
            [r["cycle_number"] for r in resN.rows]
            == [r["cycle_number"] for r in resC.rows])
    # An operator pause is the other label, and must drop the same way.
    RUN_HELD = RUN_NEW.replace(",reignite", ",pause")
    c.check("an operator 'pause' row is dropped too",
            sum(parse_reactor_run(RUN_HELD).paused) == 1)

    c.section("merge_text convenience wrapper (combined)")
    res3 = merge_text(DYN, CLEANSIDE, RUN)
    c.check("combined via merge_text", res3.mode == "combined" and res3.n_points == 7,
            f"{res3.mode}, {res3.n_points}")
    c.check("csv first col is cycle_number",
            res3.to_csv().splitlines()[0].startswith("cycle_number"))

    # -- old run export (pre cycle-numbering) must fail loudly ------------- #
    c.section("merge: a run export predating cycle_number warns, no silent empty file")
    OLD_RUN = "\n".join(["elapsed_s,pressure,inst_ammeter,recipe_cycle,recipe_step",
                         "0.0,0.01,0.0005,1,dose",
                         "0.5,0.02,0.0005,1,wait"])
    resO = merge(dyn, cleanside, parse_reactor_run(OLD_RUN))
    c.check("old run -> zero rows", resO.n_points == 0, f"{resO.n_points}")
    c.check("old run -> a clear 'predates' warning",
            any("predates" in w for w in resO.warnings), str(resO.warnings))

    # -- refit export: the columnar format actually downloaded ------------- #
    # (FS-1 dynamic screen -> "show stats" -> download): a plain tab table led
    # by Time (in MINUTES), with Thick and an OPTIONAL rho column. The real
    # input to the merge. Time here is 0.01..0.05 min = 0.6..3.0 s.
    c.section("parse_dyn_file: refit export (Time in MINUTES, normalized to s)")
    COL = "\n".join([
        "Time\tThick(A).1\trho(uOhm*cm)",
        "0.01\t2.0\t2505.3",
        "0.02\t4.0\t1091.6",
        "0.03\t6.5\t880.4",
        "0.04\t9.0\t760.1",
        "0.05\t12.0\t640.0",
    ])
    cd = parse_dyn_file(COL)
    c.check("columnar: no magic line needed", cd.n_points == 5 and cd.wavelengths == [],
            f"{cd.n_points} pts")
    c.check("columnar: thickness col detected", cd.thickness_col == "Thick(A).1",
            cd.thickness_col)
    c.check("columnar: rho kept as a column", "rho(uOhm*cm)" in cd.names, str(cd.names))
    c.check("columnar: source unit flagged as minutes", cd.time_unit == "min")
    c.check("columnar: Time converted min -> s (x60)",
            all(abs(cd.time[i] - 0.01 * (i + 1) * 60) < 1e-6 for i in range(5)),
            f"{[round(t,3) for t in cd.time]}")

    c.section("merge: rho appended when present, omitted when absent")
    # sidecar spanning the refit's seconds (0.6..3.0 s), so no span warning
    COLSIDE = "\n".join(["point_index,fs_time_s,reactor_epoch,reactor_iso",
                         f"1,0.6,{BASE + 0.6},x",
                         f"2,1.8,{BASE + 1.8},x",
                         f"3,3.0,{BASE + 3.0},x"])
    res_rho = merge_text(COL, COLSIDE)
    c.check("rho present -> output has rho", "rho(uOhm*cm)" in res_rho.header,
            str(res_rho.header))
    c.check("thickness always carried", "Thick(A).1" in res_rho.header)
    c.check("no spurious unit-span warning",
            not any("differ by" in w for w in res_rho.warnings), str(res_rho.warnings))
    col_norho = "\n".join(ln.rsplit("\t", 1)[0] for ln in COL.split("\n"))
    res_norho = merge_text(col_norho, COLSIDE)
    c.check("rho absent -> output has no rho",
            not any(h.startswith("rho") for h in res_norho.header), str(res_norho.header))

    c.section("merge: guardrail warns on a ~60x span mismatch")
    bad_side = "\n".join(["point_index,fs_time_s,reactor_epoch,reactor_iso"]
                         + [f"{i},{i*30.0},{BASE + i*30.0},x" for i in range(1, 6)])
    res_bad = merge_text(COL, bad_side)     # refit ~2.4 s vs sidecar 30..150 s
    c.check("span-mismatch warning present",
            any("differ by" in w for w in res_bad.warnings), str(res_bad.warnings))

    c.section("parse a real refit export from ~/Downloads if present")
    downloads = sorted(Path.home().joinpath("Downloads").glob("DynData*.txt"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if downloads:
        rd = parse_dyn_file(downloads[0].read_text(errors="replace"))
        c.check(f"real refit: parsed {downloads[0].name}",
                rd.n_points > 10 and rd.thickness_col.startswith("Thick") and bool(rd.time),
                f"{rd.n_points} pts, thick={rd.thickness_col}")
        c.check("real refit: Time(min) -> seconds (run is minutes long)",
                rd.time_unit == "min" and rd.time[-1] > 600,
                f"unit={rd.time_unit}, last={rd.time[-1]:.0f}s")
    else:
        c.check("real refit export present (informational)", True, "none in ~/Downloads")

    # -- real on-disk FS-1 file (smoke) ------------------------------------ #
    c.section("parse a real FS-1 file if present")
    real = Path(r"C:\fs\dyndata\Default\CH4-014.txt")
    if real.exists():
        rdyn = parse_dyn_file(real.read_text(errors="replace"))
        c.check("real: >1200 points", rdyn.n_points > 1200, f"{rdyn.n_points}")
        c.check("real: 4 wavelengths", len(rdyn.wavelengths) == 4,
                f"{rdyn.wavelengths}")
        c.check("real: Thick column found", rdyn.thickness_col.startswith("Thick"),
                rdyn.thickness_col)
        c.check("real: Time increasing",
                rdyn.time[0] < rdyn.time[-1], f"{rdyn.time[0]}..{rdyn.time[-1]}")
    else:
        c.check("real FS-1 file present (informational)", True,
                "skipped - CH4-014.txt not on this machine")

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
