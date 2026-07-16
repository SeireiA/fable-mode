#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fable_runner.executor import CodexExecutor, parse_jsonl, terminate_process_tree
from fable_runner.models import Manifest, TaskSpec
from fable_runner.scheduler import Scheduler, cancel_run, initial_state
from fable_runner.state import RunStore


class InjectedRunnerCrash(RuntimeError):
    pass


class CrashBeforeAttemptExecutor(CodexExecutor):
    def __init__(self, command: tuple[str, ...], crash_on_call: int) -> None:
        super().__init__(command)
        self.crash_on_call = crash_on_call
        self.invoke_count = 0

    def _invoke(self, *args, **kwargs):
        self.invoke_count += 1
        if self.invoke_count == self.crash_on_call:
            raise InjectedRunnerCrash("simulated runner exit")
        return super()._invoke(*args, **kwargs)


def make_fake_codex(root: Path) -> Path:
    script = root / "fake_codex.py"
    script.write_text(
        """import json, os, pathlib, sys, time
if os.environ.get('FAKE_SLEEP'):
    time.sleep(float(os.environ['FAKE_SLEEP']))
rate_counter = os.environ.get('FAKE_RATE_COUNTER')
if rate_counter:
    counter_path = pathlib.Path(rate_counter)
    count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
    counter_path.write_text(str(count))
    if count < 3:
        print('rate limit: retry later', file=sys.stderr)
        raise SystemExit(1)
if os.environ.get('FAKE_MALFORMED'):
    print('not-json')
    raise SystemExit(0)
if os.environ.get('FAKE_FAILURE_STDERR'):
    print(os.environ['FAKE_FAILURE_STDERR'], file=sys.stderr)
    raise SystemExit(int(os.environ.get('FAKE_EXIT_CODE', '1')))
args = sys.argv[1:]
args_log = os.environ.get('FAKE_ARGS_LOG')
if args_log:
    with pathlib.Path(args_log).open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(args) + '\\n')
model = args[args.index('-m') + 1]
log = os.environ.get('FAKE_MODEL_LOG')
if log:
    with pathlib.Path(log).open('a', encoding='utf-8') as handle:
        handle.write(model + '\\n')
secret_output = os.environ.get('FAKE_SECRET_OUTPUT')
if secret_output:
    print(json.dumps({'type': 'item.completed', 'item': {'text': secret_output}}))
    print(secret_output, file=sys.stderr)
print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-test'}))
print(json.dumps({'type': 'turn.completed', 'model': model}))
if os.environ.get('FAKE_EXIT_CODE'):
    raise SystemExit(int(os.environ['FAKE_EXIT_CODE']))
""",
        encoding="utf-8",
    )
    return script


def fixture(root: Path, acceptance: tuple[str, ...], timeout: int = 10):
    prompt = root / "prompt.md"
    prompt.write_text("Make the requested change.", encoding="utf-8")
    task = TaskSpec(
        id="card",
        role="worker",
        prompt_file=prompt,
        workspace=root,
        worktree=root,
        depends_on=(),
        acceptance_argv=acceptance,
    )
    manifest_path = root / "workflow.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest = Manifest(
        path=manifest_path,
        repo_root=root,
        models={
            "lead": "gpt-5.6-sol",
            "fast": "gpt-5.6-terra",
            "economy": "gpt-5.4-mini",
        },
        timeout_seconds=timeout,
        routing="frugal",
        tier="conservative",
        max_workers=5,
        tasks=(task,),
    )
    store = RunStore(root / ".fable" / "runs" / "test-run")
    store.create(initial_state(manifest, "test-run"), "{}")
    return manifest, task, store


