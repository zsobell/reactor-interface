"""Every file a run writes carries the run's name, and the merged file opens
cleanly in Excel.

Both failures this pins down came out of the Mo-015 run (2026-08-21):

  * the run was named "Mo-015" but only the reactor exports said so. The
    ellipsometer sidecar was `260821_131256_ellipsometer.csv` because the FS-1
    stream had already started an acquisition before Start run was pressed, and
    the merged file was `DynData - 2026-08-21T140934.713_reactor_synced.csv`
    because it was named after the dropped refit file.
  * the merged CSV had a blank row between every row of data: the csv module
    terminates rows with CR LF, and writing that back through text mode
    translated the LF again, giving CR CR LF.

Run directly: python -m tests.test_file_naming
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from reactor.analysis import ellipsometer_merge as ell
from reactor.server.app import merged_name
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick


class FakePoint:
    """Duck-typed EllipsometerPoint, enough for the sidecar writer."""

    def __init__(self, i: int, t: float) -> None:
        self.index = i
        self.time_s = float(i)
        self.thickness = 1.0 + i
        self.thickness_unit = "A"
        self.fit_diff = 1e-5
        self.intensity = 1.6
        self.temp = 39.0
        self.align_x = 0.04
        self.align_y = -0.21
        self.t_recv = t


async def main() -> int:
    c = Checker("test_file_naming")

    c.section("1. a run adopts the sidecar that was already open")
    async with VirtualReactor() as vr:
        log = vr.sup.logger
        data_dir = log.dir
        # The real order of events: the FS-1 is already streaming when the
        # operator presses Start run, so the capture opens FIRST - unnamed, and
        # loose in data/ because no run folder exists yet.
        t0 = time.time()
        opened = log.start_ellipsometer_capture(t0)
        log.write_ellipsometer_point(FakePoint(1, t0))
        c.check("capture opens with the bare timestamp name",
                not opened.name.startswith("Mo-"), opened.name)
        c.check("and loose in the data dir", opened.parent == data_dir)

        log.set_run_name("Mo-015")
        log.start_run_export("ALD + e-beam", t0 + 30)
        moved = log.ell_path
        c.check("sidecar picks up the run name", moved.name.startswith("Mo-015_"),
                moved.name)
        c.check("the timestamp is kept, not replaced",
                moved.name.endswith(opened.name), moved.name)
        c.check("sidecar moved into the run folder",
                moved.parent == data_dir / "Mo-015",
                str(moved.relative_to(data_dir)))
        c.check("the old path is gone (moved, not copied)", not opened.exists())

        # The capture must survive the move, or the acquisition is lost.
        log.write_ellipsometer_point(FakePoint(2, t0 + 31))
        log.stop_ellipsometer_capture()
        rows = moved.read_text(encoding="utf-8").strip().splitlines()
        c.check("both points landed in the moved file", len(rows) == 3,
                f"{len(rows)-1} data rows")

        c.check("run trace and by-cycle are in the same folder",
                log.run_path.parent == moved.parent
                and log.bycycle_path.parent == moved.parent,
                log.run_path.parent.name)

        # Adopting twice must not stack prefixes.
        log.stop_run_export()
        log.start_ellipsometer_capture(t0 + 100)
        log.set_run_name("Mo-016")
        log.start_run_export("ALD + e-beam", t0 + 130)
        log.stop_run_export()
        log.set_run_name("Mo-017")
        log.start_run_export("ALD + e-beam", t0 + 160)
        name = log.ell_path.name
        c.check("re-adopting replaces the prefix, never stacks it",
                name.startswith("Mo-017_") and "Mo-016" not in name, name)
        c.check("and it followed the run into the new folder",
                log.ell_path.parent == data_dir / "Mo-017",
                str(log.ell_path.relative_to(data_dir)))
        log.stop_ellipsometer_capture()
        log.stop_run_export()

    c.section("1b. an unnamed run still gets a folder, named by timestamp")
    async with VirtualReactor() as vr:
        log = vr.sup.logger
        log.set_run_name("")
        run_path = log.start_run_export("ALD + e-beam (precursor 1)", time.time())
        c.check("folder is the bare timestamp, without the recipe slug",
                re.fullmatch(r"\d{6}_\d{6}", run_path.parent.name) is not None,
                run_path.parent.name)
        c.check("the filename still carries the recipe slug",
                "ALD" in run_path.name, run_path.name)
        log.stop_run_export()
        c.check("run_dir clears when the export stops", log.run_dir is None)

    c.section("2. the merged file is named after the run")
    c.check("from the reactor run export",
            merged_name("Mo-015_260821_131320_run.csv",
                        "Mo-015_260821_131256_ellipsometer.csv",
                        "DynData - 2026-08-21T140934.713.txt")
            == "Mo-015_260821_131320_reactor_synced.csv")
    c.check("from the sidecar when no run is selected",
            merged_name("", "Mo-015_260821_131256_ellipsometer.csv", "DynData - x.txt")
            == "Mo-015_260821_131256_reactor_synced.csv")
    c.check("falls back to the refit file when neither is given",
            merged_name("", "", "DynData - x.txt") == "DynData - x_reactor_synced.csv")
    c.check("an unnamed run still keeps its timestamp",
            merged_name("260807_133232_ALD_run.csv", "", "x.txt")
            == "260807_133232_ALD_reactor_synced.csv")

    c.section("3. the merged CSV has one line per row (Excel-clean)")
    # Build a tiny merge and write it the way the endpoint does.
    dyn = "Time\tThick(A).1\trho(uOhm*cm)\n0.0\t1.0\t5.0\n0.05\t2.0\t6.0\n"
    base = 1_787_339_000.0
    side = io.StringIO()
    w = csv.writer(side, lineterminator="\n")
    w.writerow(["point_index", "fs_time_s", "reactor_epoch", "reactor_iso",
                "thickness_live", "thickness_unit", "fit_diff", "intensity",
                "temp", "align_x", "align_y"])
    for i in range(6):
        w.writerow([i + 1, i * 1.0, f"{base + i:.3f}", "", 1.0, "A", 0, 0, 0, 0, 0])
    result = ell.merge_text(dyn, side.getvalue())
    csv_text = result.to_csv()

    out = Path("data") / "_test_naming_reactor_synced.csv"
    try:
        with out.open("w", encoding="utf-8", newline="") as fh:
            fh.write(csv_text)
        raw = out.read_bytes()
        c.check("no CR CR LF anywhere", b"\r\r\n" not in raw)
        c.check("no blank lines between rows", b"\n\r\n" not in raw and b"\n\n" not in raw)
        parsed = list(csv.reader(out.open(newline="", encoding="utf-8")))
        c.check("every parsed row has content", all(r for r in parsed),
                f"{len(parsed)} rows, none empty")
        c.check("row count matches the merge", len(parsed) - 1 == result.n_points,
                f"{len(parsed)-1} data rows vs n_points={result.n_points}")
    finally:
        out.unlink(missing_ok=True)

    c.section("4. one run = one folder, one stem, no .json")
    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3
        log = vr.sup.logger
        data_dir = log.dir
        t0 = time.time()
        log.start_ellipsometer_capture(t0)          # FS-1 already streaming
        log.write_ellipsometer_point(FakePoint(1, t0))

        tick = await autotick(vr, period=0.05)
        try:
            await vr.sup.start_ald_run(dict(
                cycles=1, dose_s=0.05, pump_a_s=0.05, beam_s=0.2, pump_b_s=0.05,
                dose_pressure_torr=0.02, min_current_a=5.0e-4,
                ar_close_delay_s=0.05, run_name="Mo-042"))
            while vr.sup.recipes.busy:
                await asyncio.sleep(0.02)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick
        log.stop_ellipsometer_capture()

        folder = data_dir / "Mo-042"
        c.check("the run made a folder named for the run", folder.is_dir(),
                "data/Mo-042/")
        names = sorted(p.name for p in folder.iterdir())
        c.check("every file the run wrote is in it", len(names) >= 4,
                ", ".join(names))
        c.check("nothing the run wrote was left loose in data/",
                not any(p.is_file() and p.name.startswith("Mo-042")
                        for p in data_dir.iterdir()))
        c.check("no .json among them", not any(n.endswith(".json") for n in names),
                ", ".join(names))
        c.check("the parameters file is a .txt",
                any(n.endswith("_run_params.txt") for n in names))
        c.check("every filename starts with the run name",
                all(n.startswith("Mo-042_") for n in names), ", ".join(names))

        # The run's own files share one stem; the sidecar keeps its own
        # timestamp because the acquisition genuinely started earlier.
        stems = {n.rsplit("_", 1)[0] for n in names
                 if n.endswith(("_run.csv", "_bycycle.csv"))}
        stems |= {n[: -len("_run_params.txt")] for n in names
                  if n.endswith("_run_params.txt")}
        c.check("trace, by-cycle and parameters share one stem", len(stems) == 1,
                ", ".join(sorted(stems)))

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
