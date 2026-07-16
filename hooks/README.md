# fable-mode guard hooks for Codex

This directory contains the Codex-compatible guard hooks. The repository is
derived from [`cozytab/fable5-mode`](https://github.com/cozytab/fable5-mode),
whose hook configuration is not compatible with the Codex Hooks mechanism.

Install and register these hooks with:

```powershell
py -3 ../install_codex.py
```

See [`../README.md`](../README.md) for complete installation instructions and
[`../README.codex.zh-CN.md`](../README.codex.zh-CN.md) for adaptation details.

## Hook map

| Hook | Codex event | Purpose |
|---|---|---|
| `fable_profile_inject.py` | `SessionStart` | Inject the selected tier, workflow rules, and ledger context. |
| `fable_spawn_guard.py` | `SubagentStart` | Inject a design-gate warning for substantial delegation without an open card. |
| `fable_fail_streak.py` | `PostToolUse` with `Bash` | Prompt for failure attribution after every third consecutive command failure. |
| `fable_close_guard.py` | `Stop` | Continue the turn while cards remain open or completed cards lack evidence. |

`fable_lint.py` is a one-shot CLI rather than a hook. It checks the project
specification and ledger for missing source tags, acceptance criteria, and
completion evidence:

```powershell
py -3 fable_lint.py <project_dir>
```

## Project opt-in

The scripts search upward from the current working directory for `.fable/`,
bounded by the Git root:

- With `.fable/`, the guards operate on the current project.
- Without `.fable/`, the hooks pass through without changing the session.

All scripts fail open. An unexpected script error does not block Codex.

## Ledger format

`.fable/LEDGER.md` is a small state machine for the current round:

```text
- [ ] 1. open card with a machine-checkable acceptance test
- [x] 2. completed card -- evidence: pytest 21/21
- [~] 3. deferred card -- deferred: reason
PAUSED: reason
ROUTING: balanced
TIER: conservative
```

- `- [ ]` is open and prevents the turn from closing.
- `- [x]` is complete and requires a substantive evidence marker.
- `- [~]` is explicitly deferred and closed for the current round.
- `PAUSED: reason` temporarily disables workflow enforcement.
- `ROUTING` selects `quality`, `balanced`, or `frugal` routing.
- `TIER` selects `throughput` or `conservative` concurrency.

`SPEC.md` and `PROGRESS.md` remain durable project documents. The ledger only
tracks the enforcement state for the current round.

## Current limitation

`SubagentStart` does not provide a way to cancel a built-in subagent. The
delegation guard therefore injects an advisory design-gate message after start.
The other mapped events support their intended enforcement behavior.

## Safety

- Hooks are inactive outside opted-in projects.
- The close guard is loop-safe.
- Hook exceptions pass through.
- Session state is stored under the temporary directory and expires.

## Tests

Run the standard-library test suite from the repository root:

```powershell
py -3 tests/test_codex.py
py -3 tests/test_guards.py
py -3 tests/test_inject.py
```
