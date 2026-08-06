# Testing methodology: the virtual reactor

How this project verifies control logic without touching real hardware, and
— just as important — what that verification does and does not prove.

## No pytest, on purpose

There is no test framework here. `tests/test_*.py` are plain scripts with a
`main()` that prints `PASS`/`FAIL` lines and returns a process exit code.
Run one directly:

```bash
python -m tests.test_ee_cvd_recipe
```

or all of them:

```bash
python -m tests.run_all
```

This matches how the rest of the project is verified — `tools/*.py` are
also plain scripts, and `CLAUDE.md` has always said "there is no pytest
suite." If a real framework starts pulling its weight (parametrization,
fixtures, parallel runs) that's worth revisiting, but it's a deliberate
choice made once already, not an oversight — bring it up before adding one.

## The core idea: fake the hardware boundary, not the software

The wrong way to test this codebase is to write a stand-in for `Supervisor`
that reimplements what `set_valve`/`set_mfc_setpoint`/etc. are supposed to
do. That tests your understanding of the code, not the code — and it can
pass while the real thing is broken, or fail while the real thing is fine,
because "the fake's `finish_run` zeroes every MFC" and "the real
`finish_run` zeroes every MFC only if `_run_end_cleanup` was armed" are two
different claims that happen to look similar from the outside. (This
happened during this project's own development: an earlier ad hoc harness
faked `Supervisor.finish_run()` directly, and every "MFCs zeroed at run
end" check passed — for the wrong reason. It was calling `RecipeRunner`
low-level enough to skip the exact code path that arms that behavior. See
`git log` around 2026-08-06 for the fix.)

The right way — what `reactor/testing/virtual_reactor.py` does — is to fake
only the three classes `Supervisor.start()` actually constructs:
`NiDaqBackend`, `MksMfc`, `ScpiInstrument`. Everything above that boundary —
`Supervisor` itself, `RecipeRunner`, every method in `recipe.py` — runs
completely unmodified, the exact same code path that runs against the real
reactor. A test doesn't call a look-alike `set_valve`; it calls the real
one, and the real one calls the fake `write_do`.

```
RecipeRunner / Supervisor.set_valve() / start_prestart() / ...   <- REAL, unmodified
        |
        v
Supervisor.daq.write_do()  Supervisor.mfcs["ar"].set_setpoint_sccm()  ...
        |                           |
        v                           v
   FakeDaq (in-memory dict)    FakeMfc (in-memory dict)      <- fake, "no physics"
```

## "No physics" — what that means and why it's fine

The fakes have no timing model, no settling curve, no failure-injection by
default. A digital-output write is readable back the instant it's written.
An MFC's flow tracks its commanded setpoint exactly. This is deliberate,
not a shortcut: what these tests verify is *sequencing and reaction* —
does the recipe open the right valve at the right point in the right
order, does a plasma dropout trigger a reignite, does an abort still close
the dose valve, does the cycle clock actually freeze while gated. None of
that requires modeling a solenoid's response curve. Timing itself is real
— `asyncio.sleep` is never mocked, so a test genuinely takes wall-clock
time and the actual scheduler runs the actual code, which is what caught
timing bugs (like a gas window landing at the wrong point when a cycle
boundary lands mid-tick).

## What this proves, and what it flatly cannot

**Proves:** the software reacts correctly to a given sequence of readings.
Command ordering, timeout/retry cadence, state transitions under an abort
or a pause, whether two independent pieces of logic (a gas schedule and a
reignite watchdog, say) interact correctly when driven by the same clock.

**Cannot prove anything about the physical reactor.** No test here can
tell you whether a valve actually opens when told, how fast, with what
deadband; whether `cDAQ2Mod1/ai1` really is the precursor-1 Baratron;
whether an MFC setpoint write actually round-trips over Modbus; whether the
plasma reignites in 150 ms or 1.5 s. Those are hardware questions. Every
`reactor-*` bd issue phrased as "confirm on real hardware" (channel
labelling, the fill valve, the first live EE-ALD run) stayed open until
someone actually ran the real thing — logic verification here was never
treated as a substitute for that, and it shouldn't be.

## How to write a new test

