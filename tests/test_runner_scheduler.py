#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fable_runner.executor import TaskOutcome
from fable_runner.models import Manifest, TaskSpec
from fable_runner.scheduler import Scheduler, initial_state
from fable_runner.state import RunStore


class FakeExecutor:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def run_task(self, manifest, task, model, store):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1
        if task.id in self.failures:
            return TaskOutcome("failed", model, 1, error="injected failure")
        return TaskOutcome("completed", model, 1, thread_id=f"thread-{task.id}")


class RaisingExecutor:
    def run_task(self, manifest, task, model, store):
        raise RuntimeError("opaque-exception-credential")


def make_task(root: Path, task_id: str, role: str, depends_on=()) -> TaskSpec:
    prompt = root / f"{task_id}.md"
    prompt.write_text(task_id, encoding="utf-8")
    return TaskSpec(
        id=task_id,
        role=role,
        prompt_file=prompt,
        workspace=root,
        worktree=root,
        depends_on=tuple(depends_on),
        acceptance_argv=("test",),
    )


def make_manifest(root: Path, tasks: tuple[TaskSpec, ...]) -> Manifest:
    path = root / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    return Manifest(
        path=path,
        repo_root=root,
        models={"lead": "lead", "fast": "fast", "economy": "economy"},
        timeout_seconds=30,
        routing="balanced",
        tier="conservative",
        max_workers=4,
        tasks=tasks,
    )


def make_store(root: Path, manifest: Manifest) -> RunStore:
    store = RunStore(root / ".fable" / "runs" / "scheduler")
    store.create(initial_state(manifest, "scheduler"), "{}")
    return store


class SchedulerTests(unittest.TestCase):
    def test_readers_run_in_parallel_in_same_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = tuple(make_task(root, f"r{i}", "explorer") for i in range(3))
            manifest = make_manifest(root, tasks)
            executor = FakeExecutor()
            state = Scheduler(manifest, executor, make_store(root, manifest)).run()
            self.assertEqual(state["status"], "completed")
            self.assertGreaterEqual(executor.peak, 2)

    def test_writers_in_same_worktree_are_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = tuple(make_task(root, f"w{i}", "worker") for i in range(3))
            manifest = make_manifest(root, tasks)
            executor = FakeExecutor()
            Scheduler(manifest, executor, make_store(root, manifest)).run()
            self.assertEqual(executor.peak, 1)

    def test_failed_dependency_skips_downstream_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = (
                make_task(root, "first", "explorer"),
                make_task(root, "second", "explorer", ("first",)),
            )
            manifest = make_manifest(root, tasks)
            state = Scheduler(
                manifest,
                FakeExecutor({"first"}),
                make_store(root, manifest),
            ).run()
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["tasks"]["first"]["status"], "failed")
            self.assertEqual(state["tasks"]["second"]["status"], "skipped")

    def test_failure_propagates_through_multiple_dependency_levels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = (
                make_task(root, "first", "explorer"),
                make_task(root, "second", "explorer", ("first",)),
                make_task(root, "third", "explorer", ("second",)),
            )
            manifest = make_manifest(root, tasks)
            state = Scheduler(
                manifest,
                FakeExecutor({"first"}),
                make_store(root, manifest),
            ).run()
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["tasks"]["second"]["status"], "skipped")
            self.assertEqual(state["tasks"]["third"]["status"], "skipped")

    def test_executor_exception_text_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = (make_task(root, "first", "explorer"),)
            manifest = make_manifest(root, tasks)
            store = make_store(root, manifest)
            state = Scheduler(manifest, RaisingExecutor(), store).run()
            self.assertEqual(state["status"], "failed")
            self.assertEqual(
                state["tasks"]["first"]["error"],
                "runner error: RuntimeError",
            )
            self.assertNotIn(
                "opaque-exception-credential",
                store.path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
