# Master and architecture integration

> Historical release record: this file describes the 2026-09 architecture
> integration and its acceptance counts, not the current feature inventory.
> Use [DEVELOPMENT.md](DEVELOPMENT.md) for current validation commands and
> `bd ready` for current work.

The integration branch combines master `2e39978` with architecture refactor
`59cd2a1`, based on `7dab6b3`. Beads epic `reactor-r1z` owns the exhaustive
100-path disposition ledger and release gates. The merge uses the refactor as
its first parent and master as its second parent; both histories are retained.

## Behavior and ownership

| Preserved behavior | Integrated owner |
|---|---|
| ALD/CVD, pause, bias lead/trail, simultaneous gases, monotonic duration accounting | RecipeRunner and pure recipe models |
| Typed parameters, legacy UI gas-key migration, channel identity | RunParameters and per-instance gas metadata |
| Soft open, live fill tuning, pre-start cleanup | Supervisor commands and extracted controllers |
| Live edits, immutable cleanup identities, admission through drain | RunCoordinator |
| Run reports, event/error files, freshness, adopted ellipsometer sidecars | RecordingService and DataLogger |
| Frozen diagnostics, current gas names, bounded subscriptions | Telemetry |
| Analysis layout and union-of-instants CSV merge | App settings and worker-owned data routes |
| Device-release and recording-failure receipt | Shared Supervisor shutdown task and scoped process registry |
| Run/Hardware/Diagnostics/Analysis, charts, cache navigation | Native browser modules and CSS |

Reports are queued before a live edit releases its control lock, but waiting
for their disk completion happens outside that lock. A stalled report therefore
cannot delay hardware cleanup. Accepted reports remain ordered before close.
Recording errors remain latched and do not recursively log through a failed
writer. Gas names are supplied per instance to report summaries and steps.

All 42 inherited Python modules are retained. `test_merge_integration.py` adds
the edit/drain intersection and per-instance report/telemetry checks. The
simultaneous-gas test observes actual exposure samples rather than mistaking
separate gas-off and relay-off timestamps at cleanup for a mid-exposure switch.
The browser bootstrap harness checks that seeded event history is rendered
without waiting for a new event.
`test_merge_acceptance.py` covers event/error stream faults, report rewrites,
nested diagnostic snapshots and bias scheduling across wall-clock jumps.

The CVD timing audit also found pump durations rounded to the watchdog poll.
The pump now wakes the watchdog at entry, excludes earlier dose time, and
caps its final sample wait to the remaining lit-time budget. Off-grid CVD
durations and countdown drift are covered alongside ALD timing; pause,
reignition and abort are exercised with fake devices.

## Saved settings and recipes

Runtime paths are injected through `StatePaths`. The production settings and
experiment data must remain separate from temporary validation instances.
Legacy UI keys `h2_gas_*` / `n2_gas_*` migrate to `mfc1_gas_*` /
`mfc2_gas_*`; explicit channel keys win.

File recipes contain explicit hardware IDs, rather than UI parameter keys.
Before using an old YAML recipe against the renamed hardware map, make a copy
and replace `mfc: h2` with `mfc: mfc1` and `mfc: n2` with `mfc: mfc2` in
`set_flow` steps and gas schedules. Check these against the configured device
IDs. Historical YAML and recorded experiment files are not rewritten.

## Validation and rollback

The final Windows acceptance run passed all 44 Python modules and all three
Node harnesses. Its result is recorded in Beads `reactor-r1z.15` and
`.merge-backup/integration-release-final.log`. Configuration-only `--check`,
compilation and whitespace checks also passed. Browser review used fake adapters,
isolated state and a loopback port; no physical hardware was connected.

The user selected Windows-only support and removed the Linux release gate.
Windows is the supported development, CI and reactor platform. The integration
is accepted for local master; CI runs the same strict checks on Windows.

Local rollback references are `codex/pre-refactor-master-20260915` and
`codex/pre-refactor-architecture-20260915`. `.merge-backup/` contains the original
Git bundle, operator configuration/data backup and validation logs. It is
intentionally ignored, as are disposable integration worktrees. Keep a copy
of current operator settings before any deployment or rollback; a code rollback
must not rewind experiment data or the Beads database.

To inspect the previous production code without changing the deployment:

```sh
git show codex/pre-refactor-master-20260915:reactor/supervisor.py
git log --graph --oneline --decorate --all
```

If the integration merge is later promoted and needs reversing, first revert
any later integration fix commits in reverse chronological order. Then inspect the merge's
parents with `git show --no-patch --format=%P <merge-commit>` first. For this
refactor-first merge, `git revert -m 2 <merge-commit>` keeps the master parent;
using `-m 1` would keep the refactor parent instead. Review the resulting diff
and preserve current operator state before applying that rollback.
