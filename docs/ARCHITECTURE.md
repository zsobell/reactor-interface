# Runtime architecture

One Python process owns one reactor. `python -m reactor` validates the hardware
map, creates the FastAPI application, and uses its lifespan to connect and stop
the Supervisor. Run one Uvicorn worker: multiple workers would each construct a
Supervisor and compete for the same DAQ modules and serial ports.

## Ownership and dependencies

```mermaid
flowchart TD
    Browser[Control page] -->|HTTP commands| API[FastAPI app]
    API --> Supervisor
    Supervisor --> RunCoordinator
    RunCoordinator --> RecipeRunner
    Supervisor <--> RecipeRunner
    Supervisor <--> PrestartController
    Supervisor --> Devices[Device adapters]
    Devices <--> Hardware
    Supervisor --> Telemetry
    Telemetry -->|WebSocket snapshots| Browser
    Supervisor --> RecordingService
    RecordingService -->|dedicated worker| DataLogger
    DataLogger --> Files[Run files]
    Analysis[Analysis page] --> DataRoutes[Data routes]
    DataRoutes -->|worker thread| Files
```

- `dependencies.py` supplies device factories and per-instance `StatePaths`.
  Production and VirtualReactor share Supervisor startup/shutdown; tests can
  disable background polling while retaining the same connection lifecycle.
- `supervisor.py` owns connections, live readings, valve commands, fill
  commands and physical run cleanup. Its public command methods remain
  the application hardware boundary.
- `control/recipe_model.py` defines the schema and pure ALD/CVD builders.
  `control/recipe.py` executes them and maintains exposure/cycle clocks; it
  re-exports the schema/builders for existing imports.
- `control/clock.py` supplies named elapsed and wall-clock callables. Supervisor
  injects one `Clock` into the coordinator, recipe runner and pre-start controller.
  VirtualReactor accepts a clock for deterministic duration and timestamp tests.
- `control/contracts.py` describes the capabilities controllers consume. They
  query public run admission and report events without accessing private locks.
- `control/fill.py` owns fill regulation; `control/sweep.py` owns valve
  identification. Supervisor delegates progress and commands. Sweep stop/shutdown
  bounds each release and task-settlement attempt and cancels after a grace
  period. It may return while an uncooperative adapter is still unwinding, with
  device cleanup unresolved until that task finishes. Unfinished sweep and
  release tasks retain ownership, so a new sweep cannot overlap late I/O.
  Failed or timed-out releases report an unconfirmed output state instead of
  claiming the lines are low.
- `control/prestart_model.py` owns the versioned recipe schema, device/action
  capability catalog, parameter resolution and pure preview generation.
  `control/prestart_store.py` owns the server-side library, atomic replacement
  and optimistic revision checks. `control/prestart.py` snapshots one resolved
  start/abort sequence, owns its task and progress, and calls Supervisor public
  methods for every hardware action. Stop hands over primed; abort runs the
  snapshotted recipe cleanup. The protected Current recipe retains the prior
  Ar/fill/relay/HV/DC-supply behavior.
- `telemetry.py` builds independent snapshots and manages bounded subscriber
  queues. A later reading cannot mutate an already published frame. Each slow
  viewer retains at most four frames; older frames are dropped.
- Live recording callers use explicit lifecycle/sample/capture methods and
  read metadata from RecordingService snapshots. Generic dispatch remains a
  compatibility seam for isolated tests.
- `recording.py` serializes recording operations on its own single worker.
  `datalog.py` owns file formats/handles, while `run_report.py` formats the plain
  text parameter report. Callers in the live application use RecordingService;
  direct DataLogger calls are intended for isolated synchronous format tests.
- `server/data.py` owns file discovery, containment checks, merge orchestration
  and analysis HTTP routes. It receives a directory, never a Supervisor.
  `analysis/ellipsometer_merge.py` remains pure text/number processing.

## Polling and experiment timing

Three asyncio tasks poll at separate rates: DAQ/supplies at `loop_hz` 2 Hz,
current/telemetry at `current_hz` 5 Hz, and MFCs at `mfc_hz` 6 Hz by default.
Blocking instrument calls use worker threads and device locks. MFC reads cannot
hold up the current loop. Recipes and pre-start execute on the event loop.

`Clock.elapsed` is monotonic and drives exposure credit, holds, deadlines,
cycle progress and ETA. `Clock.wall` supplies epoch timestamps for events,
telemetry and run metadata. DataLogger retains its own wall-clock origins for
manual logs and their filenames. The run CSV's
`elapsed_s` column retains its historical wall-clock subtraction
(`sample["t"] - run_started_at`) for file-format compatibility; it is not the
time source for control decisions and can reflect a system-clock adjustment.

