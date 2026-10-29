"""Clearing the shared error view preserves archives and survives restart."""
import asyncio
import tempfile
from pathlib import Path

from reactor.config import load_config
from reactor.dependencies import StatePaths
from reactor.server.app import create_app
from reactor.supervisor import Supervisor
from tests._support import Checker, request


async def main():
    c = Checker("test_error_log_clear")
    with tempfile.TemporaryDirectory(prefix="error_log_clear_") as directory:
        paths = StatePaths.in_directory(Path(directory))
        sup = Supervisor(load_config(), paths=paths)
        app = create_app(supervisor=sup)
        sup._event("error", "old failure", record=False,
                   timestamp=sup.clock.wall() - 10)
        sup.report_lifecycle_event("error", "old lifecycle failure", record=False)
        before = sup.state()
        c.check("old error appears in API and telemetry",
                len((await request(app, "/api/errors"))[1]["errors"]) == 2
                and len(before["errors"]) == 2)
        status, cleared = await request(app, "/api/errors/clear", method="POST")
        c.check("clear endpoint empties shared error view",
                status == 200 and cleared["ok"] and not sup.errors
                and not (await request(app, "/api/errors"))[1]["errors"])
        c.check("telemetry carries clear boundary",
                sup.state()["error_log_cleared_at"] == cleared["cleared_at"])
        c.check("event history and durable lifecycle marker remain",
                any(e["message"] == "old failure" for e in sup.events)
                and paths.lifecycle_events.exists()
                and "errors_cleared" in paths.lifecycle_events.read_text())
        sup.report_lifecycle_event("error", "new lifecycle failure", record=False)
        c.check("new errors appear after clearing",
                [e["message"] for e in sup.errors] == ["new lifecycle failure"])
        await sup.recording.close()

        replacement = Supervisor(load_config(), paths=paths)
        c.check("cleared lifecycle errors stay hidden after restart",
                replacement.error_log_cleared_at == cleared["cleared_at"]
                and [e["message"] for e in replacement.errors]
                == ["new lifecycle failure"])
        await replacement.recording.close()
    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
