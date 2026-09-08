"""Fractional cycle numbering + plot-ready by-cycle export (reactor-ckk),
against the virtual reactor.

Two runs:
  1. a clean EE-ALD run - cycle_number climbs monotonically ~0 -> cycles, no
     paused samples, and the by-cycle file mirrors the in-cycle rows.
  2. an EE-ALD run with a forced plasma dropout during a beam step - those
     samples are labelled `recipe_step = "reignite"`, their cycle_number
     FREEZES (the reignite doesn't advance the cycle), and they are dropped
     from the by-cycle file.

Since 2026-08-21 there is no separate `paused` column: a reignite and an
operator pause name themselves in `recipe_step`, which is what PAUSE_STEPS
below matches on. A reignite is an event in its own right, not part of the
electron-beam step it interrupts.

Run directly: python -m tests.test_cycle_numbering
"""

from __future__ import annotations

#: recipe_step values meaning "not depositing here" - see reactor/datalog.py
#: and RecipeProgress.log_step.
PAUSE_STEPS = {"reignite", "pause"}


def is_paused(row) -> bool:
    return row.get("recipe_step", "").strip().lower() in PAUSE_STEPS

import asyncio
import contextlib
import sys

sys.path.insert(0, ".")

from reactor.testing.virtual_reactor import VirtualReactor
from tests._support import Checker, autotick

P = dict(cycles=3, dose_s=0.05, pump_a_s=0.15, beam_s=0.40, pump_b_s=0.10,
         dose_pressure_torr=0.02, min_current_a=5.0e-4)


def parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = text.strip().split("\n")
    header = lines[0].split(",")
    rows = [dict(zip(header, ln.split(","))) for ln in lines[1:]]
    return header, rows


def fnum(s: str):
    s = s.strip()
    return float(s) if s not in ("", "nan") else None


async def run_to_completion(vr, params, watcher=None):
    tick = await autotick(vr, period=0.05)
    jobs = []
    try:
        await vr.sup.start_ald_run(params)
        if watcher:
            jobs.append(asyncio.create_task(watcher()))
        run_csv = vr.sup.logger.run_path
        bycycle_csv = vr.sup.logger.bycycle_path
        while vr.sup.recipes.busy:
            await asyncio.sleep(0.02)
    finally:
        for j in jobs:
            j.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await j
        tick.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick
    return run_csv, bycycle_csv


