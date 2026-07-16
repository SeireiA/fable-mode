"""Codex subprocess execution and external acceptance verification."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Protocol

from .models import Manifest, TaskSpec
from .state import RunStore


MAX_FAILURE_CONTEXT = 8_000
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_DELAYS_SECONDS = (1, 2)
PROCESS_POLL_SECONDS = 0.2
PROCESS_TERMINATION_GRACE_SECONDS = 0.5
PROCESS_TERMINATION_POLL_SECONDS = 0.05
PERSISTED_EVENT_TYPES = frozenset({
    "error",
    "item.completed",
    "item.started",
    "item.updated",
    "thread.started",
    "turn.completed",
    "turn.failed",
    "turn.started",
})


@dataclass(frozen=True)
class TaskOutcome:
    status: str
    model: str
    attempts: int
    thread_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    records: tuple[dict[str, Any], ...]
    thread_id: str | None
    stderr: str
    error: str | None = None


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    status: str
    output: str
    exit_code: int | None
    log_path: str | None


class TaskExecutor(Protocol):
    def run_task(
        self,
        manifest: Manifest,
        task: TaskSpec,
        model: str,
        store: RunStore,
    ) -> TaskOutcome: ...


def write_private_text(path: Path, value: str) -> None:
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


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    write_private_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def event_types(records: tuple[dict[str, Any], ...]) -> list[str]:
    result = []
    for record in records:
        candidate = record.get("type")
        if not isinstance(candidate, str) or candidate not in PERSISTED_EVENT_TYPES:
            candidate = "unknown"
        result.append(candidate)
    return result


def actual_model(
    records: tuple[dict[str, Any], ...],
    allowed_models: set[str],
) -> str | None:
    for record in records:
        if record.get("type") != "turn.completed":
            continue
        candidate = record.get("model")
        if isinstance(candidate, str) and candidate in allowed_models:
            return candidate
    return None


def _find_thread_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("thread_id", "threadId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for candidate in value.values():
            found = _find_thread_id(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_thread_id(candidate)
            if found:
                return found
    return None


def parse_jsonl(output: str) -> tuple[tuple[dict[str, Any], ...], str | None]:
    records: list[dict[str, Any]] = []
    thread_id = None
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Codex JSONL at line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"invalid Codex JSONL record at line {line_number}")
        records.append(record)
        thread_id = thread_id or _find_thread_id(record)
    if not records:
        raise ValueError("Codex produced no JSONL records")
    return tuple(records), thread_id


def _windows_process_identity(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return None
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
    finally:
        kernel32.CloseHandle(handle)
    created_at = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return f"windows:{created_at}"


def process_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_identity(pid)

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw_stat = stat_path.read_text(encoding="ascii")
    except OSError:
        raw_stat = ""
    if raw_stat:
        command_end = raw_stat.rfind(")")
        fields = raw_stat[command_end + 2:].split() if command_end >= 0 else []
        if len(fields) > 19:
            return f"linux:{fields[19]}"

    result = subprocess.run(
        ["ps", "-o", "lstart=", "-o", "comm=", "-p", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    value = " ".join(result.stdout.split())
    return f"posix:{value}" if result.returncode == 0 and value else None


def _process_group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_tree(
    pid: int,
    expected_identity: str | None = None,
) -> bool:
    if pid <= 0:
        return False
    if (
        expected_identity is not None
        and process_identity(pid) != expected_identity
    ):
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _process_group_exists(pid):
            return True
        time.sleep(PROCESS_TERMINATION_POLL_SECONDS)
    if not _process_group_exists(pid):
        return True
    try:
        os.killpg(pid, getattr(signal, "SIGKILL", 9))
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _stop_process(process: subprocess.Popen[str]) -> None:
    terminate_process_tree(process.pid)
    if process.poll() is None:
        process.kill()


def _communicate_with_control(
    process: subprocess.Popen[str],
    input_text: str | None,
    timeout_seconds: int,
    store: RunStore,
) -> tuple[str, str, str]:
    deadline = time.monotonic() + timeout_seconds
    pending_input = input_text
    while True:
        if store.cancel_requested():
            _stop_process(process)
            stdout, stderr = process.communicate()
            return stdout, stderr, "cancelled"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(process)
            stdout, stderr = process.communicate()
            return stdout, stderr, "timeout"
        try:
            stdout, stderr = process.communicate(
                input=pending_input,
                timeout=min(PROCESS_POLL_SECONDS, remaining),
            )
            return stdout, stderr, "completed"
        except subprocess.TimeoutExpired:
            pending_input = None


def is_rate_limited(result: ProcessResult) -> bool:
    message = f"{result.stderr}\n{result.error or ''}".lower()
    markers = ("rate limit", "rate_limit", "too many requests", "http 429")
    return any(marker in message for marker in markers)


def classify_codex_failure(result: ProcessResult) -> str:
    message = f"{result.stderr}\n{result.error or ''}".lower()
    if is_rate_limited(result):
        return "Codex rate limit retries exhausted"
    authentication_markers = (
        "http 401",
        "unauthorized",
        "authentication failed",
        "not authenticated",
        "login required",
        "invalid api key",
    )
    if any(marker in message for marker in authentication_markers):
        return "Codex authentication failed"
    model_access_markers = (
        "does not have access",
        "model_not_found",
        "not available",
        "not permitted",
        "unsupported model",
    )
    if "model" in message and any(
        marker in message for marker in model_access_markers
    ):
        return "Codex model is unavailable or not permitted for this account"
    if "http 403" in message or "forbidden" in message:
        return "Codex model is unavailable or not permitted for this account"
    return f"Codex worker exited with {result.returncode}"


class CodexExecutor:
    def __init__(self, command: tuple[str, ...]) -> None:
        if not command:
            raise ValueError("Codex command cannot be empty")
        self.command = command

    def _invoke(
        self,
        manifest: Manifest,
        task: TaskSpec,
        model: str,
        prompt: str,
        store: RunStore,
        attempt: int,
        thread_id: str | None,
        process_retry: int = 0,
    ) -> ProcessResult:
        if store.cancel_requested():
            return ProcessResult(
                -1, (), thread_id, "", error="cancelled by user"
            )
        if thread_id:
            argv = [
                *self.command,
                "exec",
                "resume",
                "--json",
                "-m",
                model,
                "--disable",
                "multi_agent",
                thread_id,
                "-",
            ]
        else:
            sandbox = "workspace-write" if task.is_writer else "read-only"
            argv = [
                *self.command,
                "exec",
                "--json",
                "-m",
                model,
                "-s",
                sandbox,
                "-C",
                str(task.workspace),
                "--disable",
                "multi_agent",
                "-",
            ]

        env = os.environ.copy()
        env.update({
            "FABLE_ORCHESTRATOR_CHILD": "1",
            "FABLE_ORCHESTRATOR_ROLE": task.role,
            "FABLE_ORCHESTRATOR_CARD": task.id,
        })
        process = subprocess.Popen(
            argv,
            cwd=task.workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            **_process_group_kwargs(),
        )
        pid_identity = process_identity(process.pid)
        store.update_task(
            task.id,
            status="running",
            pid=process.pid,
            pid_identity=pid_identity,
            model=model,
            attempts=attempt,
        )
        try:
            stdout, stderr, completion = _communicate_with_control(
                process,
                prompt,
                manifest.timeout_seconds,
                store,
            )
            if completion == "timeout":
                return ProcessResult(
                    returncode=(
                        process.returncode
                        if process.returncode is not None
                        else -1
                    ),
                    records=(),
                    thread_id=thread_id,
                    stderr=stderr,
                    error=(
                        "Codex worker timed out after "
                        f"{manifest.timeout_seconds} seconds"
                    ),
                )
            if completion == "cancelled":
                return ProcessResult(
                    process.returncode,
                    (),
                    thread_id,
                    stderr,
                    error="cancelled by user",
                )
        finally:
            store.update_task(task.id, pid=None, pid_identity=None)

        if store.cancel_requested():
            return ProcessResult(
                process.returncode,
                records=(),
                thread_id=thread_id,
                stderr=stderr,
                error="cancelled by user",
            )

        try:
            records, parsed_thread_id = parse_jsonl(stdout)
        except ValueError as exc:
            records = ()
            parsed_thread_id = None
            parse_error = str(exc)
        else:
            parse_error = None

        resolved_thread_id = thread_id or parsed_thread_id
        task_dir = store.run_dir / "tasks" / task.id
        task_dir.mkdir(parents=True, exist_ok=True)
        retry_suffix = f"-retry-{process_retry}" if process_retry else ""
        log_path = task_dir / f"attempt-{attempt}{retry_suffix}.json"
        if process.returncode != 0:
            attempt_status = "failed"
        elif parse_error:
            attempt_status = "invalid_jsonl"
        else:
            attempt_status = "completed"
        write_private_json(
            log_path,
            {
                "status": attempt_status,
                "exit_code": process.returncode,
                "event_types": event_types(records),
                "thread_id": resolved_thread_id,
                "requested_model": model,
                "actual_model": actual_model(
                    records,
                    set(manifest.models.values()),
                ),
            },
        )
        relative_log_path = str(log_path.relative_to(store.run_dir)).replace(
            os.sep, "/"
        )

        def record_attempt(state: dict[str, Any]) -> None:
            state["tasks"][task.id].setdefault("attempt_logs", []).append(
                relative_log_path
            )

        store.update(record_attempt)
        if parse_error:
            return ProcessResult(
                returncode=process.returncode,
                records=(),
                thread_id=thread_id,
                stderr=stderr,
                error=parse_error,
            )
        return ProcessResult(
            returncode=process.returncode,
            records=records,
            thread_id=resolved_thread_id,
            stderr=stderr,
        )

    def _accept(
        self,
        manifest: Manifest,
        task: TaskSpec,
        store: RunStore,
        attempt: int,
    ) -> AcceptanceResult:
        if store.cancel_requested():
            return AcceptanceResult(
                False,
                "cancelled",
                "acceptance cancelled by user",
                None,
                None,
            )
        try:
            process = subprocess.Popen(
                list(task.acceptance_argv),
                cwd=task.workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                shell=False,
                **_process_group_kwargs(),
            )
            store.update_task(
                task.id,
                pid=process.pid,
                pid_identity=process_identity(process.pid),
            )
            try:
                stdout, stderr, completion = _communicate_with_control(
                    process,
                    None,
                    manifest.timeout_seconds,
                    store,
                )
                if completion == "timeout":
                    output = (
                        "acceptance timed out after "
                        f"{manifest.timeout_seconds} seconds"
                    )
                    passed = False
                    exit_code = process.returncode
                    acceptance_status = "timeout"
                elif completion == "cancelled":
                    output = "acceptance cancelled by user"
                    passed = False
                    exit_code = process.returncode
                    acceptance_status = "cancelled"
                else:
                    output = (
                        f"exit_code={process.returncode}\n"
                        f"stdout:\n{stdout}\n"
                        f"stderr:\n{stderr}"
                    )
                    passed = process.returncode == 0
                    exit_code = process.returncode
                    acceptance_status = "passed" if passed else "failed"
            finally:
                store.update_task(task.id, pid=None, pid_identity=None)
        except OSError as exc:
            output = f"acceptance could not run: {exc}"
            passed = False
            exit_code = None
            acceptance_status = "spawn_error"
        task_dir = store.run_dir / "tasks" / task.id
        task_dir.mkdir(parents=True, exist_ok=True)
        log_path = task_dir / f"acceptance-{attempt}.json"
        write_private_json(
            log_path,
            {
                "status": acceptance_status,
                "exit_code": exit_code,
            },
        )
        relative_log_path = str(log_path.relative_to(store.run_dir)).replace(
            os.sep, "/"
        )
        return AcceptanceResult(
            passed,
            acceptance_status,
            output,
            exit_code,
            relative_log_path,
        )

    def run_task(
        self,
        manifest: Manifest,
        task: TaskSpec,
        model: str,
        store: RunStore,
    ) -> TaskOutcome:
        original_prompt = task.prompt_file.read_text(encoding="utf-8")
        prompt = (
            f"Fable strict-runner card {task.id}; role={task.role}.\n"
            "Native multi-agent delegation is disabled. Work only on this card. "
            "The runner will execute the acceptance command externally.\n\n"
            f"{original_prompt}"
        )
        saved_task = store.read()["tasks"][task.id]
        current_model = saved_task.get("model") or model
        lead_model = manifest.models["lead"]
        thread_id = saved_task.get("thread_id")
        saved_attempts = saved_task.get("attempts")
        attempt = max(1, saved_attempts) if isinstance(saved_attempts, int) else 1
        if (
            thread_id
            and saved_task.get("acceptance_status") == "passed"
            and saved_task.get("recovery_stage") == "completed"
        ):
            return TaskOutcome("completed", current_model, attempt, thread_id)
        if thread_id:
            prompt = (
                "The strict runner restarted after an interruption. Continue this card "
                "from the existing thread state, then return for external acceptance."
            )
            acceptance_status = saved_task.get("acceptance_status")
            acceptance_exit_code = saved_task.get("acceptance_exit_code")
            if isinstance(acceptance_status, str):
                prompt += (
                    "\n\nLast recorded acceptance result: "
                    f"status={acceptance_status}, exit_code={acceptance_exit_code}."
                )

        while True:
            if store.cancel_requested():
                return TaskOutcome(
                    "cancelled",
                    current_model,
                    attempt,
                    thread_id,
                    "cancelled by user",
                )
            process_retry = 0
            while True:
                process_result = self._invoke(
                    manifest,
                    task,
                    current_model,
                    prompt,
                    store,
                    attempt,
                    thread_id,
                    process_retry,
                )
                if (
                    process_result.returncode != 0
                    and is_rate_limited(process_result)
                    and process_retry < RATE_LIMIT_RETRIES
                ):
                    if store.cancel_requested():
                        return TaskOutcome(
                            "cancelled",
                            current_model,
                            attempt,
                            thread_id,
                            "cancelled by user",
                        )
                    time.sleep(RATE_LIMIT_DELAYS_SECONDS[process_retry])
                    process_retry += 1
                    continue
                break
            store.update_task(task.id, exit_code=process_result.returncode)
            thread_id = process_result.thread_id
            store.update_task(task.id, thread_id=thread_id)
            if store.cancel_requested():
                return TaskOutcome(
                    "cancelled",
                    current_model,
                    attempt,
                    thread_id,
                    "cancelled by user",
                )
            if (
                process_result.error
                and process_result.error.startswith("Codex worker timed out")
            ):
                return TaskOutcome(
                    "failed", current_model, attempt, thread_id, process_result.error
                )
            if process_result.returncode != 0:
                return TaskOutcome(
                    "failed",
                    current_model,
                    attempt,
                    thread_id,
                    classify_codex_failure(process_result),
                )
            if process_result.error:
                return TaskOutcome(
                    "failed",
                    current_model,
                    attempt,
                    thread_id,
                    process_result.error,
                )
            if not thread_id:
                return TaskOutcome(
                    "failed", current_model, attempt, None, "Codex output had no thread id"
                )

            acceptance = self._accept(manifest, task, store, attempt)
            if store.cancel_requested():
                return TaskOutcome(
                    "cancelled",
                    current_model,
                    attempt,
                    thread_id,
                    "cancelled by user",
                )
            acceptance_changes = {
                "acceptance_exit_code": acceptance.exit_code,
                "acceptance_status": acceptance.status,
                "acceptance_log": acceptance.log_path,
            }
            if acceptance.passed:
                store.update_task(
                    task.id,
                    **acceptance_changes,
                    recovery_stage="completed",
                )
                return TaskOutcome("completed", current_model, attempt, thread_id)

            if attempt == 1:
                next_attempt = 2
                store.update_task(
                    task.id,
                    **acceptance_changes,
                    attempts=next_attempt,
                    model=current_model,
                    recovery_stage="same_model_retry",
                )
                prompt = (
                    "The external acceptance command failed. Diagnose and fix the card, "
                    "then return for another external verification.\n\n"
                    + acceptance.output[-MAX_FAILURE_CONTEXT:]
                )
                attempt = next_attempt
                continue
            if current_model != lead_model:
                next_attempt = attempt + 1
                store.update_task(
                    task.id,
                    **acceptance_changes,
                    attempts=next_attempt,
                    model=lead_model,
                    recovery_stage="lead_retry",
                )
                prompt = (
                    "Acceptance failed twice. You are the lead-model recovery pass. "
                    "Fix the root cause without expanding scope.\n\n"
                    + acceptance.output[-MAX_FAILURE_CONTEXT:]
                )
                current_model = lead_model
                attempt = next_attempt
                continue
            store.update_task(
                task.id,
                **acceptance_changes,
                recovery_stage="exhausted",
            )
            return TaskOutcome(
                "failed",
                current_model,
                attempt,
                thread_id,
                "acceptance failed after the allowed recovery attempts",
            )
