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

`PrestartParameters` captures the raw payload at admission. Its `opening()`,
`supplies()` and `fill()` methods return explicit typed stage objects. The
controller calls each only once, when the original code performed its numeric
conversions. This is intentional: eagerly converting all three stages would
change which hardware commands precede a malformed-input failure.

Opening conversion occurs in the background task before its hardware cleanup
`try/finally`. A malformed opening field is reported as a pre-start error and
clears its running/done flags without issuing hardware commands. This corrects
`reactor-1kl`, where conversion previously left the running flag set and blocked
later starts. Supply conversion fails inside the cleanup scope, before
turning supplies on. Fill conversion occurs after supply-on and Ar commands;
failure grounds the beam through the existing cleanup. Tests pin these distinct
behaviors. The opening-error fix was implemented separately from normalization
so its ownership change and absence of new cleanup commands are explicit.
Abort retains the captured raw identifiers so it undoes the same valve/MFC selection.

## Validation

```sh
python -m tests.test_parameters
python -m tests.test_prestart_invalid
python -m tests.test_prestart
python -m tests.test_run_admission
python -m tests.test_run_export
```

`test_parameters` compares complete recipes and parameter report text against
[`fixtures/parameters.json`](../tests/fixtures/parameters.json), captured before
normalization was introduced. Its fake pre-start host also verifies successful
command ordering and the three malformed-input conversion stages. Tests cover
raw snapshot isolation, legacy truthiness/coercions, ignored values and existing
schema rejection. Integration tests continue to use fake devices only.