1. `async with VirtualReactor() as vr:` — gives you `vr.sup` (a real
   `Supervisor`), `vr.daq`, `vr.mfcs[id]`, `vr.instruments[id]` (the fakes).
2. Drive the real entry points, not internals — `vr.sup.start_cvd_run(...)`,
   `vr.sup.set_valve(...)`, `vr.sup.start_prestart(...)`. Going in through
   the same door the UI uses is what makes a test trustworthy; see the
   `finish_run`/`_run_end_cleanup` story above.
3. If the thing you're testing needs telemetry to flow (the trend buffer,
   the run-export CSV, anything read from `vr.sup.snapshot`), start
   `tests._support.autotick(vr)` — a background task standing in for the
   real `_control_loop`/`_current_loop`, on your own schedule instead of a
   timer's.
4. Poke fake values directly: `vr.instruments["ammeter"].value = 0.0`
   simulates the plasma going out; `vr.daq.raw["gauge.prec1_dose"] = 0.021`
   is a raw-volts Baratron reading (Supervisor applies its own scaling
   curve, same as it would to a real one — see the docstring in
   `virtual_reactor.py` for the exact per-channel-kind semantics).
5. If a check depends on timing relative to *when something actually
   happens* rather than a fixed wall-clock offset (e.g. "the plasma drops
   mid-exposure"), poll for the real condition (`progress.beam.get("lit")`)
   before acting, don't guess an offset. `test_ee_ald_recipe.py`'s section 2
   has a worked example and a comment on exactly why the naive version was
   flaky.
6. Use `tests._support.Checker` for the `PASS`/`FAIL` bookkeeping so output
   stays consistent across files.
7. One `VirtualReactor` is commonly reused across several sections in a
   file for speed — if you do that, remember state carries over (an MFC
   setpoint set in section 2 is still set in section 3). Snapshot a
   "before" count (`len(vr.daq.do_writes)`, `len(vr.sup.events)`) and slice
   from there, or use an explicit sentinel value, rather than asserting a
   fresh-looking baseline. `test_prestart.py` section 4 has an example of
   getting this wrong and then fixed.

## Two safety guarantees the virtual reactor gives you for free

Both are load-bearing, not incidental — a test that forgets everything
else still can't damage the real project:

- `cfg.site.data_dir` is redirected to a throwaway temp directory, so the
  data logger and the automatic run-export never touch the project's real
  `data/`.
- `VALVE_STATE_PATH` / `LABELS_PATH` — hardcoded module-level constants in
  `supervisor.py`, not per-instance, so there's no constructor argument to
  redirect them — are monkeypatched to that same temp directory for the
  life of the `VirtualReactor` and restored on exit. A test can never
  overwrite the real `config/valve_state.json` or `config/labels.json`.
  Always use `async with VirtualReactor() as vr:` so this restoration is
  guaranteed even if the test raises.

## What's covered today

| File | Covers |
|---|---|
| `test_ee_ald_recipe.py` | Pulsed-beam mode: per-cycle beam pulse, gas lead/handoff off the single overlap, reignite mid-exposure, abort mid-dose |
| `test_ee_cvd_recipe.py` | Continuous-beam mode: beam on/off lifecycle, reignite watchdog, the cycle-clock gas schedule staying locked across a reignite, abort |
| `test_prestart.py` | The operator pre-start sequence: happy path, unlimited-retry strike, a drop mid-hold restarting the hold, grounding the beam however it ends |
| `test_run_export.py` | Automatic server-side per-run CSV: opens on start, closes on completion/abort, back-to-back runs don't collide |
| `test_mfc_interlock.py` | The Ar MFC isolation interlock — the one check no earlier ad hoc harness could exercise, because it lives in `Supervisor` itself, which every earlier fake replaced wholesale |

Two real bugs were found and fixed via this suite on 2026-08-06:
`RecipeRunner._apply_cvd_gas` not being called at a cycle boundary (a gas
window could be skipped and a gas left stranded on), and
`Supervisor.stop_prestart()` discarding the phase/done/strike-count info
`_run_prestart`'s own finally block had just set, so an operator-initiated
stop always reported back as bare "idle" instead of what actually happened.