class ExecutorTests(unittest.TestCase):
    def test_successful_worker_runs_external_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            acceptance = (sys.executable, "-c", "raise SystemExit(0)")
            manifest, task, store = fixture(root, acceptance)
            outcome = CodexExecutor((sys.executable, str(fake))).run_task(
                manifest, task, "gpt-5.6-terra", store
            )
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.attempts, 1)
            self.assertEqual(outcome.thread_id, "thread-test")
            self.assertEqual(
                json.loads(
                    (
                        store.run_dir
                        / "tasks"
                        / "card"
                        / "acceptance-1.json"
                    ).read_text(encoding="utf-8")
                ),
                {"status": "passed", "exit_code": 0},
            )
            self.assertEqual(
                store.read()["tasks"]["card"]["acceptance_exit_code"], 0
            )

    def test_two_failures_escalate_once_to_lead_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            counter = root / "counter.txt"
            acceptance_code = (
                "from pathlib import Path; import sys; "
                f"p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; "
                "p.write_text(str(n)); sys.exit(0 if n >= 3 else 1)"
            )
            manifest, task, store = fixture(
                root, (sys.executable, "-c", acceptance_code)
            )
            model_log = root / "models.txt"
            old = os.environ.get("FAKE_MODEL_LOG")
            os.environ["FAKE_MODEL_LOG"] = str(model_log)
            try:
                outcome = CodexExecutor((sys.executable, str(fake))).run_task(
                    manifest, task, "gpt-5.6-terra", store
                )
            finally:
                if old is None:
                    os.environ.pop("FAKE_MODEL_LOG", None)
                else:
                    os.environ["FAKE_MODEL_LOG"] = old
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.attempts, 3)
            self.assertEqual(
                model_log.read_text(encoding="utf-8").splitlines(),
                ["gpt-5.6-terra", "gpt-5.6-terra", "gpt-5.6-sol"],
            )

    def test_first_failure_persists_retry_stage_before_next_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(1)")
            )
            executor = CrashBeforeAttemptExecutor(
                (sys.executable, str(fake)), crash_on_call=2
            )
            with self.assertRaises(InjectedRunnerCrash):
                executor.run_task(manifest, task, "gpt-5.6-terra", store)
            task_state = store.read()["tasks"]["card"]
            self.assertEqual(task_state["attempts"], 2)
            self.assertEqual(task_state["model"], "gpt-5.6-terra")
            self.assertEqual(task_state["recovery_stage"], "same_model_retry")
            self.assertEqual(task_state["acceptance_status"], "failed")

    def test_second_failure_persists_lead_stage_before_next_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(1)")
            )
            executor = CrashBeforeAttemptExecutor(
                (sys.executable, str(fake)), crash_on_call=3
            )
            with self.assertRaises(InjectedRunnerCrash):
                executor.run_task(manifest, task, "gpt-5.6-terra", store)
            task_state = store.read()["tasks"]["card"]
            self.assertEqual(task_state["attempts"], 3)
            self.assertEqual(task_state["model"], "gpt-5.6-sol")
            self.assertEqual(task_state["recovery_stage"], "lead_retry")
            self.assertEqual(task_state["acceptance_status"], "failed")

    def test_malformed_jsonl_is_an_explicit_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(0)")
            )
            os.environ["FAKE_MALFORMED"] = "1"
            try:
                outcome = CodexExecutor((sys.executable, str(fake))).run_task(
                    manifest, task, "gpt-5.6-terra", store
                )
            finally:
                os.environ.pop("FAKE_MALFORMED", None)
            self.assertEqual(outcome.status, "failed")
            self.assertIn("invalid Codex JSONL", outcome.error or "")

    def test_authentication_failure_is_classified_without_persisting_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(0)")
            )
            secret = "opaque-authentication-detail"
            os.environ["FAKE_FAILURE_STDERR"] = (
                f"HTTP 401 unauthorized: {secret}"
            )
            try:
                outcome = CodexExecutor(
                    (sys.executable, str(fake))
                ).run_task(manifest, task, "gpt-5.6-terra", store)
            finally:
                os.environ.pop("FAKE_FAILURE_STDERR", None)
            self.assertEqual(outcome.error, "Codex authentication failed")
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in store.run_dir.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, persisted)

    def test_model_entitlement_failure_is_classified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(0)")
            )
            os.environ["FAKE_FAILURE_STDERR"] = (
                "model gpt-example is not available for this account"
            )
            try:
                outcome = CodexExecutor(
                    (sys.executable, str(fake))
                ).run_task(manifest, task, "gpt-5.6-terra", store)
            finally:
                os.environ.pop("FAKE_FAILURE_STDERR", None)
            self.assertEqual(
                outcome.error,
                "Codex model is unavailable or not permitted for this account",
            )

    def test_jsonl_requires_object_records(self):
        with self.assertRaisesRegex(ValueError, "record"):
            parse_jsonl("[]\n")

    def test_codex_attempt_persists_metadata_without_raw_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(0)")
            )
            secret = "opaque-credential-that-has-no-label"
            os.environ["FAKE_SECRET_OUTPUT"] = secret
            try:
                outcome = CodexExecutor(
                    (sys.executable, str(fake))
                ).run_task(manifest, task, "gpt-5.6-terra", store)
            finally:
                os.environ.pop("FAKE_SECRET_OUTPUT", None)
            self.assertEqual(outcome.status, "completed")
            log = store.run_dir / "tasks" / "card" / "attempt-1.json"
            self.assertEqual(
                json.loads(log.read_text(encoding="utf-8")),
                {
                    "status": "completed",
                    "exit_code": 0,
                    "event_types": [
                        "item.completed",
                        "thread.started",
                        "turn.completed",
                    ],
                    "thread_id": "thread-test",
                    "requested_model": "gpt-5.6-terra",
                    "actual_model": "gpt-5.6-terra",
                },
            )
            task_state = store.read()["tasks"]["card"]
            self.assertEqual(task_state["attempt_logs"], ["tasks/card/attempt-1.json"])
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in store.run_dir.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, persisted)

    def test_codex_failure_stderr_is_not_persisted_in_run_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, _task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(0)")
            )
            secret = "opaque-failure-credential"
            os.environ["FAKE_SECRET_OUTPUT"] = secret
            os.environ["FAKE_EXIT_CODE"] = "1"
            try:
                state = Scheduler(
                    manifest,
                    CodexExecutor((sys.executable, str(fake))),
                    store,
                ).run()
            finally:
                os.environ.pop("FAKE_SECRET_OUTPUT", None)
                os.environ.pop("FAKE_EXIT_CODE", None)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(
                state["tasks"]["card"]["error"],
                "Codex worker exited with 1",
            )
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in store.run_dir.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, persisted)

    def test_acceptance_secret_is_not_copied_into_run_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            secret = "sensitive-token-value"
            acceptance = (
                sys.executable,
                "-c",
                f"print('token={secret}'); raise SystemExit(1)",
            )
            manifest, task, store = fixture(root, acceptance)
            outcome = CodexExecutor((sys.executable, str(fake))).run_task(
                manifest, task, "gpt-5.6-terra", store
            )
            self.assertEqual(outcome.status, "failed")
            self.assertNotIn(
                secret, store.path.read_text(encoding="utf-8")
            )
            log = store.run_dir / "tasks" / "card" / "acceptance-3.json"
            self.assertEqual(
                json.loads(log.read_text(encoding="utf-8")),
                {"status": "failed", "exit_code": 1},
            )
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in store.run_dir.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, persisted)

    def test_interrupted_task_resumes_saved_thread(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(0)")
            )
            store.update_task(
                "card",
                status="pending",
                attempts=1,
                thread_id="saved-thread",
                model="gpt-5.6-terra",
            )
            args_log = root / "args.txt"
            os.environ["FAKE_ARGS_LOG"] = str(args_log)
            try:
                outcome = CodexExecutor((sys.executable, str(fake))).run_task(
                    manifest, task, "gpt-5.6-terra", store
                )
            finally:
                os.environ.pop("FAKE_ARGS_LOG", None)
            self.assertEqual(outcome.status, "completed")
            invocation = json.loads(args_log.read_text(encoding="utf-8"))
            self.assertIn("resume", invocation)
            self.assertIn("saved-thread", invocation)

    def test_rate_limit_retries_are_bounded_and_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(0)")
            )
            counter = root / "rate-count.txt"
            os.environ["FAKE_RATE_COUNTER"] = str(counter)
            try:
                with mock.patch("fable_runner.executor.time.sleep"):
                    outcome = CodexExecutor(
                        (sys.executable, str(fake))
                    ).run_task(manifest, task, "gpt-5.6-terra", store)
            finally:
                os.environ.pop("FAKE_RATE_COUNTER", None)
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(counter.read_text(encoding="utf-8"), "3")
            logs = store.run_dir / "tasks" / "card"
            expected_logs = [
                "tasks/card/attempt-1.json",
                "tasks/card/attempt-1-retry-1.json",
                "tasks/card/attempt-1-retry-2.json",
            ]
            self.assertEqual(
                store.read()["tasks"]["card"]["attempt_logs"], expected_logs
            )
            for relative_path in expected_logs:
                metadata = json.loads(
                    (store.run_dir / relative_path).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    set(metadata),
                    {
                        "status",
                        "exit_code",
                        "event_types",
                        "thread_id",
                        "requested_model",
                        "actual_model",
                    },
                )

    def test_worker_timeout_is_an_explicit_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root,
                (sys.executable, "-c", "raise SystemExit(0)"),
                timeout=1,
            )
            os.environ["FAKE_SLEEP"] = "5"
            try:
                outcome = CodexExecutor(
                    (sys.executable, str(fake))
                ).run_task(manifest, task, "gpt-5.6-terra", store)
            finally:
                os.environ.pop("FAKE_SLEEP", None)
            self.assertEqual(outcome.status, "failed")
            self.assertIn("timed out", outcome.error or "")

    def test_cancel_before_spawn_starts_no_codex_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            manifest, task, store = fixture(
                root, (sys.executable, "-c", "raise SystemExit(0)")
            )
            args_log = root / "args.txt"
            store.request_cancel()
            os.environ["FAKE_ARGS_LOG"] = str(args_log)
            try:
                outcome = CodexExecutor(
                    (sys.executable, str(fake))
                ).run_task(manifest, task, "gpt-5.6-terra", store)
            finally:
                os.environ.pop("FAKE_ARGS_LOG", None)
            self.assertEqual(outcome.status, "cancelled")
            self.assertFalse(args_log.exists())

    def test_pid_identity_mismatch_prevents_process_tree_kill(self):
        with mock.patch(
            "fable_runner.executor.process_identity", return_value="new-process"
        ), mock.patch("fable_runner.executor.subprocess.run") as taskkill, mock.patch(
            "fable_runner.executor.os.killpg", create=True
        ) as killpg:
            terminated = terminate_process_tree(12345, "old-process")
        self.assertFalse(terminated)
        taskkill.assert_not_called()
        killpg.assert_not_called()

    def test_posix_process_tree_kill_escalates_to_sigkill(self):
        with mock.patch("fable_runner.executor.os.name", "posix"), mock.patch(
            "fable_runner.executor.os.killpg", create=True
        ) as killpg, mock.patch(
            "fable_runner.executor._process_group_exists",
            return_value=True,
            create=True,
        ), mock.patch(
            "fable_runner.executor.time.monotonic", side_effect=[0.0, 1.0]
        ):
            terminated = terminate_process_tree(24680)
        self.assertTrue(terminated)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(24680, signal.SIGTERM),
                mock.call(24680, 9),
            ],
        )

    @unittest.skipIf(os.name == "nt", "requires POSIX process groups")
    def test_posix_process_tree_kill_stops_term_ignoring_descendant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid_path = root / "child-pid"
            program = (
                "import pathlib, signal, subprocess, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "child=subprocess.Popen([sys.executable, '-c', "
                "'import signal,time; signal.signal(signal.SIGTERM, "
                "signal.SIG_IGN); time.sleep(30)']); "
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
                "time.sleep(30)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", program],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while not child_pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            terminate_process_tree(process.pid)
            process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("TERM-ignoring descendant survived process-group cleanup")

    def test_cancel_terminates_running_acceptance_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = make_fake_codex(root)
            acceptance_started = root / "acceptance-started"
            acceptance_code = (
                "from pathlib import Path; import time; "
                f"Path({str(acceptance_started)!r}).write_text('yes'); "
                "time.sleep(20)"
            )
            manifest, task, store = fixture(
                root, (sys.executable, "-c", acceptance_code), timeout=30
            )
            outcomes = []

            def execute():
                outcomes.append(
                    CodexExecutor((sys.executable, str(fake))).run_task(
                        manifest, task, "gpt-5.6-terra", store
                    )
                )

            worker = threading.Thread(target=execute)
            worker.start()
            deadline = time.monotonic() + 5
            while not acceptance_started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(acceptance_started.exists())
            cancel_run(store)
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0].status, "cancelled")


if __name__ == "__main__":
    unittest.main()
