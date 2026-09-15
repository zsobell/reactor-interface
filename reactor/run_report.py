"""Pure operator-facing recipe and parameter reports."""
from datetime import datetime

def _describe_recipe(recipe, gas_names: dict[str, str] | None = None) -> str:
    """Plain-English summary of a built EE-ALD / EE-CVD Recipe - cycle
    architecture and the gas-schedule timeline, for a human skimming the run
    snapshot file.

    The gas window is anchored differently per mode, and this mirrors exactly
    what the runner measures against (see control/recipe.py's GasSchedule):
    EE-ALD divides up the beam step's exposure, EE-CVD the whole cycle. Both
    handoffs move the incoming gas earlier by the single Recipe.gas_overlap_s.

    Simultaneous gases do not divide the window at all - they each cover the
    whole of it - so their percentages are deliberately NOT printed. A run
    report that said "60% of the window" for a gas that ran the whole window
    would be a lie in the one file kept to say what the run actually did.
    """
    # The gas a line is ACTUALLY flowing, not the channel id: the MFC's own
    # selection moves (the `h2` line runs NH3), and this report is the file that
    # says what the run did. Falls back to the id for a device that never said.
    def gas(mfc_id: str) -> str:
        return (gas_names or {}).get(mfc_id) or mfc_id

    lines = [f"{recipe.name} — {recipe.cycles} cycles"]
    for s in recipe.steps:
        lines.append(f"  {s.describe(gas_names)}")
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
    simul = [g for g in recipe.gas_schedules if g.order == "simultaneous"]
    if simul:
        lines.append(f"  gas schedule (relative to {anchor} start): "
                     f"simultaneous - each covers the whole {anchor}")
        # EE-CVD's window IS the cycle and the beam never stops, so these are
        # not re-cycled per cycle: they come on once and stay on to run end.
        when = ("on at the first cycle, off at run end" if cvd
                else f"on at {anchor}-{ov:g}s, off at {anchor}+{span:g}s")
        for g in simul:
            lines.append(f"    {gas(g.mfc)}: {when} (@ {g.flow_sccm:g} sccm)")
        return "\n".join(lines)
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
        lines.append(f"    {gas(first.mfc)}: {on}, off at {anchor}+{handoff:g}s "
                     f"({first.pct:g}% @ {first.flow_sccm:g} sccm)")
    if second:
        lines.append(
            f"    {gas(second.mfc)}: on at {anchor}+{max(0.0, handoff - ov):g}s, "
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


def _step_lines(steps, gas_names=None) -> list[str]:
    """Numbered recipe steps. Step.describe() already carries the duration for
    every step that has one, so there is no separate duration column."""
    if not steps:
        return ["  (none)"]
    out = []
    for i, s in enumerate(steps, start=1):
        gated = "   [freezes while the plasma is out]" if s.lit_gated else ""
        out.append(f"  {i:>2}. {s.describe(gas_names)}{gated}")
    return out


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def _fmt_elapsed(seconds: float) -> str:
    """h:mm:ss from the start of the run, for the changes log."""
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def format_run_params(params: dict, recipe, run_name: str = "",
                      recorded_at: datetime | None = None,
                      changes: list[dict] | None = None,
                      gas_names: dict[str, str] | None = None) -> str:
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
    L += [f"  {ln}" for ln in _describe_recipe(recipe, gas_names).splitlines()]
    L.append("")

    L.append("PARAMETERS SET IN THE UI")
    L.append("-" * 60)
    L += _param_block(sorted(params.items()))
    L.append("")

    # Mid-run edits (2026-09-01). The block above is the CURRENT value of every
    # parameter - which for a run that was adjusted is not what it started
    # with, so the report would otherwise quietly describe a run that never
    # happened. Operator: "if I change a parameter mid run you should update
    # the document with a changes section that shows when (timestamp from start
    # and cycle #) a parameter was changed, and to what value."
    if changes:
        L.append("CHANGES DURING THE RUN")
        L.append("-" * 60)
        L.append(f"  {'ELAPSED':>8}  {'CYCLE':>7}  {'PARAMETER':<26}  "
                 f"{'FROM':>14}  ->  TO")
        for ch in changes:
            cyc = ch.get("cycle")
            cyc_s = (f"{cyc}/{recipe.cycles}" if cyc else "setup")
            L.append(f"  {_fmt_elapsed(ch.get('elapsed_s', 0.0)):>8}  "
                     f"{cyc_s:>7}  {str(ch.get('key', '')):<26}  "
                     f"{_fmt_value(ch.get('old')):>14}  ->  "
                     f"{_fmt_value(ch.get('new'))}")
        L.append("")
        L.append("  Times are from the start of the run. A parameter changed")
        L.append("  more than once appears once per change, in order.")
        L.append("")

    L.append("RECIPE STEPS")
    L.append("-" * 60)
    L.append("Setup (once, before the first cycle)")
    L += _step_lines(recipe.setup, gas_names)
    L.append("")
    L.append(f"Cycle (repeated {recipe.cycles} times)")
    L += _step_lines(recipe.steps, gas_names)
    L.append("")
    L.append("End of run")
    L += _step_lines(recipe.teardown, gas_names)
    L.append("")

    L.append("GAS SCHEDULE")
    L.append("-" * 60)
    if recipe.gas_schedules:
        L += _param_block(
            [((gas_names or {}).get(g.mfc) or g.mfc,
              f"{g.order}, whole window @ {g.flow_sccm:g} sccm"
                     if g.order == "simultaneous" else
                     f"{g.order}, {g.pct:g}% of the window @ {g.flow_sccm:g} sccm")
             for g in recipe.gas_schedules])
        if any(g.order == "simultaneous" for g in recipe.gas_schedules):
            # No handoff to overlap. In EE-ALD the same number still leads the
            # gases into the beam; in EE-CVD it does nothing at all.
            L.append(f"  overlap           {recipe.gas_overlap_s:g} s "
                     + ("(unused - simultaneous gases run the whole cycle)"
                        if recipe.mode == "cvd"
                        else "(how early the gases lead the beam in)"))
        else:
            L.append(f"  handoff overlap   {recipe.gas_overlap_s:g} s "
                     f"(the incoming gas starts this early)")
    else:
        L.append("  (no scheduled gas)")
    L.append("")
    return NL.join(L) + NL
