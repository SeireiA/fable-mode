#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fable_runner.executor import TaskOutcome
from fable_runner.models import Manifest, TaskSpec
from fable_runner.scheduler import Scheduler, cancel_run, initial_state
from fable_runner.state import RunStore


class CompletingExecutor:
    def run_task(self, manifest, task, model, store):
        return TaskOutcome("completed", model, 1, thread_id="resumed-thread")


def fixture(root: Path):
    prompt = root / "prompt.md"
    prompt.write_text("resume", encoding="utf-8")
    task = TaskSpec("one", "explorer", prompt, root, root, (), ("test",))
    manifest_path = root / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest = Manifest(
        manifest_path,
        root,
        {"lead": "lead", "fast": "fast", "economy": "economy"},
        30,
        "balanced",
        "conservative",
        2,
        (task,),
    )
    store = RunStore(root / ".fable" / "runs" / "recovery")
    store.create(initial_state(manifest, "recovery"), "{}")
    return manifest, store


class RecoveryTests(unittest.TestCase):
    def test_resume_resets_interrupted_running_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, store = fixture(Path(temporary))
            store.update_task(
                "one",
                status="running",
                pid=12345,
                pid_identity="process-12345",
            )
            with mock.patch(
                "fable_runner.scheduler.terminate_process_tree"
            ) as terminate:
                state = Scheduler(
                    manifest, CompletingExecutor(), store
                ).run(resume=True)
            terminate.assert_called_once_with(12345, "process-12345")
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["tasks"]["one"]["thread_id"], "resumed-thread")

    def test_cancel_terminates_pid_and_marks_nonterminal_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            _manifest, store = fixture(Path(temporary))
            store.update_task(
                "one",
                status="running",
                pid=4321,
                pid_identity="process-4321",
            )
            with mock.patch("fable_runner.scheduler.terminate_process_tree") as terminate:
                state = cancel_run(store)
            terminate.assert_called_once_with(4321, "process-4321")
            self.assertTrue(store.cancel_requested())
            self.assertEqual(state["status"], "cancelled")
            self.assertEqual(state["tasks"]["one"]["status"], "cancelled")

    def test_resume_does_not_kill_pid_without_saved_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, store = fixture(Path(temporary))
            store.update_task("one", status="running", pid=12345)
            with mock.patch(
                "fable_runner.scheduler.terminate_process_tree"
            ) as terminate:
                state = Scheduler(
                    manifest, CompletingExecutor(), store
                ).run(resume=True)
            terminate.assert_not_called()
            self.assertEqual(state["status"], "completed")

    def test_scheduler_honors_cross_process_cancel_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, store = fixture(Path(temporary))
            store.request_cancel()
            state = Scheduler(manifest, CompletingExecutor(), store).run()
            self.assertEqual(state["status"], "cancelled")
            self.assertEqual(state["tasks"]["one"]["status"], "cancelled")

    def test_atomic_update_preserves_valid_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            _manifest, store = fixture(Path(temporary))
            store.update_task("one", error="recorded")
            state = store.read()
            self.assertEqual(state["tasks"]["one"]["error"], "recorded")
            self.assertEqual(list(store.run_dir.glob("*.tmp")), [])

    def test_run_store_records_status_transitions_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            _manifest, store = fixture(Path(temporary))
            store.update(lambda state: state.update(status="running"))
            store.update_task("one", status="running")
            store.update_task("one", model="lead")
            store.update_task("one", status="completed")
            events = store.read()["events"]
            self.assertEqual(
                [
                    (
                        event["scope"],
                        event.get("task_id"),
                        event["from"],
                        event["to"],
                    )
                    for event in events
                ],
                [
                    ("run", None, "pending", "running"),
                    ("task", "one", "pending", "running"),
                    ("task", "one", "running", "completed"),
                ],
            )
            self.assertTrue(all(event.get("at") for event in events))

    def test_atomic_replace_retries_transient_windows_file_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            _manifest, store = fixture(Path(temporary))
            real_replace = __import__("os").replace
            calls = 0

            def flaky_replace(source, destination):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise PermissionError("injected file lock")
                return real_replace(source, destination)

            with mock.patch("fable_runner.state.os.replace", flaky_replace), mock.patch(
                "fable_runner.state.time.sleep"
            ) as sleep:
                store.update_task("one", error="after retry")
            self.assertEqual(calls, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(
                store.read()["tasks"]["one"]["error"], "after retry"
            )

    def test_process_lock_prevents_cross_instance_lost_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            _manifest, first = fixture(Path(temporary))
            second = RunStore(first.run_dir)
            entered = threading.Event()
            release = threading.Event()

            def slow_update(state):
                state["tasks"]["one"]["error"] = "first update"
                entered.set()
                release.wait(timeout=3)

            first_thread = threading.Thread(target=lambda: first.update(slow_update))
            second_thread = threading.Thread(
                target=lambda: second.update_task("one", model="second update")
            )
            first_thread.start()
            self.assertTrue(entered.wait(timeout=1))
            second_thread.start()
            release.set()
            first_thread.join(timeout=3)
            second_thread.join(timeout=3)
            state = first.read()["tasks"]["one"]
            self.assertEqual(state["error"], "first update")
            self.assertEqual(state["model"], "second update")

    def test_runner_lease_rejects_second_scheduler(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest, store = fixture(Path(temporary))
            with store.runner_lease(), self.assertRaisesRegex(
                RuntimeError, "already active"
            ):
                Scheduler(manifest, CompletingExecutor(), store).run()


if __name__ == "__main__":
    unittest.main()
