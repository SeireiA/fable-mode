# fable-mode for Codex

A Fable-5-inspired work-discipline skill and hook set for Codex CLI and Codex
desktop.

This repository is derived from
[`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode). The upstream
hook configuration is not compatible with the Codex Hooks mechanism. This fork
adapts the installer, event mapping, command format, and guard behavior for
Codex while retaining the core workflow discipline.

## What it provides

- A plan gate before substantial implementation or delegation.
- Small, verifiable work cards recorded in `.fable/LEDGER.md`.
- Context recovery at session start.
- A reminder after repeated command failures.
- Evidence checks before a task is closed.
- Per-project opt-in: hooks stay inactive unless a `.fable/` directory exists.

The complete protocol is defined in [`SKILL.md`](SKILL.md). Hook mechanics are
documented in [`hooks/README.md`](hooks/README.md).

## Install

Prerequisites: Git, Python 3, and Codex CLI or Codex desktop.

Clone the repository into the shared Codex skills directory:

```powershell
git clone https://github.com/SeireiA/fable-mode.git "$HOME/.codex/skills/fable-mode"
cd "$HOME/.codex/skills/fable-mode"
py -3 install_codex.py
```

On macOS or Linux, use `python3 install_codex.py` instead of `py -3`.
If `CODEX_HOME` is set, install the repository under
`$CODEX_HOME/skills/fable-mode`.

The installer:

- enables the Codex Hooks feature in `$CODEX_HOME/config.toml`;
- adds a marked, idempotent hook block without replacing other settings;
- writes platform-specific Python commands;
- creates `config.toml.fable-mode.bak` before the first modification.

Restart Codex after installation. Use `/hooks` to review and trust the installed
commands. Re-run the installer after updating the repository.

## Enable it in a project

For substantial work, create the project state directory and ledger:

```powershell
New-Item -ItemType Directory .fable -Force
Copy-Item "$HOME/.codex/skills/fable-mode/templates/LEDGER.template.md" ".fable/LEDGER.md"
```

The hooks search upward from the current working directory for `.fable/`,
stopping at the Git root. Without that directory, they pass through silently.

You can also activate the skill explicitly by asking Codex to use fable-mode or
rigorous mode. See [`SKILL.md`](SKILL.md) for the activation rules and workflow.

## Codex hook mapping

| Function | Codex event | Behavior |
|---|---|---|
| Profile Injector | `SessionStart` | Injects the selected profile and current ledger context. |
| Delegation Guard | `SubagentStart` | Adds a design-gate warning after a subagent starts. This event cannot cancel a built-in subagent, so the guard is advisory. |
| Fail-Streak Reminder | `PostToolUse` with `Bash` | Adds an attribution prompt after every third consecutive command failure. |
| Close Guard | `Stop` | Continues the turn while cards remain open or completed cards lack evidence. |

All hooks fail open: an internal hook error does not block the Codex session.

## Update and uninstall

Update the skill and refresh its hook paths:

```powershell
cd "$HOME/.codex/skills/fable-mode"
git pull
py -3 install_codex.py
```

Remove the registered hooks:

```powershell
py -3 install_codex.py --uninstall
```

The uninstaller removes only the marked fable-mode block. It does not remove the
skill directory or unrelated Codex settings.

## Tests

The implementation uses only the Python standard library:

```powershell
py -3 tests/test_codex.py
py -3 tests/test_guards.py
py -3 tests/test_inject.py
```

## Upstream and license

Based on [`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode), with
Codex Hooks compatibility changes maintained in this repository.

[MIT](LICENSE) (c) 2026 cozytab and contributors.
