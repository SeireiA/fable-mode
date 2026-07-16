"""Command-line interface for the strict fable runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .executor import CodexExecutor
from .models import (
    Manifest,
    ManifestError,
    codex_command,
    find_repo_root,
    load_manifest,
    validate_models,
)
from .scheduler import Scheduler, cancel_run, initial_state, new_run_id, render_state
from .state import RunStore, find_run_dir


def _repo_from_cwd() -> Path:
    return find_repo_root(Path.cwd())


def _store_for_id(run_id: str) -> RunStore:
    return RunStore(find_run_dir(_repo_from_cwd(), run_id))


def _load_saved_manifest(store: RunStore) -> Manifest:
    state = store.read()
    return load_manifest(
        store.run_dir / "manifest.json",
        require_open_ledger=False,
        routing_override=state["routing"],
        tier_override=state["tier"],
    )


def command_run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    command = codex_command()
    validate_models(command, manifest.models)
    run_id = new_run_id()
    store = RunStore(manifest.repo_root / ".fable" / "runs" / run_id)
    store.create(initial_state(manifest, run_id), manifest.path.read_text(encoding="utf-8"))
    print(f"run {run_id}: starting", flush=True)
    state = Scheduler(manifest, CodexExecutor(command), store).run()
    print(render_state(state))
    return 0 if state["status"] == "completed" else 1


def command_status(args: argparse.Namespace) -> int:
    state = _store_for_id(args.run_id).read()
    print(json.dumps(state, ensure_ascii=False, indent=2) if args.json else render_state(state))
    return 0


def command_resume(args: argparse.Namespace) -> int:
    store = _store_for_id(args.run_id)
    manifest = _load_saved_manifest(store)
    command = codex_command()
    validate_models(command, manifest.models)
    state = Scheduler(manifest, CodexExecutor(command), store).run(resume=True)
    print(render_state(state))
    return 0 if state["status"] == "completed" else 1


def command_cancel(args: argparse.Namespace) -> int:
    state = cancel_run(_store_for_id(args.run_id))
    print(render_state(state))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m fable_runner")
    commands = root.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="validate and run a workflow manifest")
    run.add_argument("--manifest", type=Path, required=True)
    run.set_defaults(handler=command_run)

    status = commands.add_parser("status", help="show persisted run state")
    status.add_argument("--run-id", required=True)
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_status)

    resume = commands.add_parser("resume", help="resume tasks interrupted by runner exit")
    resume.add_argument("--run-id", required=True)
    resume.set_defaults(handler=command_resume)

    cancel = commands.add_parser("cancel", help="cancel active worker process trees")
    cancel.add_argument("--run-id", required=True)
    cancel.set_defaults(handler=command_cancel)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except (ManifestError, OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"fable-runner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
