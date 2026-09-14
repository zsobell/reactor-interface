# Agent instructions

Read [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup, module ownership and
focused validation commands. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains
runtime dependencies; [docs/CONTROL_MODEL.md](docs/CONTROL_MODEL.md) is the
authoritative description of requested reactor behavior.

## Working rules

- Zach governs reactor behavior. Preserve the existing requested interlocks,
  sequences and cleanup. Do not introduce new automatic hardware actions without
  his explicit authorization. Do not actuate physical hardware unless directed.
- One process owns one reactor. Restoring last-commanded valve state must not
  actuate hardware. User-editable run parameters belong in the UI.
- Use Beads for all durable task tracking and memory, never markdown TODO lists
  or MEMORY.md. Read the [Beads skill](.agents/skills/beads/SKILL.md); the shared
  Claude skill, when installed alongside this checkout, is
  `../patterns/skills/beads/SKILL.md`.
- Begin with `bd prime` and `bd ready`; inspect `bd show <id>` and claim with
  `bd update <id> --claim` before editing. Create focused issues for new work.
  Close completed issues immediately and record durable context with `bd remember`.
- Add and run meaningful tests for each change. Establish a baseline, verify each
  implementation step, and run the full applicable checks before handoff. Tests
  use fake devices and temporary files; software tests do not validate hardware.
- Delegate bounded independent issues when authorized. Give agents distinct file
  ownership, review their changes and rerun integration checks before acceptance.
- Use non-interactive file operations (`cp -f`, `mv -f`, `rm -f`; recursive forms
  `cp -rf`, `rm -rf`), and `ssh`/`scp -o BatchMode=yes` when applicable.
  Use `apt-get -y` and `HOMEBREW_NO_AUTO_UPDATE=1` for unattended installs.

## Handoff and repository authority

The default profile is conservative: do not make Git commits, Git pushes or
Dolt remote syncs without explicit user authorization. This overrides generic
commit/push instructions printed by `bd prime`. At handoff, report changed files,
validation, issue status and any remaining blocker. Reference issue IDs in commit
messages when commits are authorized.

Beads issues and memories live in a local Dolt database; inspect its location
with `bd where`. Authorized cross-machine sync uses `bd dolt push/pull` under
`refs/dolt/data`, separate from code branches. `.beads/issues.jsonl` is a passive
export, not the source of truth. Do not import it during normal operation or
reinitialize a database to solve a routine setup problem.
