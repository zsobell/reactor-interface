"""Shared helpers for tests/test_*.py. Not a test framework - this project
deliberately has none (see tests/README.md) - just enough structure to keep
every test script's output uniform. Run each test file directly with
python; nothing here requires pytest or any other dependency.
"""

from __future__ import annotations

import asyncio
import json
import time


class Checker:
    """Tracks pass/fail lines for one test file and prints as it goes."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.fails: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")

    def check(self, label: str, cond: bool, detail: str = "") -> None:
        mark = "PASS" if cond else "FAIL"
        print(f"  {mark}  {label}{'  ' + detail if detail else ''}")
        if not cond:
            self.fails.append(label)

    def summary(self) -> int:
        """Print a final verdict and return a process exit code."""
        if self.fails:
            print(f"\n{self.name}: FAILURES ({len(self.fails)}): {self.fails}")
            return 1
        print(f"\n{self.name}: ALL PASS")
        return 0


async def wait_for(pred, timeout: float = 5.0, poll: float = 0.01) -> bool:
    """Wait until `pred()` is true; return False on timeout instead of hanging.

    Use this instead of `await asyncio.sleep(<guessed offset>)` whenever a test
    needs to act at a particular point in something the code is doing. A guessed
    offset can drift past the moment it was aiming at - or, worse, land entirely
    between two of the code's own polls, so the event it was staging is never
    observed at all. That is a flaky test, not a bug in the reactor, and it has
    bitten this suite twice (see tests/README.md).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(poll)
    return False


async def autotick(vr, period: float = 0.1) -> asyncio.Task:
    """Start a background task calling `vr.tick()` on a fixed period, standing
    in for the real _control_loop/_current_loop that a live Supervisor would
    be running. Without this, self.sup.snapshot never picks up changes made
    to vr.daq.raw / vr.instruments[x].value / vr.mfcs[x].flow_sccm - exactly
    as on the real reactor, a device's value doesn't reach the recipe engine
    until something polls it and copies it into the snapshot.

    Returns the task; cancel it (and await the CancelledError) when done:

        task = await autotick(vr)
        try:
            ...
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    """
    async def _loop():
        while True:
            await vr.tick()
            await asyncio.sleep(period)

    return asyncio.create_task(_loop(), name="autotick")


async def asgi_call(app, method: str, path: str, body=None) -> tuple[int, object]:
    """Send one JSON request straight into a FastAPI app over raw ASGI.

    Deliberately does NOT run the app's lifespan: startup calls
    Supervisor.start(), which would take the DAQ and the serial ports away from
    the server actually running the reactor. Building scopes by hand exercises
    the routes and nothing else.

    Returns (status, decoded JSON body).
    """
    payload = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "scheme": "http",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 1234),
        "headers": [(b"host", b"127.0.0.1:8000"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode())],
    }
    got = {"status": None, "body": b""}
    done = asyncio.Event()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            got["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            got["body"] += msg.get("body", b"")
            if not msg.get("more_body"):
                done.set()

    await app(scope, receive, send)
    await done.wait()
    return got["status"], json.loads(got["body"] or b"null")
