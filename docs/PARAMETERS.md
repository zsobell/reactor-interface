# Parameter normalization

[`control/parameters.py`](../reactor/control/parameters.py) owns typed UI parameter
views. The original dictionary remains the public API payload and the input to
saved settings and human-readable experiment reports. Typed views capture an
independent copy, retaining unknown fields, original strings and explicit nulls.
Changing a request dictionary after submission cannot change the captured run.
Legacy `h2_gas_*` and `n2_gas_*` keys migrate to `mfc1_gas_*` and
`mfc2_gas_*`; an explicitly supplied channel key wins. Channel identity stays
stable when the gas selected on a device changes. Reports receive each
instance's gas names rather than sharing a global gas map.

`RunParameters.normalize(payload, mode=...)` converts run fields once at the
Supervisor entry point. Builders accept either this object or a dictionary for
compatibility with callers that only build recipes. Passing an already normalized
object for its own mode reuses it. Explicit nested fill, beam and gas models keep
controller dependencies discoverable; raw values are available through `.raw`,
which returns an independent copy for reporting. File recipes still use the
existing `Recipe` and `Step` schemas directly.

The normalization is deliberately compatible with existing Python coercions:

- Numeric fields use `float` and `int`; integer conversion of a fractional number
  truncates as before, and booleans retain their previous numeric meaning.
- Gas enable fields use Python truthiness. A nonempty string such as `"false"`
  enables a schedule, matching the old builder. Disabled gas details are ignored.
- CVD does not coerce ALD-only `beam_s` or `pump_b_s`, even if those fields contain
  values that would be invalid for ALD. Unused pre-start settings in a run payload
  remain report data rather than newly validated fields.
- Existing `Recipe`, `Step` and `GasSchedule` validation continues to reject
  invalid negative durations, invalid gas orders and conflicting gas schedules.
  No new Pydantic input constraints were added. Multiple simultaneous invalid
  run fields may now report a different first conversion error; failed building
  still occurs before admission or hardware commands.

The merged schema also retains sample-bias lead/trail brackets and paired
`simultaneous` gas schedules. Simultaneous gases each cover the whole window;
their percentages do not divide exposure. A lone simultaneous selection or a
mixture of simultaneous and sequential selections is rejected.

## Pre-start compatibility

The versioned pre-start recipe is resolved in full before its first hardware
command. Resolution validates target capabilities, converts typed arguments and
parameter references, and snapshots both start and abort steps. A malformed or
stale recipe therefore fails without actuation. Later edits affect the next
launch only. The controller retains the resolved abort snapshot until the run
accepts a primed handoff or abort cleanup completes.

Abort has one task per pre-start session. Concurrent callers join that task and
caller cancellation does not cancel physical cleanup. Admission remains closed
while cleanup is unwinding. Process shutdown has a separate bounded path which
explicitly cancels and records an unfinished owned abort before device release.

## Validation

```sh
python -m tests.test_parameters
python -m tests.test_prestart_invalid
python -m tests.test_prestart_sequence
python -m tests.test_run_admission
python -m tests.test_run_export
```

`test_parameters` compares complete recipes and parameter report text against
[`fixtures/parameters.json`](../tests/fixtures/parameters.json), captured before
normalization was introduced. Pre-start tests cover whole-recipe validation,
snapshot isolation, concurrent abort ownership and existing schema rejection.
Integration tests continue to use fake devices only.
