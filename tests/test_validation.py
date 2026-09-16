"""The validation wrapper selects checks correctly and propagates failures."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Sequence

from reactor.testing.validate import (
    PYTHON_GROUPS,
    ROOT,
    node_test_paths,
    python_checks,
    validate,
)
from tests._support import Checker


class FakeRunner:
    def __init__(self, failures: set[str] | None = None):
        self.failures = failures or set()
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, argv: Sequence[str], cwd: Path) -> int:
        command = tuple(argv)
        self.calls.append((command, cwd))
        return int(any(part in self.failures for part in command))


async def main() -> int:
    c = Checker("test_validation")

    c.section("1. full checks preserve the existing Python runner")
    full_python = python_checks("full", "python-for-test")
    c.check(
        "full Python command delegates to tests.run_all",
        len(full_python) == 1
        and full_python[0].argv == ("python-for-test", "-m", "tests.run_all"),
        str(full_python),
    )

    c.section("2. focused groups execute their declared Python modules")
    for group, modules in PYTHON_GROUPS.items():
        runner = FakeRunner()
        result = validate(
            group,
            python="python-for-test",
            run=runner,
            output=lambda _: None,
            find_node=lambda _: None,
        )
        expected = [
            ("python-for-test", "-m", f"tests.{module}") for module in modules
        ]
        actual = [argv for argv, _ in runner.calls]
        c.check(f"{group} group succeeds", result == 0, str(result))
        c.check(
            f"{group} group selects expected modules", actual == expected, str(actual)
        )
        c.check(
            f"{group} commands run from repository root",
            all(cwd == ROOT for _, cwd in runner.calls),
        )

    c.section("3. Node handling is explicit and can be strict")
    output: list[str] = []
    optional = FakeRunner()
    optional_result = validate(
        "full", python="python-for-test", find_node=lambda _: None,
        run=optional, output=output.append,
    )
    c.check("missing optional Node still runs Python", len(optional.calls) == 1)
    c.check("missing optional Node is reported", optional_result == 0 and any(
        line.startswith("SKIP:") and "did not run" in line for line in output
    ), str(output))

    strict = FakeRunner()
    strict_output: list[str] = []
    strict_result = validate(
        "full", require_node=True, python="python-for-test", find_node=lambda _: None,
        run=strict, output=strict_output.append,
    )
    c.check("strict CI mode fails without Node", strict_result == 1, str(strict_result))
    c.check("strict mode does not claim tests ran", not strict.calls, str(strict.calls))
    c.check("strict failure identifies Node", any(
        line.startswith("ERROR:") and "Node.js" in line for line in strict_output
    ), str(strict_output))

    c.section("4. Node checks and subprocess failures affect the result")
    node = "/test/bin/node"
    node_paths = node_test_paths()
    node_runner = FakeRunner()
    node_result = validate(
        "frontend", require_node=True, python=sys.executable,
        find_node=lambda _: node, run=node_runner, output=lambda _: None,
    )
    node_commands = [argv for argv, _ in node_runner.calls if argv[0] == node]
    c.check("JavaScript harnesses are discovered in sorted order",
            len(node_paths) >= 2 and node_paths == sorted(node_paths), str(node_paths))
    c.check("every discovered JavaScript harness runs",
            node_result == 0 and node_commands == [
                (node, str(path)) for path in node_paths
            ], str(node_commands))

    failure_output: list[str] = []
    failing = FakeRunner({str(node_paths[0])})
    failure_result = validate(
        "full", require_node=True, python="python-for-test",
        find_node=lambda _: node, run=failing, output=failure_output.append,
    )
    c.check("one failed subprocess makes validation fail", failure_result == 1)
    c.check("remaining checks still run for a useful report",
            len(failing.calls) == 1 + len(node_paths), str(failing.calls))
    c.check("failed check is named", any(
        str(node_paths[0]) in line
        for line in failure_output
        if line.startswith("FAILED:")
    ), str(failure_output))

    launch_calls = 0

    def unavailable(argv: Sequence[str], cwd: Path) -> int:
        nonlocal launch_calls
        launch_calls += 1
        if launch_calls == 1:
            raise OSError("executable unavailable")
        return 0

    launch_output: list[str] = []
    launch_result = validate(
        "full", require_node=True, python="missing-python",
        find_node=lambda _: node, run=unavailable, output=launch_output.append,
    )
    c.check("launch errors become a failed validation", launch_result == 1)
    c.check("launch error is reported without aborting later checks",
            launch_calls == 1 + len(node_paths) and any(
                "OSError: executable unavailable" in line for line in launch_output
            ), str(launch_output))

    c.section("5. CI installs both runtimes and enables strict validation")
    workflow = Path(".github/workflows/validate.yml").read_text(encoding="utf-8")
    c.check("CI pins the Python setup action", "actions/setup-python@v5" in workflow)
    c.check("CI selects the supported Python version", 'python-version: "3.12"' in workflow)
    c.check("CI pins the Node setup action", "actions/setup-node@v4" in workflow)
    c.check("CI targets the supported Windows platform",
            "runs-on: windows-latest" in workflow and "ubuntu-latest" not in workflow)
    c.check("CI installs the declared dependencies",
            "python -m pip install -r requirements.txt" in workflow)
    c.check("CI requires both Python and Node validation",
            "python -m reactor.testing.validate full --require-node" in workflow)

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
