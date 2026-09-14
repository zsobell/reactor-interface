"""Named recording operations preserve ordering, copies, and metadata ownership."""

from __future__ import annotations

import asyncio
import csv
import time
from pathlib import Path
from types import SimpleNamespace

from reactor.control.recipe import Recipe, Step
from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker


async def main() -> int:
    c = Checker("test_recording_api")

    async with VirtualReactor() as vr:
        recording = vr.sup.recording

        c.section("1. manual lifecycle is ordered behind copied samples")
        manual_path = await recording.start_manual_log("typed-api.tsv")
        sample = {"pressure": 2.5}
        accepted = recording.submit_manual_sample(sample, sampled_at=time.time())
        sample["pressure"] = 99.0
        await recording.stop_manual_log()
        manual_rows = list(csv.DictReader(
            manual_path.open(encoding="utf-8"), delimiter="\t"
        ))
        c.check("manual sample accepted", accepted)
        c.check("stop waits for the queued sample", len(manual_rows) == 1)
        c.check("manual sample was copied before queuing",
                manual_rows[0]["Pressure"] == "2.5", str(manual_rows[0]))
        c.check("service status owns the manual path",
                recording.status()["path"] == str(manual_path))

        c.section("2. run lifecycle exposes its name and paths through the service")
        recipe = Recipe(name="Typed recording", steps=[Step(op="wait", seconds=0.1)])
        started_at = time.time()
        run_name = await recording.set_run_name("Typed-007")
        run_path = await recording.start_run_export(recipe.name, started_at)
        params_path = await recording.write_run_parameters(
            {"run_name": run_name}, recipe
        )
        progress = SimpleNamespace(
            cycle=1, cycle_fraction=0.25, paused=False, step_desc="original"
        )
        run_sample = {"t": started_at + 0.1, "pressure": 4.0}
        run_accepted = recording.submit_run_sample(run_sample, progress)
        run_sample["pressure"] = 400.0
        progress.step_desc = "mutated"
        await recording.stop_run_export()
        run_rows = list(csv.DictReader(run_path.open(encoding="utf-8")))
        run_status = recording.status()["run_export"]
        c.check("sanitized run name is service-owned", run_name == recording.run_name)
        c.check("run sample accepted", run_accepted)
        c.check("run stop waits for queued sample", len(run_rows) == 1)
        c.check("run sample and progress were copied",
                run_rows[0]["pressure"] == "4" and
                run_rows[0]["recipe_step"] == "original", str(run_rows[0]))
        c.check("run status owns the stable path after close",
                not run_status["active"] and run_status["path"] == str(run_path))
        c.check("parameter report operation returns its path", params_path.is_file())

        c.section("3. capture is copied and serialized on the same worker")
        point = SimpleNamespace(
            index=1,
            time_s=0.2,
            thickness=3.5,
            thickness_unit="nm",
            fit_diff=0.1,
            intensity=12.0,
            temp=25.0,
            align_x=0.0,
            align_y=0.0,
            t_recv=time.time(),
        )
        capture_accepted = recording.capture_ellipsometer_point(point, 5.0)
        point.thickness = 350.0
        await recording.drain()
        ell_path = Path(recording.status()["ellipsometer"]["path"])
        ell_rows = list(csv.DictReader(ell_path.open(encoding="utf-8")))
        c.check("capture accepted", capture_accepted)
        c.check("capture point was copied", ell_rows[0]["thickness_live"] == "3.5")

    c.section("4. production callers use the named boundary")
    production = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in ("reactor/supervisor.py", "reactor/server/app.py",
                     "reactor/control/run_coordinator.py")
    )
    c.check("production has no method-name dispatch", ".recording.call(" not in production)
    c.check("production has no generic sample dispatch", ".recording.submit(" not in production)
    c.check("Supervisor does not read recording metadata from DataLogger",
            "self.logger.run_name" not in production)

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
