#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fable_runner.models import (
    ManifestError,
    load_manifest,
    validate_models,
    worker_limit,
)


MODELS = {
    "lead": "gpt-5.6-sol",
    "fast": "gpt-5.6-terra",
    "economy": "gpt-5.4-mini",
}


class ManifestFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        subprocess.run(
            ["git", "init", "-q", str(root)],
            capture_output=True,
            check=True,
        )
        (root / ".fable" / "tasks").mkdir(parents=True)
        (root / ".fable" / "LEDGER.md").write_text(
            "ROUTING: balanced\nTIER: conservative\n"
            "- [ ] 1. card -- acceptance: test\n",
            encoding="utf-8",
        )
        (root / ".fable" / "tasks" / "one.md").write_text(
            "Inspect the project.", encoding="utf-8"
        )

    def write(self, tasks: list[dict] | None = None, **changes) -> Path:
        data = {
            "schema_version": 1,
            "models": MODELS,
            "timeout_seconds": 30,
            "tasks": tasks or [{
                "id": "one",
                "role": "explorer",
                "prompt_file": ".fable/tasks/one.md",
                "workspace": ".",
                "depends_on": [],
                "acceptance_argv": [sys.executable, "-c", "raise SystemExit(0)"],
            }],
        }
        data.update(changes)
        path = self.root / ".fable" / "workflow.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_uses_ledger_policy_and_routes_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ManifestFixture(Path(temporary))
            manifest = load_manifest(fixture.write())
            self.assertEqual(manifest.routing, "balanced")
            self.assertEqual(manifest.tier, "conservative")
            self.assertEqual(manifest.max_workers, 5)
            self.assertEqual(manifest.model_for("explorer"), MODELS["fast"])
            self.assertEqual(manifest.tasks[0].worktree, Path(temporary).resolve())

    def test_preflight_rejects_no_open_ledger_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ManifestFixture(Path(temporary))
            (fixture.root / ".fable" / "LEDGER.md").write_text(
                "- [x] done -- evidence: unit tests passed\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ManifestError, "no open task cards"):
                load_manifest(fixture.write())

    def test_saved_manifest_can_resume_after_ledger_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ManifestFixture(Path(temporary))
            path = fixture.write()
            (fixture.root / ".fable" / "LEDGER.md").write_text(
                "- [x] done -- evidence: acceptance passed\n",
                encoding="utf-8",
            )
            manifest = load_manifest(
                path,
                require_open_ledger=False,
                routing_override="frugal",
                tier_override="throughput",
            )
            self.assertEqual(manifest.routing, "frugal")
            self.assertEqual(manifest.tier, "throughput")

    def test_rejects_dependency_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ManifestFixture(Path(temporary))
            base = {
                "role": "explorer",
                "prompt_file": ".fable/tasks/one.md",
                "workspace": ".",
                "acceptance_argv": ["test"],
            }
            tasks = [
                {**base, "id": "one", "depends_on": ["two"]},
                {**base, "id": "two", "depends_on": ["one"]},
            ]
            with self.assertRaisesRegex(ManifestError, "cycle"):
                load_manifest(fixture.write(tasks))

    def test_rejects_string_acceptance_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ManifestFixture(Path(temporary))
            task = {
                "id": "one",
                "role": "worker",
                "prompt_file": ".fable/tasks/one.md",
                "workspace": ".",
                "depends_on": [],
                "acceptance_argv": "dangerous shell command",
            }
            with self.assertRaisesRegex(ManifestError, "argv array"):
                load_manifest(fixture.write([task]))

    def test_rejects_prompt_outside_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ManifestFixture(Path(temporary) / "repo")
            outside = fixture.root.parent / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            task = {
                "id": "one",
                "role": "explorer",
                "prompt_file": "../outside.md",
                "workspace": ".",
                "depends_on": [],
                "acceptance_argv": ["test"],
            }
            with self.assertRaisesRegex(ManifestError, "outside"):
                load_manifest(fixture.write([task]))

    def test_rejects_task_id_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ManifestFixture(Path(temporary))
            task = {
                "id": "../../escape",
                "role": "explorer",
                "prompt_file": ".fable/tasks/one.md",
                "workspace": ".",
                "depends_on": [],
                "acceptance_argv": ["test"],
            }
            with self.assertRaisesRegex(ManifestError, "ASCII slug"):
                load_manifest(fixture.write([task]))

    def test_throughput_limit_scales_with_available_cpu_and_cap(self):
        cases = (
            (1, 1),
            (4, 2),
            (128, 16),
            (None, 5),
        )
        for cpu_count, expected in cases:
            with self.subTest(cpu_count=cpu_count), mock.patch(
                "fable_runner.models.os.cpu_count", return_value=cpu_count
            ):
                self.assertEqual(worker_limit("throughput"), expected)

    def test_model_catalog_rejects_missing_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "catalog.py"
            script.write_text(
                "import json\nprint(json.dumps([{'slug': 'gpt-5.6-sol'}]))\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestError, "not in the Codex catalog"):
                validate_models((sys.executable, str(script)), MODELS)


if __name__ == "__main__":
    unittest.main()
