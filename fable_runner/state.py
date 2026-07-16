"""Atomic, thread-safe local run state."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable


TERMINAL_TASK_STATES = frozenset({
    "completed", "failed", "skipped", "cancelled",
})
REPLACE_RETRY_DELAYS = (0.01, 0.05, 0.1, 0.25, 0.5)


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            path.chmod(0o600)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            descriptor = -1
            handle.write(value)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class RunStore:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir.resolve()
        self.path = self.run_dir / "run.json"
        self.cancel_path = self.run_dir / ".cancel-requested"
        self.process_lock_path = self.run_dir / ".state-lock"
        self.runner_lock_path = self.run_dir / ".runner-lock"
        self._lock = threading.RLock()

    def create(self, state: dict[str, Any], manifest_text: str) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = self.run_dir / "manifest.json"
        _write_private_text(manifest_path, manifest_text)
        self.write(state)

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def write(self, state: dict[str, Any]) -> None:
        with self._lock:
            with self._process_lock():
                self._write_unlocked(state)

    def update(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            with self._process_lock():
                state = self._read_unlocked()
                previous_run_status = state.get("status")
                previous_task_statuses = {
                    task_id: task.get("status")
                    for task_id, task in state.get("tasks", {}).items()
                }
                callback(state)
                self._record_status_changes(
                    state,
                    previous_run_status,
                    previous_task_statuses,
                )
                self._write_unlocked(state)
                return deepcopy(state)

    @staticmethod
    def _record_status_changes(
        state: dict[str, Any],
        previous_run_status: Any,
        previous_task_statuses: dict[str, Any],
    ) -> None:
        transitions = []
        current_run_status = state.get("status")
        if current_run_status != previous_run_status:
            transitions.append({
                "scope": "run",
                "from": previous_run_status,
                "to": current_run_status,
            })
        for task_id, task in state.get("tasks", {}).items():
            previous_status = previous_task_statuses.get(task_id)
            current_status = task.get("status")
            if current_status != previous_status:
                transitions.append({
                    "scope": "task",
                    "task_id": task_id,
                    "from": previous_status,
                    "to": current_status,
                })
        if not transitions:
            return
        changed_at = datetime.now(timezone.utc).isoformat()
        events = state.setdefault("events", [])
        for transition in transitions:
            transition["at"] = changed_at
            events.append(transition)

    def _read_unlocked(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.",
            suffix=".tmp",
            dir=self.run_dir,
        )
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                descriptor = -1
                handle.write(
                    json.dumps(state, ensure_ascii=False, indent=2) + "\n"
                )
            for retry, delay in enumerate((0.0, *REPLACE_RETRY_DELAYS)):
                if delay:
                    time.sleep(delay)
                try:
                    os.replace(temporary, self.path)
                    return
                except PermissionError:
                    if retry == len(REPLACE_RETRY_DELAYS):
                        raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        with self._file_lock(self.process_lock_path, blocking=True):
            yield

    @contextmanager
    def _file_lock(self, path: Path, *, blocking: bool) -> Iterator[None]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(handle.fileno(), mode, 1)
                except OSError as exc:
                    raise RuntimeError("run is already active") from exc
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                operation = fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(handle.fileno(), operation)
                except BlockingIOError as exc:
                    raise RuntimeError("run is already active") from exc
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def runner_lease(self) -> Iterator[None]:
        with self._file_lock(self.runner_lock_path, blocking=False):
            yield

    def update_task(self, task_id: str, **changes: Any) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> None:
            task = state["tasks"][task_id]
            if task["status"] == "cancelled" and changes.get("status") != "cancelled":
                return
            task.update(changes)

        return self.update(apply)

    def request_cancel(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_path.write_text("cancel requested\n", encoding="ascii")

    def cancel_requested(self) -> bool:
        return self.cancel_path.is_file()


def find_run_dir(repo_root: Path, run_id: str) -> Path:
    safe_characters = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    )
    if not run_id or any(char not in safe_characters for char in run_id):
        raise ValueError("invalid run id")
    run_dir = (repo_root / ".fable" / "runs" / run_id).resolve()
    expected_parent = (repo_root / ".fable" / "runs").resolve()
    if run_dir.parent != expected_parent:
        raise ValueError("invalid run id")
    return run_dir
