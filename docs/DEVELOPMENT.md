# Development guide

## Start here

This is a Python/FastAPI controller for a single UHV electron-beam ALD/CVD
reactor, with a browser UI using vanilla JavaScript ES modules and no build step.
[ARCHITECTURE.md](ARCHITECTURE.md) owns the runtime map;
[CONTROL_MODEL.md](CONTROL_MODEL.md) owns requested automatic behavior;
[RUN_PROGRAM.md](RUN_PROGRAM.md) owns recipe semantics;
[HARDWARE.md](HARDWARE.md) owns hardware identification;
[the test guide](../tests/README.md) explains the fake-device harness and its limits.
Use Beads for work history and outstanding tasks.
See [INTEGRATION.md](INTEGRATION.md) for branch provenance, compatibility and rollback.

## Environments

The reactor host is Windows with NI-DAQmx and instrument drivers. Development
can run on macOS or Linux without those drivers; tests inject fake hardware.
Use Python 3.12 for a consistent development/CI environment. The existing suite
also runs on the current Mac Python 3.11 environment. Create a local environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m tests.run_all
```

In Windows PowerShell use `py -3.12 -m venv .venv`, then
`.venv\Scripts\python.exe -m pip install -r requirements.txt` and
`.venv\Scripts\python.exe -m tests.run_all`. Commands below use `python` to mean
that environment's interpreter. Node is development tooling for browser-module
checks, not a reactor runtime dependency. Do not start the hardware server to
run tests. `python -m reactor --check` validates configuration without serving.

## Change-to-test map

Run a listed Python test as `python -m tests.<module>` (omit `.py`). Paths below
identify the owner; public Supervisor commands remain the hardware boundary.

| Change | Read first | Focused Python modules |
|---|---|---|
| Dependency construction, instance paths and controller contracts | [dependencies.py](../reactor/dependencies.py), [contracts.py](../reactor/control/contracts.py), [supervisor.py](../reactor/supervisor.py) | `test_dependencies`, `test_controller_contracts` |
| Run admission, cancellation and cleanup | [run_coordinator.py](../reactor/control/run_coordinator.py), [supervisor.py](../reactor/supervisor.py), [recipe.py](../reactor/control/recipe.py) | `test_run_admission`, `test_run_lifecycle`, `test_hv_and_prestart_abort` |
| Live edits, reports and instance isolation | [run_coordinator.py](../reactor/control/run_coordinator.py), [recording.py](../reactor/recording.py) | `test_live_params`, `test_merge_integration`, `test_run_export` |
| Pause, bias and simultaneous gases | [recipe.py](../reactor/control/recipe.py) | `test_pause`, `test_sample_bias_bracket`, `test_gas_simultaneous` |
| Process shutdown and recording receipts | [instances.py](../reactor/instances.py), [supervisor.py](../reactor/supervisor.py) | `test_server_shutdown`, `test_shutdown_teardown` |
| ALD/CVD timing and gas scheduling | [recipe.py](../reactor/control/recipe.py), [recipe_model.py](../reactor/control/recipe_model.py), [clock.py](../reactor/control/clock.py) | `test_ee_ald_recipe`, `test_ee_cvd_recipe`, `test_run_timing`, `test_cycle_numbering`, `test_clock_domains`, `test_merge_acceptance` |
| Timing prototype and production trace compatibility | [timing_prototype.py](../reactor/testing/timing_prototype.py), [recipe.py](../reactor/control/recipe.py), [clock.py](../reactor/control/clock.py) | `test_timing_prototype`, `test_clock_domains` |
| Run and pre-start parameters | [parameters.py](../reactor/control/parameters.py), [recipe_model.py](../reactor/control/recipe_model.py), [prestart.py](../reactor/control/prestart.py) | `test_parameters`, `test_prestart_invalid`, `test_run_admission` |
| Pre-start and supplies | [prestart.py](../reactor/control/prestart.py), [supervisor.py](../reactor/supervisor.py) | `test_prestart`, `test_prestart_invalid`, `test_keithley_supplies`, `test_hv_and_prestart_abort` |
| Fill regulation and MFC interlocks | [fill.py](../reactor/control/fill.py), [supervisor.py](../reactor/supervisor.py) | `test_fill_controller`, `test_mfc_interlock`, `test_prestart`, `test_ee_ald_recipe` |
| Valve identification sweep | [sweep.py](../reactor/control/sweep.py), [supervisor.py](../reactor/supervisor.py) | `test_sweep_controller` |
| Recording and file formats | [recording.py](../reactor/recording.py), [datalog.py](../reactor/datalog.py) | `test_recording_api`, `test_recording_worker`, `test_recording_errors`, `test_run_export`, `test_file_naming`, `test_sample_freshness` |
| Telemetry and data routes | [telemetry.py](../reactor/telemetry.py), [data.py](../reactor/server/data.py) | `test_telemetry`, `test_data_routes`, `test_ellipsometer_merge` |
| Frontend modules | [control.js](../reactor/server/static/control.js), [live-charts.js](../reactor/server/static/live-charts.js), [analysis.js](../reactor/server/static/analysis.js) | `test_static_assets` plus Node commands below |
| Documentation and entry guidance | [AGENTS.md](../AGENTS.md), [test guide](../tests/README.md) | `test_docs` |

```sh
python -m reactor.testing.validate full --require-node
python -m compileall -q reactor tests
git diff --check
```

Use `python -m reactor.testing.validate control` (or `recording`, `api`,
`frontend`) for a focused group. Without `--require-node`, missing Node is
reported as skipped. CI requires Node and runs on Linux and Windows.

Run the affected baseline before editing, add regression coverage for the
behavior at stake, run focused checks after each step, and run the complete
applicable checks before handoff. Prefer observed test conditions over guessed
sleep durations. Retain real asynchronous integration tests alongside any
simulated-clock tests. A passing test with fake devices proves software
sequencing, not valve response, physical timing or browser appearance.

## Runtime facts that must stay explicit

The last-commanded valve state is not measured valve position. Recording errors
are latched for the server session because a later successful write cannot
restore lost data. Run timing is cooperative software timing, with no verified
upper jitter bound; elapsed exposure and wall timestamps have different purposes.
See the architecture and control documents for details rather than duplicating
hardware specifications or historical run narratives in agent entry files.
