"""Run every tests/test_*.py in one process and print an aggregate summary.

    python -m tests.run_all

Equivalent to running each test file directly (`python -m tests.test_x`) and
checking its exit code, just in one command. No pytest, no dependency beyond
the project's own venv - see tests/README.md for why.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import sys

import tests


def discover() -> list[str]:
    names = []
    for mod in pkgutil.iter_modules(tests.__path__):
        if mod.name.startswith("test_"):
            names.append(mod.name)
    return sorted(names)


async def main() -> int:
    names = discover()
    if not names:
        print("no tests/test_*.py files found")
        return 1

    results: dict[str, int] = {}
    for name in names:
        print(f"\n{'#'*70}\n# {name}\n{'#'*70}")
        mod = importlib.import_module(f"tests.{name}")
        try:
            results[name] = await mod.main()
        except Exception as exc:
            print(f"  CRASHED: {type(exc).__name__}: {exc}")
            results[name] = 1

    print(f"\n{'='*70}")
    for name, code in results.items():
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {name}")
    failed = [n for n, c in results.items() if c != 0]
    print(f"\n{len(results)-len(failed)}/{len(results)} test files passed"
         + (f" - FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
