# ALD exposure timing proof of concept

Beads: `reactor-tm8`. The isolated model is
[`reactor/testing/timing_prototype.py`](../reactor/testing/timing_prototype.py).
Production does not import it. It covers one electron-beam step, without gas
scheduling, setup, teardown, supply cleanup or CVD. No hardware is accessed.

## Characterized behavior

[`test_timing_prototype.py`](../tests/test_timing_prototype.py) first executes the
actual `RecipeRunner._electron_beam` coroutine with a fake command adapter,
clock and sleep. These checks were added and passed before the model:

- Beam-on commands ground OFF. Beam-off commands ground ON, on ordinary exit
  and cancellation through the production step's `finally` block.
- The final sample interval is clamped to remaining exposure. Current measured
  at the end of a sample determines credit for that entire interval. Negative
  current above the absolute threshold counts too; missing current does not.
- Initial settling is a grace window, not an additional blind delay. Lit samples
  inside it count. A dead sample triggers restrike only if its **start** was
  outside initial grace. Repeated restrikes use pulse and settle sleeps; those
  intervals earn no exposure.
- Operator pause prevents starting the next sample. It does not interrupt an
  already running sample or restrike. It does not itself ground the beam.
- Plasma loss and operator pause are separate overlapping reasons for freezing
  progress. Resuming the operator pause does not clear the plasma-loss reason.
- Graceful abort sets the production abort flag and lets the current iteration
  finish before cleanup. A dead in-flight sample can therefore still command a
  restrike after abort is requested. Coroutine cancellation is a distinct path.

These are compatibility observations, not recommendations to change reactor
behavior. For example, replacing abort with immediate beam-off would change the
command trace. The prototype preserves graceful abort and exposes `cancel()` as
its separate cancellation path. Production's ten-second abort escalation is
outside this step-level model.

## Decision core and adapter boundary

`ExposureModel` receives a unique run ID and an injected monotonic clock. Events
are method calls in serialized arrival order: start, command acknowledgment,
timer expiry with a current reading, pause, resume, abort and cancellation. Each
method reads its event time from the injected clock. There is no wall clock,
I/O, task creation or sleep inside the model. Its state includes remaining
qualified exposure, initial strike time, current sample start, pause reasons,
one outstanding command and one outstanding deadline.

Commands are immutable returned values. An adapter performs the command and
acknowledges it; only then does its pulse, settle or sampling deadline start.
Consequently slow command completion consumes no exposure. Acknowledgment means
software command completion, not a measured physical valve transition.

Deadlines carry run identity, generation, phase and absolute elapsed-clock time.
The adapter schedules the deadline and returns that exact token. Early,
duplicate, superseded and previous-run tokens cannot produce commands. A late
valid sampling event earns the actual elapsed interval if its endpoint reading
is lit, matching the current loop; reported exposure is capped at the target.
The caller must generate a different run ID for every model instance.

The fake adapter in the tests executes commands outside transitions and advances
the clock directly to deadlines. All 96 combinations of four initial binary
plasma readings with selected pause/abort/cancellation cases produce the same timestamped
command traces as the production coroutine. Additional checks cover delayed
acknowledgment, missing current, overlapping pause reasons, stale events,
cancellation and backward-clock rejection. This bounded exhaustive exploration
uses the standard library; it does not claim exhaustive behavior verification.

## Library evaluation and adoption decision

The official [`transitions` documentation](https://github.com/pytransitions/transitions)
describes declarative transitions, guards, callbacks, queued processing,
hierarchical states and asynchronous extensions. Those features could help a
larger run coordinator visualize legal transitions. They do not define this
reactor's exposure-credit rule or the relationship between command completion,
timer identity and pause reasons; those remain application code.

For this bounded sequence, retain the plain model as a reference experiment.
Its small explicit methods keep timer invalidation and command acknowledgment
visible without dynamically attached triggers or a dependency. The library was
evaluated from its official interfaces, not installed or performance-benchmarked.
If shared hierarchical transitions become the main maintenance burden, a later
comparison can implement the same trace contract using `transitions`.

Do not adopt this model as the production runner yet. The experiment demonstrates
that event ownership and clock injection make compatibility testable; it does
not cover I/O failures, gas handoff, whole-run lifecycle, CVD clocks or hardware
feedback. Controller-level cleanup still belongs to the production controller.
In particular, cancellation invalidates model acknowledgments but cannot undo a
command already executing in an adapter. A real adapter would need serialized
commands and a reviewed cleanup ordering policy.

For `reactor-e1j.11`, adopt the smaller independent improvement: inject named
elapsed and wall-clock sources. Use monotonic time for duration/deadline and
pause accumulation while retaining wall time for experiment timestamps and
operator history. [Python's clock documentation](https://docs.python.org/3/library/time.html#time.monotonic)
specifies that monotonic time cannot move backwards and has an unspecified
reference point, so persist wall timestamps rather than monotonic values.
Retain the characterized endpoint-sampling, grace, pause and abort rules when
changing clock sources. A wall-clock jump should affect timestamp labels but
not earned exposure or deadlines.

## Validation and limits

```sh
python3 -m tests.test_timing_prototype
python3 -m tests.test_run_timing
python3 -m tests.test_ee_ald_recipe
python3 -m tests.run_all
```

The focused real-async run timing test passed before implementation. The
prototype checks pass without sleeps; retain the real-async tests because a fake
clock cannot reveal event-loop scheduling regressions. The production
characterization passes a `Clock` through the relevant constructors, letting
tests supply independent elapsed and wall sources without changing global
`time.monotonic` or `time.time` functions.

The experiment establishes software decision and command timing only. Sampling
age, scheduler lateness, communication delay and actuator response still
contribute to physical exposure error. Neither this model nor a state-machine
library provides a hard timing guarantee. No physical timing acceptance was
performed.