The live snapshot carries the last known value. Run exports use read-sequence
counters to leave a channel blank unless it was measured since the previous
row. Samples and recipe progress are copied before crossing to the recording
worker, so a later tick cannot change a queued row.

Recording samples use a bounded backlog (2,048 pending jobs by default). When
full, further samples are rejected and a persistent recording error is shown;
control continues. Lifecycle operations are ordered behind accepted writes.
Stopping a run performs hardware cleanup before waiting for its recording to
close. Server shutdown gives recording close a five-second deadline and reports
timeout or latched recording errors in its receipt. A released device port does
not prove that queued files were flushed. Accepted worker jobs retain ownership
after caller cancellation; the process exit deadline can still interrupt a hung
filesystem operation.

Analysis file reads, merging and saving run in a worker, separate from the
recording executor. This removes synchronous analysis work from the control
event loop, but it is not a hard real-time guarantee: CPU work still shares the
Python process, and small settings writes remain synchronous. Per-run event and
error files use the recording worker; recording-error events are not submitted
back to that failing writer. Hard physical deadlines require measured end-to-end timing and
suitable hardware, such as supported hardware-clocked DAQ output or a PLC;
deployment measurements remain separate from fake-device acceptance in Beads.

## Run admission and recording failures

`control/run_coordinator.py` owns the admission lock, cancellation flag and an
immutable `RunSession` containing selected valve IDs and cleanup policy.
`idle`/`finished` → `preparing` → `executing` → `finishing` → `finished` is the
normal lifecycle. A cancelled preparation restores prior metadata and finishes
without executing a recipe. Pause/reignition remain runner phases within an
executing session. `in_progress` also observes the runner task, keeping admission
closed through its final unwind. Pre-start phases remain separate.

All run starts share the coordinator lock. A running recipe or pre-start is checked before
changing run metadata or opening another export. Recording is prepared before
starting recipe execution. Abort/shutdown can cancel a start waiting on file
preparation, without starting the recipe or advancing the remembered run name.
Live edits share an edit lock with run completion. They preserve the admitted
hardware identities and append change history using elapsed time; a supplied
`_run_started_at` rejects edits from a different run.

Recording errors (open, header/row write, flush, close, or backlog overflow)
appear in `logging.errors`, the event log, and the control page's alert chip.
Repeated identical errors are deduplicated. Errors remain visible for the server
session because later successful writes do not repair missing experiment data.
Recording failure does not add an automatic hardware action or stop a run.

## Browser and persistence

There is no frontend build step. `index.html` loads `control.css` and the ES
module `control.js`; `live-charts.js` owns plotting and chart interaction through
an explicit `createLiveCharts` interface. `analysis.html` loads `analysis.css`
and `analysis.js`. `control-transport.js` owns HTTP/WebSocket lifecycle,
`control-run-forms.js` owns parameter persistence/modes, and
`control-device-panels.js` owns supply/MFC/valve rendering and commands. Cached
page navigation suspends/resumes transport; final unload disposes handlers.
Static assets revalidate on reload.

Hardware mapping is YAML. Labels, last-commanded valve state, last-started run
name, shared run parameters, the pre-start recipe library and the shared
analysis layout are JSON. All state
paths, including the process registry, are supplied through `StatePaths`.
Browser localStorage caches parameters
and stores display preferences. Experimental records are CSV/text under
`data/<run>/`. Beads' Dolt database tracks development work, not reactor state.
Valve state is commanded state, not measured position, and restoring it never
writes a hardware output.

The server binds to `0.0.0.0` by default. `REACTOR_PASSWORD` enables Basic auth
for pages, API and WebSocket; `--host 127.0.0.1` binds locally. The control page
uses `wss:` when served over HTTPS. See CONTROL_MODEL.md for the exact, explicitly
requested automatic actions and lifecycle behavior.

## Validation

Run `python -m tests.run_all` for the Python regression suite. Tests use temporary
files and fake hardware, with the real Supervisor and extracted controllers.
`node tests/js/live-charts.mjs` checks chart rendering and interaction with a
small canvas/DOM harness. `node tests/js/control-bootstrap.mjs` checks module
bootstrap and chart integration. Node is optional development tooling, not a runtime
dependency. These checks do not validate physical hardware or browser visuals.

Typed input models in `control/parameters.py` preserve the original raw payload
for settings/reports. Run inputs normalize once before building a recipe.
Pre-start normalizes each stage when it is reached: moving a failing conversion
earlier can change which existing commands precede cleanup.
