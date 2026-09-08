"""Analysis endpoints retain their contract and do not block the event loop."""
import asyncio
import sys
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI
from reactor.server.data import DataFiles, create_data_router
from tests._support import Checker, request


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
        (root / "link.csv").symlink_to(outside)
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
