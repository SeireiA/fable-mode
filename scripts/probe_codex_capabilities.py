#!/usr/bin/env python3
"""Report Codex capabilities relevant to fable-mode without changing config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fable_runner.models import ManifestError, codex_command  # noqa: E402


HOOK_EVENTS = (
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStart",
    "SubagentStop",
)
KNOWN_AGENT_TYPES = ("explorer", "worker", "verifier")


def run(command: tuple[str, ...], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*command, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def feature_map(output: str) -> dict[str, dict[str, object]]:
    features: dict[str, dict[str, object]] = {}
    pattern = re.compile(r"^(\S+)\s{2,}(.+?)\s{2,}(true|false)$")
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if match:
            features[match.group(1)] = {
                "stage": match.group(2).strip(),
                "enabled": match.group(3) == "true",
            }
    return features


def model_slugs(output: str) -> list[str]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ManifestError("Codex model catalog is not valid JSON") from exc
    if isinstance(value, list):
        entries = value
    elif isinstance(value, dict) and isinstance(value.get("models"), list):
        entries = value["models"]
    else:
        raise ManifestError("Codex model catalog has an invalid structure")
    return [
        entry["slug"] for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
    ]


def _collect_schema_tokens(value: object, tokens: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            tokens.add(str(key))
            _collect_schema_tokens(item, tokens)
    elif isinstance(value, list):
        for item in value:
            _collect_schema_tokens(item, tokens)
    elif isinstance(value, str):
        tokens.add(value)


def runtime_schema(
    command: tuple[str, ...],
) -> tuple[dict[str, object], int]:
    with tempfile.TemporaryDirectory(prefix="fable-codex-schema-") as temporary:
        output_dir = Path(temporary) / "schema"
        result = run(
            command,
            "app-server",
            "generate-json-schema",
            "--experimental",
            "--out",
            str(output_dir),
        )
        tokens: set[str] = set()
        if result.returncode == 0:
            for schema_path in output_dir.rglob("*.json"):
                try:
                    schema = json.loads(schema_path.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise ManifestError(
                        f"Codex app-server schema is unreadable: {schema_path.name}"
                    ) from exc
                except json.JSONDecodeError as exc:
                    raise ManifestError(
                        f"Codex app-server schema is invalid JSON: {schema_path.name}"
                    ) from exc
                _collect_schema_tokens(schema, tokens)
        return (
            {
                "hook_events": [event for event in HOOK_EVENTS if event in tokens],
                "agent_types": [
                    agent_type
                    for agent_type in KNOWN_AGENT_TYPES
                    if agent_type in tokens
                ],
                "spawn_agents_on_csv_exposed": "spawn_agents_on_csv" in tokens,
            },
            result.returncode,
        )


def load_run_observations(path: Path) -> dict[str, object]:
    run_state_path = path.resolve()
    try:
        state = json.loads(run_state_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"run state not found: {run_state_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError("run state is not valid JSON") from exc
    if not isinstance(state, dict):
        raise ManifestError("run state must be a JSON object")
    tasks = state.get("tasks")
    events = state.get("events")
    if not isinstance(tasks, dict) or not isinstance(events, list):
        raise ManifestError("run state must contain task and event metadata")

    run_dir = run_state_path.parent
    manifest_path = run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError("saved run manifest is unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError("saved run manifest is not valid JSON") from exc
    model_map = manifest.get("models") if isinstance(manifest, dict) else None
    if not isinstance(model_map, dict) or not all(
        isinstance(model, str) and model for model in model_map.values()
    ):
        raise ManifestError("saved run manifest has invalid model metadata")
    allowed_models = set(model_map.values())

    requested_models = {
        task.get("model")
        for task in tasks.values()
        if (
            isinstance(task, dict)
            and isinstance(task.get("model"), str)
            and task.get("model") in allowed_models
        )
    }
    actual_models: set[str] = set()
    for task in tasks.values():
        if not isinstance(task, dict):
            raise ManifestError("run state contains an invalid task record")
        attempt_logs = task.get("attempt_logs", [])
        if not isinstance(attempt_logs, list):
            raise ManifestError("run state task attempt_logs must be an array")
        for relative_path in attempt_logs:
            if not isinstance(relative_path, str):
                raise ManifestError("run state contains an invalid attempt log path")
            log_path = (run_dir / relative_path).resolve()
            try:
                log_path.relative_to(run_dir)
            except ValueError as exc:
                raise ManifestError("attempt log path escapes the run directory") from exc
            try:
                metadata = json.loads(log_path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise ManifestError(f"attempt metadata is unreadable: {relative_path}") from exc
            except json.JSONDecodeError as exc:
                raise ManifestError(
                    f"attempt metadata is not valid JSON: {relative_path}"
                ) from exc
            if not isinstance(metadata, dict):
                raise ManifestError("attempt metadata must be a JSON object")
            requested_model = metadata.get("requested_model")
            if (
                isinstance(requested_model, str)
                and requested_model in allowed_models
            ):
                requested_models.add(requested_model)
            model = metadata.get("actual_model")
            if isinstance(model, str) and model in allowed_models:
                actual_models.add(model)

    active_tasks: set[str] = set()
    peak_concurrency = 0
    saw_concurrency_event = False
    for event in events:
        if not isinstance(event, dict) or event.get("scope") != "task":
            continue
        task_id = event.get("task_id")
        if not isinstance(task_id, str):
            continue
        previous_status = event.get("from")
        current_status = event.get("to")
        if previous_status != "running" and current_status == "running":
            active_tasks.add(task_id)
            saw_concurrency_event = True
        elif previous_status == "running" and current_status != "running":
            active_tasks.discard(task_id)
            saw_concurrency_event = True
        peak_concurrency = max(peak_concurrency, len(active_tasks))

    missing_observations = []
    if not actual_models:
        missing_observations.append(
            "Run metadata contained no allowlisted turn.completed model values."
        )
    if not saw_concurrency_event:
        missing_observations.append(
            "Run metadata contained no task concurrency transitions."
        )
    return {
        "actual_models": sorted(actual_models) or None,
        "requested_models": sorted(requested_models),
        "peak_concurrency": peak_concurrency if saw_concurrency_event else None,
        "source": str(run_state_path),
        "reason": " ".join(missing_observations) or None,
    }


def probe(run_state: Path | None = None) -> dict[str, object]:
    command = codex_command()
    version = run(command, "--version")
    features = run(command, "features", "list")
    models = run(command, "debug", "models", "--bundled")
    exec_help = run(command, "exec", "--help")
    schema, schema_exit_code = runtime_schema(command)
    parsed_features = feature_map(features.stdout)
    parsed_models = model_slugs(models.stdout) if models.returncode == 0 else []
    observations = (
        load_run_observations(run_state)
        if run_state is not None
        else {
            "actual_models": None,
            "requested_models": None,
            "peak_concurrency": None,
            "source": None,
            "reason": (
                "Actual models and peak concurrency require a live Codex run; "
                "pass --run-state to inspect persisted runner metadata."
            ),
        }
    )
    return {
        "codex_command": list(command),
        "version": version.stdout.strip() or None,
        "features": {
            name: parsed_features.get(name)
            for name in ("hooks", "multi_agent", "multi_agent_v2")
        },
        "models": parsed_models,
        "codex_exec": {
            "json": "--json" in exec_help.stdout,
            "model": "--model" in exec_help.stdout,
            "sandbox": "--sandbox" in exec_help.stdout,
            "disable_feature": "--disable" in exec_help.stdout,
        },
        "runtime_schema": schema,
        "runtime_observations": observations,
        "native_delegation": {
            "pre_spawn_hard_block": False,
            "reason": (
                "The current hook protocol cannot reject PreToolUse, and "
                "SubagentStart occurs after startup"
            ),
            "spawn_agents_on_csv": schema["spawn_agents_on_csv_exposed"],
            "actual_concurrency": None,
        },
        "exit_codes": {
            "version": version.returncode,
            "features": features.returncode,
            "models": models.returncode,
            "exec_help": exec_help.returncode,
            "app_server_schema": schema_exit_code,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-state", type=Path)
    args = parser.parse_args()
    try:
        result = probe(args.run_state)
    except ManifestError as exc:
        print(f"capability probe: {exc}", file=sys.stderr)
        return 2
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0 if all(code == 0 for code in result["exit_codes"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
