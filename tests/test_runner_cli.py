#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4-mini"]


class RunnerCliTests(unittest.TestCase):
    def test_two_task_fake_codex_run_completes_and_persists_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".fable" / "tasks").mkdir(parents=True)
            (repo / ".fable" / "LEDGER.md").write_text(
                "ROUTING: balanced\nTIER: conservative\n"
                "- [ ] 1. first -- acceptance: python\n"
                "- [ ] 2. second -- acceptance: python\n",
                encoding="utf-8",
            )
            for task_id in ("first", "second"):
                (repo / ".fable" / "tasks" / f"{task_id}.md").write_text(
                    f"Inspect {task_id}.", encoding="utf-8"
                )
            manifest = {
                "schema_version": 1,
                "models": {
                    "lead": MODELS[0],
                    "fast": MODELS[1],
                    "economy": MODELS[2],
                },
                "timeout_seconds": 30,
                "tasks": [
                    {
                        "id": task_id,
                        "role": "explorer",
                        "prompt_file": f".fable/tasks/{task_id}.md",
                        "workspace": ".",
                        "depends_on": [],
                        "acceptance_argv": [
                            sys.executable,
                            "-c",
                            "raise SystemExit(0)",
                        ],
                    }
                    for task_id in ("first", "second")
                ],
            }
            manifest_path = repo / ".fable" / "workflow.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            fake_codex = repo / "fake_codex.py"
            fake_codex.write_text(
                """import json, sys, time
args = sys.argv[1:]
if args[:3] == ['debug', 'models', '--bundled']:
    print(json.dumps([{'slug': item} for item in %r]))
else:
    time.sleep(0.05)
    card = 'unknown'
    print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-' + card}))
    print(json.dumps({'type': 'turn.completed'}))
""" % MODELS,
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["FABLE_CODEX_COMMAND"] = json.dumps(
                [sys.executable, str(fake_codex)]
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "fable_runner",
                    "run",
                    "--manifest",
                    str(manifest_path),
                ],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout.splitlines()[0], r"run .+: starting")
            self.assertIn("first: completed", result.stdout)
            self.assertIn("second: completed", result.stdout)
            run_files = list((repo / ".fable" / "runs").glob("*/run.json"))
            self.assertEqual(len(run_files), 1)
            state = json.loads(run_files[0].read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "completed")


if __name__ == "__main__":
    unittest.main()
