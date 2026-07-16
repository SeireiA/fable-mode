"""Bounded dependency scheduling with persistent recovery state."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any
import uuid

from .executor import (
    TaskExecutor,
    TaskOutcome,
    terminate_process_tree,
)
from .models import Manifest, TaskSpec
from .state import RunStore, TERMINAL_TASK_STATES


FAILURE_STATES = frozenset({"failed", "skipped", "cancelled"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:10]}"


def initial_state(manifest: Manifest, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "pending",
        "manifest_path": str(manifest.path),
        "repo_root": str(manifest.repo_root),
        "routing": manifest.routing,
        "tier": manifest.tier,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "events": [],
        "tasks": {
            task.id: {
                "status": "pending",
                "role": task.role,
                "model": manifest.model_for(task.role),
                "attempts": 0,
                "thread_id": None,
                "pid": None,
                "pid_identity": None,
                "exit_code": None,
                "attempt_logs": [],
                "error": None,
                "acceptance_exit_code": None,
                "acceptance_status": None,
                "acceptance_log": None,
                "recovery_stage": "initial",
            }
            for task in manifest.tasks
        },
    }


class Scheduler:
    def __init__(
        self,
        manifest: Manifest,
        executor: TaskExecutor,
        store: RunStore,
    ) -> None:
        self.manifest = manifest
        self.executor = executor
        self.store = store
        self.tasks = {task.id: task for task in manifest.tasks}
        self._active_readers: dict[Path, int] = {}
        self._active_writers: set[Path] = set()
        self._access_lock = threading.Lock()

    def _can_start(self, task: TaskSpec) -> bool:
        with self._access_lock:
            worktree = task.worktree
            if task.is_writer:
                return (
                    worktree not in self._active_writers
                    and not self._active_readers.get(worktree)
                )
            return worktree not in self._active_writers

    def _reserve(self, task: TaskSpec) -> None:
        with self._access_lock:
            if task.is_writer:
                self._active_writers.add(task.worktree)
            else:
                readers = self._active_readers.get(task.worktree, 0)
                self._active_readers[task.worktree] = readers + 1

    def _release(self, task: TaskSpec) -> None:
        with self._access_lock:
            if task.is_writer:
                self._active_writers.discard(task.worktree)
            else:
                remaining = self._active_readers.get(task.worktree, 0) - 1
                if remaining > 0:
                    self._active_readers[task.worktree] = remaining
                else:
                    self._active_readers.pop(task.worktree, None)

    def _run_one(self, task: TaskSpec) -> TaskOutcome:
        try:
            return self.executor.run_task(
                self.manifest,
                task,
                self.manifest.model_for(task.role),
                self.store,
            )
        finally:
            self._release(task)

    def _honor_cancel_request(self) -> None:
        state = self.store.read()
        for task_id, task_state in state["tasks"].items():
            pid = task_state.get("pid")
            identity = task_state.get("pid_identity")
            if (
                isinstance(pid, int)
                and pid > 0
                and isinstance(identity, str)
            ):
                terminate_process_tree(pid, identity)
            if task_state["status"] not in TERMINAL_TASK_STATES:
                self.store.update_task(
                    task_id,
                    status="cancelled",
                    pid=None,
                    pid_identity=None,
                    error="cancelled by user",
                )

    def run(self, resume: bool = False) -> dict[str, Any]:
        with self.store.runner_lease():
            return self._run(resume)

    def _propagate_dependency_failures(self) -> None:
        while True:
            state = self.store.read()
            task_states = state["tasks"]
            to_skip = []
            for task in self.tasks.values():
                if task_states[task.id]["status"] != "pending":
                    continue
                dependency_states = (
                    task_states[item]["status"] for item in task.depends_on
                )
                if any(status in FAILURE_STATES for status in dependency_states):
                    to_skip.append(task.id)
            if not to_skip:
                return
            for task_id in to_skip:
                self.store.update_task(
                    task_id,
                    status="skipped",
                    error="a dependency did not complete successfully",
                )

    def _run(self, resume: bool = False) -> dict[str, Any]:
        if resume:
            interrupted = self.store.read()
            for task_state in interrupted["tasks"].values():
                pid = task_state.get("pid")
                identity = task_state.get("pid_identity")
                if (
                    task_state["status"] == "running"
                    and isinstance(pid, int)
                    and isinstance(identity, str)
                ):
                    terminate_process_tree(pid, identity)

            def reset_interrupted(state: dict[str, Any]) -> None:
                for task_state in state["tasks"].values():
                    if task_state["status"] == "running":
                        task_state.update(
                            status="pending",
                            pid=None,
                            pid_identity=None,
                            error=None,
                        )
                state["status"] = "running"
                state["updated_at"] = utc_now()
            self.store.update(reset_interrupted)
        else:
            self.store.update(
                lambda state: state.update(
                    status="running", updated_at=utc_now()
                )
            )

        running: dict[Future[TaskOutcome], TaskSpec] = {}
        with ThreadPoolExecutor(max_workers=self.manifest.max_workers) as pool:
            while True:
                if self.store.cancel_requested():
                    self._honor_cancel_request()

                self._propagate_dependency_failures()
                state = self.store.read()
                task_states = state["tasks"]
                pending = [
                    self.tasks[task_id]
                    for task_id, value in task_states.items()
                    if value["status"] == "pending"
                ]

                capacity = self.manifest.max_workers - len(running)
                if capacity > 0:
                    for task in pending:
                        dependencies_done = all(
                            task_states[item]["status"] == "completed"
                            for item in task.depends_on
                        )
                        if not dependencies_done or not self._can_start(task):
                            continue
                        self._reserve(task)
                        self.store.update_task(
                            task.id,
                            status="running",
                            started_at=utc_now(),
                        )
                        future = pool.submit(self._run_one, task)
                        running[future] = task
                        capacity -= 1
                        if capacity == 0:
                            break

                if not running:
                    current = self.store.read()
                    if all(
                        value["status"] in TERMINAL_TASK_STATES
                        for value in current["tasks"].values()
                    ):
                        break
                    has_pending = any(
                        value["status"] == "pending"
                        for value in current["tasks"].values()
                    )
                    if not has_pending:
                        break
                    raise RuntimeError("scheduler deadlock: pending tasks cannot be started")

                done, _not_done = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    task = running.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = TaskOutcome(
                            "failed",
                            self.manifest.model_for(task.role),
                            0,
                            error=f"runner error: {type(exc).__name__}",
                        )
                    self.store.update_task(
                        task.id,
                        status=outcome.status,
                        model=outcome.model,
                        attempts=outcome.attempts,
                        thread_id=outcome.thread_id,
                        error=outcome.error,
                        finished_at=utc_now(),
                        pid=None,
                        pid_identity=None,
                    )

        def finish(state: dict[str, Any]) -> None:
            failed = any(
                task["status"] in FAILURE_STATES
                for task in state["tasks"].values()
            )
            if self.store.cancel_requested():
                final_status = "cancelled"
            else:
                final_status = "failed" if failed else "completed"
            state.update(
                status=final_status,
                updated_at=utc_now(),
                finished_at=utc_now(),
            )

        return self.store.update(finish)


def cancel_run(store: RunStore) -> dict[str, Any]:
    store.request_cancel()
    processes: list[tuple[int, str]] = []

    def mark_cancelled(state: dict[str, Any]) -> None:
        for task_state in state["tasks"].values():
            pid = task_state.get("pid")
            identity = task_state.get("pid_identity")
            if (
                isinstance(pid, int)
                and pid > 0
                and isinstance(identity, str)
            ):
                processes.append((pid, identity))
            if task_state["status"] not in TERMINAL_TASK_STATES:
                task_state.update(
                    status="cancelled",
                    pid=None,
                    pid_identity=None,
                    error="cancelled by user",
                )
        state.update(
            status="cancelled",
            updated_at=utc_now(),
            finished_at=utc_now(),
        )

    state = store.update(mark_cancelled)
    for pid, identity in processes:
        terminate_process_tree(pid, identity)
    return state


def render_state(state: dict[str, Any]) -> str:
    lines = [f"run {state['run_id']}: {state['status']}"]
    for task_id, task in state["tasks"].items():
        suffix = f" - {task['error']}" if task.get("error") else ""
        lines.append(f"  {task_id}: {task['status']} ({task['model']}){suffix}")
    return "\n".join(lines)
