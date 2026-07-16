"""Manifest validation, ledger policy, and model routing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from hooks._fable_common import parse_ledger, read_routing, read_tier


SCHEMA_VERSION = 1
ROLES = frozenset({"explorer", "worker", "verifier"})
MODEL_KEYS = frozenset({"lead", "fast", "economy"})
ROUTING_MATRIX = {
    "quality": {"explorer": "lead", "worker": "lead", "verifier": "lead"},
    "balanced": {"explorer": "fast", "worker": "lead", "verifier": "lead"},
    "frugal": {"explorer": "economy", "worker": "fast", "verifier": "lead"},
}
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_TIMEOUT_SECONDS = 86_400
CONSERVATIVE_WORKERS = 5
THROUGHPUT_WORKER_CAP = 16
TASK_ID_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9_-])?\Z"
)


class ManifestError(ValueError):
    """Raised when a workflow cannot pass strict preflight validation."""


@dataclass(frozen=True)
class TaskSpec:
    id: str
    role: str
    prompt_file: Path
    workspace: Path
    worktree: Path
    depends_on: tuple[str, ...]
    acceptance_argv: tuple[str, ...]

    @property
    def is_writer(self) -> bool:
        return self.role == "worker"


@dataclass(frozen=True)
class Manifest:
    path: Path
    repo_root: Path
    models: dict[str, str]
    timeout_seconds: int
    routing: str
    tier: str
    max_workers: int
    tasks: tuple[TaskSpec, ...]

    def model_for(self, role: str) -> str:
        model_key = ROUTING_MATRIX[self.routing][role]
        return self.models[model_key]


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be a non-empty string")
    return value.strip()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ManifestError(f"no Git repository found from {start}")


def discover_worktrees(repo_root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise ManifestError(f"cannot list Git worktrees: {result.stderr.strip()}")
    roots = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            roots.append(Path(line[9:]).resolve())
    if not roots:
        raise ManifestError("Git reported no worktrees")
    return tuple(roots)


def _containing_worktree(path: Path, roots: tuple[Path, ...]) -> Path:
    matches = [root for root in roots if _inside(path, root)]
    if not matches:
        raise ManifestError(f"workspace is not inside an existing Git worktree: {path}")
    return max(matches, key=lambda root: len(root.parts))


def _validate_dependencies(tasks: list[TaskSpec]) -> None:
    ids = {task.id for task in tasks}
    if len(ids) != len(tasks):
        raise ManifestError("task ids must be unique")
    for task in tasks:
        unknown = set(task.depends_on) - ids
        if unknown:
            raise ManifestError(
                f"task {task.id} has unknown dependencies: {', '.join(sorted(unknown))}"
            )
        if task.id in task.depends_on:
            raise ManifestError(f"task {task.id} cannot depend on itself")

    visiting: set[str] = set()
    visited: set[str] = set()
    dependencies = {task.id: task.depends_on for task in tasks}

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ManifestError("task dependency graph contains a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)


def _load_models(value: Any) -> dict[str, str]:
    raw = _require_dict(value, "models")
    if set(raw) != MODEL_KEYS:
        required = ", ".join(sorted(MODEL_KEYS))
        raise ManifestError(f"models must contain exactly: {required}")
    return {key: _require_string(raw[key], f"models.{key}") for key in MODEL_KEYS}


def _load_task(
    raw_value: Any,
    repo_root: Path,
    worktrees: tuple[Path, ...],
) -> TaskSpec:
    raw = _require_dict(raw_value, "task")
    task_id = _require_string(raw.get("id"), "task.id")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ManifestError(
            "task.id must be a 1-64 character ASCII slug containing only "
            "letters, digits, dots, underscores, and hyphens"
        )
    role = _require_string(raw.get("role"), f"task {task_id}.role").lower()
    if role not in ROLES:
        raise ManifestError(f"task {task_id}.role must be explorer, worker, or verifier")

    prompt_value = _require_string(raw.get("prompt_file"), f"task {task_id}.prompt_file")
    prompt_file = (repo_root / prompt_value).resolve()
    if not _inside(prompt_file, repo_root) or not prompt_file.is_file():
        raise ManifestError(f"task {task_id} prompt_file is missing or outside the repository")

    workspace_value = _require_string(raw.get("workspace"), f"task {task_id}.workspace")
    workspace = (repo_root / workspace_value).resolve()
    if not workspace.is_dir():
        raise ManifestError(f"task {task_id} workspace does not exist: {workspace}")
    worktree = _containing_worktree(workspace, worktrees)

    dependencies_value = raw.get("depends_on", [])
    if not isinstance(dependencies_value, list) or not all(
        isinstance(item, str) and item.strip() for item in dependencies_value
    ):
        raise ManifestError(f"task {task_id}.depends_on must be an array of task ids")

    acceptance_value = raw.get("acceptance_argv")
    if not isinstance(acceptance_value, list) or not acceptance_value or not all(
        isinstance(item, str) and item for item in acceptance_value
    ):
        raise ManifestError(f"task {task_id}.acceptance_argv must be a non-empty argv array")

    return TaskSpec(
        id=task_id,
        role=role,
        prompt_file=prompt_file,
        workspace=workspace,
        worktree=worktree,
        depends_on=tuple(item.strip() for item in dependencies_value),
        acceptance_argv=tuple(acceptance_value),
    )


def worker_limit(tier: str) -> int:
    if tier == "conservative":
        return CONSERVATIVE_WORKERS
    cpu_count = os.cpu_count() or CONSERVATIVE_WORKERS + 2
    return max(1, min(THROUGHPUT_WORKER_CAP, cpu_count - 2))


def load_manifest(
    path: Path,
    *,
    require_open_ledger: bool = True,
    routing_override: str | None = None,
    tier_override: str | None = None,
) -> Manifest:
    manifest_path = path.resolve()
    repo_root = find_repo_root(manifest_path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    root = _require_dict(raw, "manifest")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"schema_version must be {SCHEMA_VERSION}")

    timeout = root.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    valid_timeout = (
        isinstance(timeout, int)
        and not isinstance(timeout, bool)
        and 0 < timeout <= MAX_TIMEOUT_SECONDS
    )
    if not valid_timeout:
        raise ManifestError(
            f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}"
        )

    ledger = repo_root / ".fable" / "LEDGER.md"
    open_items, _has_any, paused = parse_ledger(str(ledger))
    if require_open_ledger and paused:
        raise ManifestError("the fable ledger is paused")
    if require_open_ledger and not open_items:
        raise ManifestError("the fable ledger has no open task cards")
    routing = routing_override or read_routing(str(ledger)) or "balanced"
    tier = tier_override or read_tier(str(ledger)) or "conservative"
    if routing not in ROUTING_MATRIX:
        raise ManifestError(f"unsupported routing profile: {routing}")
    if tier not in {"conservative", "throughput"}:
        raise ManifestError(f"unsupported concurrency tier: {tier}")

    worktrees = discover_worktrees(repo_root)
    tasks_value = root.get("tasks")
    if not isinstance(tasks_value, list) or not tasks_value:
        raise ManifestError("tasks must be a non-empty array")
    tasks = [_load_task(value, repo_root, worktrees) for value in tasks_value]
    _validate_dependencies(tasks)

    return Manifest(
        path=manifest_path,
        repo_root=repo_root,
        models=_load_models(root.get("models")),
        timeout_seconds=timeout,
        routing=routing,
        tier=tier,
        max_workers=worker_limit(tier),
        tasks=tuple(tasks),
    )


def codex_command() -> tuple[str, ...]:
    configured_command = os.environ.get("FABLE_CODEX_COMMAND")
    if configured_command:
        try:
            values = json.loads(configured_command)
        except json.JSONDecodeError as exc:
            raise ManifestError("FABLE_CODEX_COMMAND must be a JSON argv array") from exc
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ManifestError("FABLE_CODEX_COMMAND must be a non-empty JSON argv array")
        return tuple(values)
    configured = os.environ.get("FABLE_CODEX_BIN")
    if configured:
        path = shutil.which(configured) or configured
        return (path,)
    candidates = ("codex.cmd", "codex") if os.name == "nt" else ("codex",)
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return (path,)
    raise ManifestError("Codex CLI is not installed or not on PATH")


def validate_models(command: tuple[str, ...], models: dict[str, str]) -> None:
    result = subprocess.run(
        [*command, "debug", "models", "--bundled"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise ManifestError(f"cannot read Codex model catalog: {result.stderr.strip()}")
    try:
        catalog = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ManifestError("Codex model catalog is not valid JSON") from exc
    entries = catalog if isinstance(catalog, list) else catalog.get("models", [])
    available = {
        entry.get("slug") for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
    }
    missing = sorted(set(models.values()) - available)
    if missing:
        raise ManifestError(f"models are not in the Codex catalog: {', '.join(missing)}")
