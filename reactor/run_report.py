"""Pure formatting of the human-readable experiment parameter report."""
from __future__ import annotations
from datetime import datetime

def _describe_recipe(recipe) -> str:
    """Plain-English summary of a built EE-ALD / EE-CVD Recipe - cycle
    architecture and the gas-schedule timeline, for a human skimming the run
    snapshot file.

    The gas window is anchored differently per mode, and this mirrors exactly
    what the runner measures against (see control/recipe.py's GasSchedule):
    EE-ALD divides up the beam step's exposure, EE-CVD the whole cycle. Both
    handoffs move the incoming gas earlier by the single Recipe.gas_overlap_s.
    """
    lines = [f"{recipe.name} — {recipe.cycles} cycles"]
    for s in recipe.steps:
        lines.append(f"  {s.describe()}")
    if not recipe.gas_schedules:
        return "\n".join(lines)

    cvd = recipe.mode == "cvd"
    if cvd:
        span, anchor = recipe.cycle_seconds(), "cycle"
    else:
        beam = next((s for s in recipe.steps if s.op == "electron_beam"), None)
        span, anchor = ((beam.seconds or 0.0) if beam else 0.0), "beam"
    if span <= 0:
        return "\n".join(lines)

    ov = recipe.gas_overlap_s
    first = next((g for g in recipe.gas_schedules if g.order == "first"), None)
    second = next((g for g in recipe.gas_schedules if g.order == "second"), None)
    handoff = (first.pct / 100.0 * span) if first else 0.0
    second_off = min(span, handoff + (second.pct / 100.0 * span if second else 0.0))

    lines.append(f"  gas schedule (relative to {anchor} start, "
                 f"{ov:g}s handoff overlap):")
    if first:
        # EE-CVD has no run-up before a cycle, so "first" re-arms before this
        # cycle ends rather than leading a beam step (RecipeRunner._build_gas_plan).
        on = (f"on at {anchor}+0s, re-arms at {anchor}+{max(0.0, second_off - ov):g}s"
              if cvd else f"on at {anchor}-{ov:g}s")
        lines.append(f"    {first.mfc}: {on}, off at {anchor}+{handoff:g}s "
                     f"({first.pct:g}% @ {first.flow_sccm:g} sccm)")
    if second:
        lines.append(
            f"    {second.mfc}: on at {anchor}+{max(0.0, handoff - ov):g}s, "
            f"off at {anchor}+{second_off:g}s "
            f"({second.pct:g}% @ {second.flow_sccm:g} sccm)")
    return "\n".join(lines)


#: Report line separator, kept as a name so the literal never has to survive
#: a round-trip through a shell heredoc.
NL = chr(10)


def _fmt_value(v) -> str:
    """One parameter value, as an operator would want to read it."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    if v is None or v == "":
        return "-"
    return str(v)


def _param_block(items, indent: str = "  ") -> list[str]:
    """Key/value lines with the values column-aligned."""
    items = [(str(k), _fmt_value(v)) for k, v in items]
    if not items:
        return [f"{indent}(none)"]
    width = max(len(k) for k, _ in items)
    return [f"{indent}{k.ljust(width)}   {v}" for k, v in items]


def _step_lines(steps) -> list[str]:
    """Numbered recipe steps. Step.describe() already carries the duration for
    every step that has one, so there is no separate duration column."""
    if not steps:
        return ["  (none)"]
    out = []
    for i, s in enumerate(steps, start=1):
        gated = "   [freezes while the plasma is out]" if s.lit_gated else ""
        out.append(f"  {i:>2}. {s.describe()}{gated}")
    return out


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def format_run_params(params: dict, recipe, run_name: str = "",
                      recorded_at: datetime | None = None) -> str:
    """The run's settings as a plain-text report.

    This used to be a JSON dump. Zach could not open it ("I dont know how to
    open them"), which is fair: a .json has no default handler on this machine
    and the nesting made it unreadable anyway. A .txt double-clicks into
    Notepad and reads like the settings sheet it is. Nothing is dropped -
    every UI parameter and every recipe step is still here, just laid out.
    """
    when = recorded_at or datetime.now()
    cycle_s = recipe.cycle_seconds()
    total_s = cycle_s * recipe.cycles
    mode = "EE-CVD" if recipe.mode == "cvd" else "EE-ALD"

    L: list[str] = []
    L.append("RUN PARAMETERS")
    L.append("=" * 60)
    L.append("")
    L += _param_block([
        ("Run", run_name or "(unnamed)"),
        ("Recorded", when.strftime("%Y-%m-%d %H:%M:%S")),
        ("Recipe", recipe.name),
        ("Mode", mode),
        ("Cycles", recipe.cycles),
        ("Cycle length", f"{cycle_s:g} s"),
        ("Nominal run time", f"{_fmt_duration(total_s)}  ({total_s:g} s)"),
    ])
    L.append("")
    L.append("Nominal run time is the cycling phase only - the pre-start, the")
    L.append("setup and the end-of-run steps below are not counted, and a")
    L.append("reignite makes the real run longer.")
    L.append("")

    L.append("SUMMARY")
    L.append("-" * 60)
    L += [f"  {ln}" for ln in _describe_recipe(recipe).splitlines()]
    L.append("")

    L.append("PARAMETERS SET IN THE UI")
    L.append("-" * 60)
    L += _param_block(sorted(params.items()))
    L.append("")

    L.append("RECIPE STEPS")
    L.append("-" * 60)
    L.append("Setup (once, before the first cycle)")
    L += _step_lines(recipe.setup)
    L.append("")
    L.append(f"Cycle (repeated {recipe.cycles} times)")
    L += _step_lines(recipe.steps)
    L.append("")
    L.append("End of run")
    L += _step_lines(recipe.teardown)
    L.append("")

    L.append("GAS SCHEDULE")
    L.append("-" * 60)
    if recipe.gas_schedules:
        L += _param_block(
            [(g.mfc, f"{g.order}, {g.pct:g}% of the window @ {g.flow_sccm:g} sccm")
             for g in recipe.gas_schedules])
        L.append(f"  handoff overlap   {recipe.gas_overlap_s:g} s "
                 f"(the incoming gas starts this early)")
    else:
        L.append("  (no scheduled gas)")
    L.append("")
    return NL.join(L) + NL