async def main() -> int:
    c = Checker("test_cycle_numbering")

    async with VirtualReactor() as vr:
        vr.instruments["ammeter"].value = 1.0e-3      # lit

        # -- 1. clean run -------------------------------------------------- #
        c.section("1. clean run: monotonic cycle numbers, nothing paused")
        run_csv, bycycle_csv = await run_to_completion(vr, P)
        rhdr, rrows = parse_csv(run_csv.read_text(encoding="utf-8"))
        bhdr, brows = parse_csv(bycycle_csv.read_text(encoding="utf-8"))

        c.check("run CSV has cycle_number and recipe_step",
                "cycle_number" in rhdr and "recipe_step" in rhdr, str(rhdr))
        c.check("run CSV has NO separate paused column",
                "paused" not in rhdr, str(rhdr))
        c.check("by-cycle CSV is keyed by cycle_number", bhdr[0] == "cycle_number",
                str(bhdr[:2]))
        c.check("by-cycle has rows", len(brows) > 0, str(len(brows)))

        bnums = [fnum(r["cycle_number"]) for r in brows]
        c.check("by-cycle cycle_number is non-decreasing",
                all(bnums[i] <= bnums[i + 1] + 1e-6 for i in range(len(bnums) - 1)))
        c.check("cycle_number starts in the first cycle (<1)",
                0.0 <= bnums[0] < 1.0, f"{bnums[0]:.3f}")
        c.check("cycle_number reaches the last cycle (>= cycles-1)",
                bnums[-1] >= P["cycles"] - 1, f"{bnums[-1]:.3f} of {P['cycles']}")
        c.check("cycle_number never runs past the cycle count",
                bnums[-1] < P["cycles"] + 0.3, f"{bnums[-1]:.3f}")

        clean_paused = [r for r in rrows if is_paused(r)]
        c.check("no paused samples in a clean run", len(clean_paused) == 0,
                f"{len(clean_paused)} paused")
        incycle = [r for r in rrows if fnum(r["cycle_number"]) is not None]
        c.check("by-cycle row count == in-cycle run rows",
                len(brows) == len(incycle), f"{len(brows)} vs {len(incycle)}")

        # -- 2. run with a plasma dropout during a beam step -------------- #
        c.section("2. dropout run: paused samples freeze + are excluded")
        vr.instruments["ammeter"].value = 1.0e-3

        async def drop_during_beam():
            amm = vr.instruments["ammeter"]
            # wait for the first beam step, then kill the plasma for a spell
            for _ in range(2000):
                if vr.sup.recipes.progress.step_op == "electron_beam":
                    break
                if not vr.sup.recipes.busy:
                    return
                await asyncio.sleep(0.01)
            amm.value = 0.0
            await asyncio.sleep(0.45)
            amm.value = 1.0e-3

        run_csv2, bycycle_csv2 = await run_to_completion(vr, P, watcher=drop_during_beam)
        _, rrows2 = parse_csv(run_csv2.read_text(encoding="utf-8"))
        _, brows2 = parse_csv(bycycle_csv2.read_text(encoding="utf-8"))

        paused2 = [r for r in rrows2 if is_paused(r)]
        c.check("dropout produced reignite samples", len(paused2) >= 2,
                f"{len(paused2)} labelled reignite")
        c.check("they are labelled 'reignite', not the beam step they froze",
                all(r["recipe_step"].strip().lower() == "reignite" for r in paused2),
                ", ".join(sorted({r["recipe_step"] for r in paused2})))
        c.check("the beam step itself still appears on unfrozen samples",
                any("electron beam" in r["recipe_step"] for r in rrows2))

        paused_nums = [fnum(r["cycle_number"]) for r in paused2
                       if fnum(r["cycle_number"]) is not None]
        if paused_nums:
            span = max(paused_nums) - min(paused_nums)
            c.check("cycle_number is frozen across the pause", span < 0.05,
                    f"span={span:.4f}")
        else:
            c.check("paused samples carried a cycle_number", False, "none had one")

        incycle2 = [r for r in rrows2 if fnum(r["cycle_number"]) is not None]
        c.check("by-cycle excludes the paused samples",
                len(brows2) == len(incycle2) - len(paused2),
                f"bycycle={len(brows2)}, in-cycle={len(incycle2)}, paused={len(paused2)}")
        c.check("by-cycle has no paused rows carried in",
                len(brows2) < len(incycle2), f"{len(brows2)} < {len(incycle2)}")

        bnums2 = [fnum(r["cycle_number"]) for r in brows2]
        c.check("by-cycle still monotonic after excluding pauses",
                all(bnums2[i] <= bnums2[i + 1] + 1e-6 for i in range(len(bnums2) - 1)))

        # -- 2b. an operator pause is its own label, distinct from a reignite --
        c.section("2b. an operator pause is labelled 'pause', not 'reignite'")
        vr.instruments["ammeter"].value = 1.0e-3      # plasma stays lit

        async def pause_mid_run():
            for _ in range(2000):
                if vr.sup.recipes.progress.phase == "cycling":
                    break
                if not vr.sup.recipes.busy:
                    return
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            vr.sup.recipes.pause()
            await asyncio.sleep(0.40)
            vr.sup.recipes.resume()

        run_csv3, bycycle_csv3 = await run_to_completion(
            vr, P, watcher=pause_mid_run)
        _, rrows3 = parse_csv(run_csv3.read_text(encoding="utf-8"))
        _, brows3 = parse_csv(bycycle_csv3.read_text(encoding="utf-8"))

        held = [r for r in rrows3 if r["recipe_step"].strip().lower() == "pause"]
        c.check("the pause produced 'pause' rows", len(held) >= 2,
                f"{len(held)} rows")
        c.check("no reignite rows - the plasma never went out",
                not any(r["recipe_step"].strip().lower() == "reignite"
                        for r in rrows3))
        held_nums = [fnum(r["cycle_number"]) for r in held
                     if fnum(r["cycle_number"]) is not None]
        if held_nums:
            c.check("cycle_number is frozen across an operator pause",
                    max(held_nums) - min(held_nums) < 0.05,
                    f"span={max(held_nums) - min(held_nums):.4f}")
        c.check("by-cycle drops the paused rows too",
                not any(r["recipe_step"].strip().lower() in PAUSE_STEPS
                        for r in brows3),
                f"{len(brows3)} by-cycle rows, none paused")

        c.section("3. status() surfaces the by-cycle export")
        st = vr.sup.logger.status()
        c.check("run_export block reports a by-cycle path",
                "bycycle_path" in st["run_export"])

    return c.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
