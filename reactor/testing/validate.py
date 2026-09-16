"""Cross-platform validation entry point for development and CI.

The project intentionally keeps its plain-script Python test runner.  This
module adds stable focused groups and includes the JavaScript harnesses in the
full/frontend checks without requiring a shell-specific wrapper.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

PYTHON_GROUPS: dict[str, tuple[str, ...]] = {
    "control": (
        "test_clock_domains",
        "test_controller_contracts",
        "test_cycle_numbering",
        "test_dependencies",
        "test_ee_ald_recipe",
        "test_ee_cvd_recipe",
        "test_fill_controller",
        "test_glassman_fl",
        "test_gas_simultaneous",
        "test_hv_and_prestart_abort",
        "test_keithley_supplies",
        "test_mfc_interlock",
        "test_merge_integration",
        "test_live_params",
        "test_param_migration",
        "test_parameters",
        "test_pause",
        "test_prestart",
        "test_prestart_invalid",
        "test_run_admission",
        "test_run_lifecycle",
        "test_run_estimate",
        "test_run_timing",
        "test_sample_bias_bracket",
        "test_soft_open",
        "test_sweep_controller",
        "test_timing_prototype",
    ),
    "recording": (
        "test_file_naming",
        "test_merge_acceptance",
        "test_recording_api",
        "test_recording_errors",
        "test_recording_worker",
        "test_run_export",
        "test_sample_freshness",
    ),
    "api": (
        "test_analysis_layout_api",
        "test_data_routes",
        "test_ellipsometer_decode",
        "test_ellipsometer_merge",
        "test_telemetry",
        "test_server_shutdown",
        "test_shutdown_teardown",
    ),
    "frontend": ("test_static_assets",),
}

@dataclass(frozen=True)
class Check:
    """One validation subprocess, represented without shell quoting."""

    label: str
    argv: tuple[str, ...]


CommandRunner = Callable[[Sequence[str], Path], int]


def _print(message: str) -> None:
    print(message, flush=True)


def python_checks(group: str, python: str = sys.executable) -> list[Check]:
    """Return Python checks for a named group.

    Full validation deliberately delegates to ``tests.run_all`` so there is
    still one canonical test-discovery implementation.
    """

    if group == "full":
        return [Check("Python test suite", (python, "-m", "tests.run_all"))]
    return [
        Check(module, (python, "-m", f"tests.{module}"))
        for module in PYTHON_GROUPS[group]
    ]


def node_test_paths(root: Path = ROOT) -> list[Path]:
    """Discover JavaScript harnesses just as ``tests.run_all`` discovers Python."""

    return sorted(path.relative_to(root) for path in (root / "tests/js").glob("*.mjs"))


def node_checks(node: str) -> list[Check]:
    return [Check(str(path), (node, str(path))) for path in node_test_paths()]


def _subprocess_runner(argv: Sequence[str], cwd: Path) -> int:
    return subprocess.run(argv, cwd=cwd, check=False).returncode


def validate(
    group: str = "full",
    *,
    require_node: bool = False,
    python: str = sys.executable,
    find_node: Callable[[str], str | None] = shutil.which,
    run: CommandRunner = _subprocess_runner,
    output: Callable[[str], None] = _print,
) -> int:
    """Run one validation group and return a process-compatible status."""

    checks = python_checks(group, python)
    needs_node = group in ("full", "frontend")
    if needs_node:
        node = find_node("node")
        if node:
            checks.extend(node_checks(node))
        else:
            status = "ERROR" if require_node else "SKIP"
            output(
                f"{status}: Node.js was not found; JavaScript tests did not run. "
                "Install Node.js or use --require-node in CI."
            )
            if require_node:
                return 1

    failed: list[str] = []
    output(f"Validation group: {group}")
    for check in checks:
        output(f"RUN: {check.label}")
        try:
            returncode = run(check.argv, ROOT)
        except OSError as exc:
            output(f"ERROR: could not run {check.label}: {type(exc).__name__}: {exc}")
            returncode = 1
        if returncode != 0:
            failed.append(check.label)

    if failed:
        output(f"FAILED: {', '.join(failed)}")
        return 1
    output(f"PASS: {group} validation")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "group",
        nargs="?",
        default="full",
        choices=("full", *PYTHON_GROUPS),
        help="validation group (default: full)",
    )
    parser.add_argument(
        "--require-node",
        action="store_true",
        help="fail when a full/frontend check cannot find Node.js",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return validate(args.group, require_node=args.require_node)


if __name__ == "__main__":
    raise SystemExit(main())
