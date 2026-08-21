"""The docs still describe THIS program, checked against the code and config.

Documentation rots silently: nothing fails when a README keeps quoting a poll
rate, an IP, a register address or a filename that the code has since moved on
from, and by the time anyone notices, the doc has been misleading for months.
Every claim below is one that was repeated in prose and could drift out from
under it. Three had already drifted when this file was written (2026-08-21):

  * README and CLAUDE.md still said the MFCs were read over HTTP, months after
    the reads moved to Modbus;
  * the Glassman was still described as "never commanded" after hv_off was
    wired up;
  * tests/README.md had lost a row for test_sample_freshness.py.

This is a documentation test, not a hardware test. It reads files. If it fails,
the fix is usually to update the prose, not the code - but check which one is
actually wrong first.

Run directly: python -m tests.test_docs
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from reactor.config import load_config
from reactor.control.recipe import Step
from reactor.devices.mks_mfc import MfcRegisters
from tests._support import Checker

DOC_PATHS = [Path("README.md"), Path("CLAUDE.md")] + sorted(Path("docs").glob("*.md"))


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


async def main() -> int:
    c = Checker("test_docs")
    cfg = load_config("config/reactor.yaml")
    docs = {p: read(p) for p in DOC_PATHS}
    ALL = "\n".join(docs.values())
    readme, claude = docs[Path("README.md")], docs[Path("CLAUDE.md")]

    c.section("1. poll rates quoted in prose match the config defaults")
    c.check("loop_hz 2.0", cfg.site.loop_hz == 2.0, str(cfg.site.loop_hz))
    c.check("current_hz 5.0", cfg.site.current_hz == 5.0, str(cfg.site.current_hz))
    c.check("mfc_hz 6.0", cfg.site.mfc_hz == 6.0, str(cfg.site.mfc_hz))
    c.check("the docs quote the MFC rate", re.search(r"mfc_hz`? 6", ALL) is not None)

    c.section("2. hardware addresses in prose match config/reactor.yaml")
    for m in cfg.mfcs:
        c.check(f"MFC {m.id} host {m.host} appears in the docs", m.host in ALL)
    ps = cfg.power_supplies[0]
    c.check("HV port/baud/address are quoted",
            ps.port in ALL and str(ps.baud) in ALL and f"address {ps.address}" in ALL,
            f"{ps.port} {ps.baud} addr {ps.address}")
    c.check("ellipsometer host:port is quoted",
            cfg.ellipsometer.host in ALL and str(cfg.ellipsometer.port) in ALL,
            f"{cfg.ellipsometer.host}:{cfg.ellipsometer.port}")

    c.section("3. the channel map in prose matches the config")
    c.check("pressure channel", cfg.pressure.channel in ALL, cfg.pressure.channel)
    c.check("stage TC channel", cfg.stage_temp.channel in ALL, cfg.stage_temp.channel)
    aux = {a.id: a.channel for a in cfg.aux_inputs}
    c.check("bubbler TC channel", aux.get("bubbler", "") in ALL, str(aux.get("bubbler")))
    c.check(f"valve count ({len(cfg.valves)}) is stated",
            f"{len(cfg.valves)} valves" in ALL, f"{len(cfg.valves)} valves")
    pg = next((v for v in cfg.valves if v.id == "plasma_ground"), None)
    c.check("plasma_ground line is quoted",
            pg is not None and pg.line.split("/")[0] in ALL and pg.line.rsplit("/", 1)[-1] in ALL,
            pg.line if pg else "missing")

    c.section("4. MFC register addresses in prose match the driver")
    r = MfcRegisters()
    c.check("flow read register", r.flow_read == 0x4000 and "0x4000" in ALL, hex(r.flow_read))
    c.check("setpoint register",
            r.setpoint_read == r.setpoint_write == 0xA000 and "0xA000" in ALL,
            hex(r.setpoint_read))

    c.section("5. no doc still describes behaviour that has changed")
    c.check("MFCs are not described as HTTP-read",
            "HTTP read / Modbus write" not in ALL
            and not re.search(r"MFCs?[^.\n]{0,30}HTTP read", ALL))
    c.check("the HV supply is not described as never commanded",
            "never commands the supply" not in ALL
            and "It never commands it." not in ALL)
    # `_ald_run_params.json` is the superseded name and may appear in prose
    # explaining the change; the CURRENT name must never carry .json.
    c.check("the params file is not advertised as .json",
            "_run_params.json" not in ALL.replace("_ald_run_params.json", ""))
    c.check("the ellipsometer sync is not placed on the Diagnostics tab",
            not re.search(r"Diagnostics[^.\n]{0,60}[Ee]llipsometer sync", ALL))

    c.section("6. every module and test is mentioned where it should be")
    modules = sorted(p for p in Path("reactor").rglob("*.py")
                     if p.name not in ("__init__.py",))
    modules += sorted(Path("tools").glob("*.py"))
    for p in modules:
        if p.name == "__init__.py":
            continue
        c.check(f"README lists {p.name}", p.name in readme, str(p))
    for p in sorted(Path("reactor/server/static").glob("*.html")):
        c.check(f"README lists {p.name}", p.name in readme)

    tests_readme = read(Path("tests/README.md"))
    for p in sorted(Path("tests").glob("test_*.py")):
        c.check(f"tests/README lists {p.name}", p.name in tests_readme)

    c.section("7. the recipe step ops the docs name all exist")
    ops = set(Step.model_fields["op"].annotation.__args__)
    for op in sorted(ops):
        c.check(f"'{op}' is documented", op in ALL, op)

    c.section("8. the files a run writes are all documented")
    for suf in ("_run.csv", "_bycycle.csv", "_run_params.txt",
                "_ellipsometer.csv", "_reactor_synced.csv"):
        c.check(f"{suf} is documented", suf in ALL)

    return c.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
