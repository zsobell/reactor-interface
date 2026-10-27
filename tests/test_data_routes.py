"""Analysis endpoints retain their contract and do not block the event loop."""
import asyncio
import json
import sys
import tempfile
import threading
from unittest.mock import patch
from pathlib import Path

from fastapi import FastAPI, HTTPException
from reactor.server.data import DataFiles, create_data_router
from tests._support import Checker, asgi_call, request


async def main():
    c = Checker("test_data_routes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "data"
        folder = root / "Test"
        folder.mkdir(parents=True)
        side = folder / "Test_ellipsometer.csv"
        side.write_text("point_index,fs_time_s,reactor_epoch\n1,0,1000\n2,1,1001\n3,2,1002\n")
        files = DataFiles(root)
        app = FastAPI()
        app.include_router(create_data_router(files))
        status, data = await request(app, "/api/data/files")
        c.check("lists per-run files with type", status == 200 and
                data["files"][0]["kind"] == "ellipsometer sidecar")
        status, data = await request(app, "/api/data/file?name=../outside.csv")
        c.check("path escape refused", status == 404)
        outside = Path(td) / "outside.csv"
        outside.write_text("outside")
        try:
            (root / "link.csv").symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1314:
                raise
            # Windows without Developer Mode cannot create symlinks. Exercise
            # the same resolved-path boundary; Linux CI also uses a real link.
            real_resolve = Path.resolve
            def resolve(path, *args, **kwargs):
                return outside if path == root / "link.csv" else real_resolve(path, *args, **kwargs)
            with patch.object(Path, "resolve", resolve):
                status, _ = await request(app, "/api/data/file?name=link.csv")
            c.check("resolved symlink escape refused (Windows privilege fallback)", status == 404)
        else:
            status, _ = await request(app, "/api/data/file?name=link.csv")
            c.check("symlink escape refused", status == 404)

        text = b"Time\tThick(A).1\n0\t1\n0.0166666667\t2\n"
        url = "/api/ellipsometer/merge?sidecar=Test/Test_ellipsometer.csv"
        status, data = await request(app, url, method="POST", body=text)
        c.check("refit merges through HTTP", status == 200 and data["n_points"] == 2)
        saved = root / data["saved_as"]
        c.check("saved beside the sidecar", saved.parent == folder and saved.exists())
        c.check("saved and downloaded CSV agree", saved.read_bytes() == data["csv"].encode())
        status, _ = await request(app, url, method="POST")
        c.check("empty upload still rejected", status == 400)

        c.section("delete selected run data")
        run = folder / "Test_260101_120000_run.csv"
        cycle = folder / "Test_260101_120000_bycycle.csv"
        events = folder / "Test_260101_120000_events.log"
        other = folder / "Test_260101_130000_run.csv"
        for path in (run, cycle, events, other):
            path.write_text("test", encoding="utf-8")
        identity = folder / "Test_260101_120000_analysis.json"
        identity.write_text(json.dumps({"schema_version": 1, "name": "Test",
            "date": "2026-01-01", "number": 1}), encoding="utf-8")
        (folder / "Test_260101_130000_analysis.json").write_text(json.dumps({
            "schema_version": 1, "name": "Better run", "date": "2026-01-01",
            "number": 1}), encoding="utf-8")
        selected = "Test/Test_260101_120000_bycycle.csv"
        status, data = await request(app, "/api/data/files")
        chosen = next(row for row in data["files"] if row["name"] == selected)
        c.check("new run selector entry has readable name and date",
                status == 200 and chosen["display_name"] == "Test · 2026-01-01")
        status, data = await asgi_call(app, "POST", "/api/data/rename",
            {"name": selected, "new_name": "Better run"})
        c.check("rename updates persistent display identity",
                status == 200 and data["display_name"] == "Better run · 2026-01-01 · 02"
                and json.loads(identity.read_text(encoding="utf-8"))["name"] == "Better run",
                str({"status": status, "response": data,
                     "identity": identity.read_text(encoding="utf-8")}))
        status, data = await request(app,
            f"/api/data/delete-preview?name={selected}")
        c.check("confirmation preview names exact run companions",
                status == 200 and set(data["files"]) == {
                    "Test/Test_260101_120000_run.csv",
                    "Test/Test_260101_120000_bycycle.csv",
                    "Test/Test_260101_120000_events.log",
                    "Test/Test_260101_120000_analysis.json",
                })
        status, _ = await request(app,
            "/api/data/file?name=../outside.csv", method="DELETE")
        c.check("delete cannot escape the data directory", status == 404)
        hcpes = root / "HCPES-test"
        hcpes.mkdir()
        hcpes_csv = hcpes / "points.csv"
        hcpes_csv.write_text("point_index\n", encoding="utf-8")
        status, _ = await request(app,
            "/api/data/file?name=HCPES-test/points.csv", method="DELETE")
        c.check("run-data delete cannot erase HCPES files",
                status == 404 and hcpes_csv.exists())
        guarded = FastAPI()

        def refuse(_name):
            raise HTTPException(409, "active run")

        guarded.include_router(create_data_router(files, before_delete=refuse))
        status, _ = await request(guarded,
            f"/api/data/file?name={selected}", method="DELETE")
        c.check("active-run guard preserves data", status == 409 and cycle.exists())
        status, _ = await asgi_call(guarded, "POST", "/api/data/rename",
            {"name": selected, "new_name": "Unsafe now"})
        c.check("active-run guard also refuses rename", status == 409)
        status, _ = await asgi_call(app, "POST", "/api/data/rename",
            {"name": "Test/Test_ellipsometer.csv", "new_name": "Old data"})
        c.check("historical files without an identity are not renamed", status == 404)
        status, data = await request(app,
            f"/api/data/file?name={selected}", method="DELETE")
        c.check("only the chosen run stem is deleted",
                status == 200 and len(data["deleted"]) == 4
                and not run.exists() and not cycle.exists() and not events.exists()
                and not identity.exists()
                and other.exists() and side.exists())

        entered, release = threading.Event(), threading.Event()
        original = files.merge
        def slow(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("event loop could not release analysis")
            return original(*args)
        files.merge = slow
        pending = asyncio.create_task(request(app, url, method="POST", body=text))
        try:
            c.check("merge is running in worker", await asyncio.to_thread(entered.wait, 1))
            await asyncio.sleep(0.01)
            c.check("event loop advances while merge waits", not pending.done())
            status, _ = await asyncio.wait_for(request(app, "/api/data/files"), 0.5)
            c.check("other requests remain responsive", status == 200)
        finally:
            release.set()
        status, _ = await pending
        c.check("slow merge completes normally", status == 200)
    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
