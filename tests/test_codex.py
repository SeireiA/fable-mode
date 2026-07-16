#!/usr/bin/env python3
"""Codex-specific hook protocol and installer tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install_codex as installer_module  # noqa: E402

HOOKS = ROOT / "hooks"
INSTALLER = ROOT / "install_codex.py"


def run_hook(script: str, payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload), text=True, encoding="utf-8",
        capture_output=True,
        cwd=cwd, env=env, check=False,
    )


class CodexHookTests(unittest.TestCase):
    def test_subagent_start_warns_without_open_card(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".fable").mkdir()
            result = run_hook("fable_spawn_guard.py", {
                "hook_event_name": "SubagentStart",
                "session_id": "codex-test",
                "cwd": str(root),
                "model": "gpt-5",
            }, root)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(
                output["hookSpecificOutput"]["hookEventName"],
                "SubagentStart",
            )
            self.assertIn("DESIGN GATE WARNING",
                          output["hookSpecificOutput"]["additionalContext"])

    def test_subagent_start_is_quiet_with_open_card(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fable = root / ".fable"
            fable.mkdir()
            (fable / "LEDGER.md").write_text(
                "- [ ] card -- acceptance: pytest\n", encoding="utf-8")
            result = run_hook("fable_spawn_guard.py", {
                "hook_event_name": "SubagentStart",
                "session_id": "codex-test",
                "cwd": str(root),
            }, root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_codex_metadata_exit_code_triggers_third_failure_reminder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".fable").mkdir()
            sid = "codex-test-" + uuid.uuid4().hex
            payload = {
                "hook_event_name": "PostToolUse",
                "session_id": sid,
                "cwd": str(root),
                "tool_name": "Bash",
                "tool_response": {"metadata": {"exit_code": 1}},
            }
            results = [run_hook("fable_fail_streak.py", payload, root)
                       for _ in range(3)]
            self.assertEqual([r.returncode for r in results], [0, 0, 0])
            self.assertEqual(results[0].stdout, "")
            self.assertEqual(results[1].stdout, "")
            output = json.loads(results[2].stdout)
            self.assertEqual(
                output["hookSpecificOutput"]["hookEventName"],
                "PostToolUse",
            )


class CodexInstallerTests(unittest.TestCase):
    def test_install_is_valid_preserving_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text(
                "model = \"gpt-5\"\n\n"
                "[features]\n"
                "goals = true\n\n"
                "[[hooks.PreToolUse]]\n"
                "matcher = \"^Bash$\"\n",
                encoding="utf-8",
            )
            command = [
                sys.executable, str(INSTALLER),
                "--config", str(config),
                "--skill-dir", str(ROOT),
            ]
            first = subprocess.run(command, text=True, encoding="utf-8",
                                   capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertFalse(
                (config.parent / "fable-strict.config.toml").exists()
            )
            installed = config.read_text(encoding="utf-8")
            data = tomllib.loads(installed)
            self.assertTrue(data["features"]["hooks"])
            self.assertEqual(data["hooks"]["PreToolUse"][0]["matcher"],
                             "^Bash$")
            self.assertEqual(len(data["hooks"]["SessionStart"]), 1)
            self.assertIn("command_windows",
                          data["hooks"]["SessionStart"][0]["hooks"][0])
            self.assertTrue(config.with_name(
                "config.toml.fable-mode.bak").exists())

            second = subprocess.run(command, text=True, encoding="utf-8",
                                    capture_output=True, check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"), installed)

            removed = subprocess.run(command + ["--uninstall"], text=True,
                                     encoding="utf-8", capture_output=True,
                                     check=False)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            after = config.read_text(encoding="utf-8")
            self.assertNotIn("BEGIN fable-mode", after)
            after_data = tomllib.loads(after)
            self.assertNotIn("hooks", after_data["features"])
            self.assertEqual(after_data["hooks"]["PreToolUse"][0]
                             ["matcher"], "^Bash$")

    def test_uninstall_restores_disabled_hooks_feature(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.toml"
            config.write_text("[features]\nhooks = false\n", encoding="utf-8")
            command = [
                sys.executable, str(INSTALLER), "--config", str(config),
                "--skill-dir", str(ROOT),
            ]
            installed = subprocess.run(
                command, text=True, encoding="utf-8", capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(tomllib.loads(
                config.read_text(encoding="utf-8"))["features"]["hooks"])

            removed = subprocess.run(
                command + ["--uninstall"], text=True, encoding="utf-8",
                capture_output=True, check=False,
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(tomllib.loads(
                config.read_text(encoding="utf-8"))["features"]["hooks"])

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_backup_inherits_current_source_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.toml"
            config.write_text('model = "gpt-5"\n', encoding="utf-8")
            config.chmod(0o640)
            command = [
                sys.executable, str(INSTALLER),
                "--config", str(config),
                "--skill-dir", str(ROOT),
            ]

            installed = subprocess.run(
                command, text=True, encoding="utf-8", capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            backup = config.with_name("config.toml.fable-mode.bak")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o640)

            config.chmod(0o600)
            removed = subprocess.run(
                command + ["--uninstall"], text=True, encoding="utf-8",
                capture_output=True, check=False,
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_explicit_strict_runner_profile_is_valid_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            codex_home = Path(td) / ".codex"
            config = codex_home / "config.toml"
            command = [
                sys.executable, str(INSTALLER),
                "--config", str(config),
                "--skill-dir", str(ROOT),
                "--with-strict-runner",
            ]

            first = subprocess.run(
                command, text=True, encoding="utf-8", capture_output=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            profile = codex_home / "fable-strict.config.toml"
            installed = profile.read_text(encoding="utf-8")
            profile_data = tomllib.loads(installed)
            self.assertFalse(profile_data["features"]["multi_agent"])
            python_path = profile_data["shell_environment_policy"]["set"][
                "PYTHONPATH"
            ]
            self.assertEqual(
                Path(python_path.split(os.pathsep)[0]).resolve(), ROOT.resolve()
            )
            self.assertIn("codex -p fable-strict", first.stdout)

            second = subprocess.run(
                command, text=True, encoding="utf-8", capture_output=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(profile.read_text(encoding="utf-8"), installed)

            removed = subprocess.run(
                command[:-1] + ["--uninstall"], text=True,
                encoding="utf-8", capture_output=True, check=False,
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(profile.exists())
            backup = profile.with_name(
                "fable-strict.config.toml.fable-mode.bak"
            )
            self.assertEqual(backup.read_text(encoding="utf-8"), installed)

    def test_strict_runner_updates_managed_profile_with_backup(self):
        with tempfile.TemporaryDirectory() as td:
            codex_home = Path(td) / ".codex"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            profile = codex_home / "fable-strict.config.toml"
            original = (
                "# fable-mode managed strict runner profile\n"
                "[features]\n"
                "multi_agent = true\n"
            )
            profile.write_text(original, encoding="utf-8")
            command = [
                sys.executable, str(INSTALLER),
                "--config", str(config),
                "--skill-dir", str(ROOT),
                "--with-strict-runner",
            ]

            result = subprocess.run(
                command, text=True, encoding="utf-8", capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(tomllib.loads(
                profile.read_text(encoding="utf-8")
            )["features"]["multi_agent"])
            backup = profile.with_name(
                "fable-strict.config.toml.fable-mode.bak"
            )
            self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_strict_runner_does_not_overwrite_unmanaged_profile(self):
        with tempfile.TemporaryDirectory() as td:
            codex_home = Path(td) / ".codex"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            original_config = 'model = "gpt-5"\n'
            config.write_text(original_config, encoding="utf-8")
            profile = codex_home / "fable-strict.config.toml"
            original_profile = "[features]\nmulti_agent = true\n"
            profile.write_text(original_profile, encoding="utf-8")
            command = [
                sys.executable, str(INSTALLER),
                "--config", str(config),
                "--skill-dir", str(ROOT),
                "--with-strict-runner",
            ]

            install = subprocess.run(
                command, text=True, encoding="utf-8", capture_output=True,
                check=False,
            )
            self.assertNotEqual(install.returncode, 0)
            self.assertIn("not managed by fable-mode", install.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"),
                             original_config)
            self.assertEqual(profile.read_text(encoding="utf-8"),
                             original_profile)

            uninstall = subprocess.run(
                command[:-1] + ["--uninstall"], text=True,
                encoding="utf-8", capture_output=True, check=False,
            )
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
            self.assertEqual(profile.read_text(encoding="utf-8"),
                             original_profile)

    def test_strict_profile_failure_rolls_back_main_config(self):
        with tempfile.TemporaryDirectory() as td:
            codex_home = Path(td) / ".codex"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            original = 'model = "gpt-5"\n'
            config.write_text(original, encoding="utf-8")
            with mock.patch.object(
                installer_module,
                "install_strict_profile",
                side_effect=OSError("injected profile write failure"),
            ), self.assertRaisesRegex(OSError, "injected"):
                installer_module.apply_installation(
                    config,
                    ROOT,
                    uninstall=False,
                    with_strict_runner=True,
                )
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertFalse(
                (codex_home / "fable-strict.config.toml").exists()
            )


if __name__ == "__main__":
    unittest.main()
