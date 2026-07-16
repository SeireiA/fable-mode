#!/usr/bin/env python3
"""CLI tests for the read-only Codex capability probe."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_codex_capabilities.py"


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
scenario = os.environ.get("FAKE_CODEX_SCENARIO", "success")

if args == ["--version"]:
    print("codex-cli 0.144.3")
elif args == ["features", "list"]:
    print("hooks        stable        true")
    print("multi_agent  experimental  false")
elif args == ["debug", "models", "--bundled"]:
    if scenario == "command_failure":
        print("model query failed", file=sys.stderr)
        raise SystemExit(7)
    elif scenario == "malformed_json":
        print("{not-json")
    elif scenario == "invalid_structure":
        print(json.dumps({"models": {"lead": "lead-model"}}))
    else:
        print(json.dumps([{"slug": "lead-model"}, {"slug": "fast-model"}]))
elif args == ["exec", "--help"]:
    print("--json --model --sandbox --disable")
elif args[:4] == [
    "app-server", "generate-json-schema", "--experimental", "--out"
]:
    output = pathlib.Path(args[4])
    output.mkdir(parents=True, exist_ok=True)
    (output / "protocol.json").write_text(json.dumps({
        "hookEvents": [
            "SessionStart", "PreToolUse", "PostToolUse", "Stop",
            "SubagentStart", "SubagentStop"
        ],
        "tools": ["spawn_agent"]
    }))
else:
    raise SystemExit(3)
'''


def run_probe(
    scenario: str = "success",
    *probe_args: str,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temporary:
        fake_codex = Path(temporary) / "fake_codex.py"
        fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
        env = os.environ.copy()
        env["FABLE_CODEX_COMMAND"] = json.dumps(
            [sys.executable, str(fake_codex)]
        )
        env["FAKE_CODEX_SCENARIO"] = scenario
        return subprocess.run(
            [sys.executable, str(PROBE), *probe_args],
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )


class CapabilityProbeTests(unittest.TestCase):
    def test_success_reports_codex_capabilities_as_json(self) -> None:
        result = run_probe()

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["version"], "codex-cli 0.144.3")
        self.assertTrue(report["features"]["hooks"]["enabled"])
        self.assertFalse(report["features"]["multi_agent"]["enabled"])
        self.assertEqual(report["models"], ["lead-model", "fast-model"])
        self.assertEqual(
            report["codex_exec"],
            {
                "json": True,
                "model": True,
                "sandbox": True,
                "disable_feature": True,
            },
        )
        self.assertEqual(
            report["runtime_schema"]["hook_events"],
            [
                "SessionStart",
                "PreToolUse",
                "PostToolUse",
                "Stop",
                "SubagentStart",
                "SubagentStop",
            ],
        )
        self.assertEqual(report["runtime_schema"]["agent_types"], [])
        self.assertFalse(
            report["runtime_schema"]["spawn_agents_on_csv_exposed"]
        )
        self.assertIsNone(report["runtime_observations"]["actual_models"])
        self.assertIsNone(report["runtime_observations"]["peak_concurrency"])
        self.assertIn(
            "live",
            report["runtime_observations"]["reason"].lower(),
        )
        self.assertEqual(report["exit_codes"]["app_server_schema"], 0)

    def test_command_failure_is_reported_with_nonzero_exit(self) -> None:
        result = run_probe("command_failure")

        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["exit_codes"]["models"], 7)
        self.assertEqual(report["models"], [])
        self.assertTrue(report["features"]["hooks"]["enabled"])

    def test_malformed_model_json_fails_explicitly(self) -> None:
        result = run_probe("malformed_json")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("model catalog is not valid JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_model_catalog_structure_fails_explicitly(self) -> None:
        result = run_probe("invalid_structure")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("model catalog has an invalid structure", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_run_state_reports_actual_models_and_peak_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            task_dir = run_dir / "tasks" / "first"
            task_dir.mkdir(parents=True)
            attempt_log = task_dir / "attempt-1.json"
            attempt_log.write_text(
                json.dumps({
                    "status": "completed",
                    "exit_code": 0,
                    "event_types": ["thread.started", "turn.completed"],
                    "thread_id": "thread-first",
                    "requested_model": "fast-model",
                    "actual_model": "fast-model",
                }),
                encoding="utf-8",
            )
            (run_dir / "manifest.json").write_text(
                json.dumps({
                    "models": {
                        "lead": "lead-model",
                        "fast": "fast-model",
                        "economy": "economy-model",
                    }
                }),
                encoding="utf-8",
            )
            run_state = run_dir / "run.json"
            run_state.write_text(
                json.dumps({
                    "tasks": {
                        "first": {
                            "model": "lead-model",
                            "attempt_logs": ["tasks/first/attempt-1.json"],
                        },
                        "second": {
                            "model": "lead-model",
                            "attempt_logs": [],
                        },
                    },
                    "events": [
                        {
                            "scope": "task",
                            "task_id": "first",
                            "from": "pending",
                            "to": "running",
                        },
                        {
                            "scope": "task",
                            "task_id": "second",
                            "from": "pending",
                            "to": "running",
                        },
                        {
                            "scope": "task",
                            "task_id": "first",
                            "from": "running",
                            "to": "completed",
                        },
                        {
                            "scope": "task",
                            "task_id": "second",
                            "from": "running",
                            "to": "completed",
                        },
                    ],
                }),
                encoding="utf-8",
            )
            result = run_probe("success", "--run-state", str(run_state))
        self.assertEqual(result.returncode, 0, result.stderr)
        observations = json.loads(result.stdout)["runtime_observations"]
        self.assertEqual(observations["actual_models"], ["fast-model"])
        self.assertEqual(
            observations["requested_models"],
            ["fast-model", "lead-model"],
        )
        self.assertEqual(observations["peak_concurrency"], 2)
        self.assertEqual(observations["source"], str(run_state.resolve()))


if __name__ == "__main__":
    unittest.main()
