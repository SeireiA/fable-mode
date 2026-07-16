#!/usr/bin/env python3
"""Install fable-mode hooks into a shared Codex config.toml.

Codex CLI and the desktop app use the same CODEX_HOME. This installer adds an
idempotent, marked TOML block so upgrades and uninstall do not disturb other
hooks or settings.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib


BEGIN = "# BEGIN fable-mode Codex hooks"
END = "# END fable-mode Codex hooks"

HOOKS = (
    ("SessionStart", "startup|resume|clear|compact",
     "fable_profile_inject.py", "Loading fable-mode profile"),
    ("SubagentStart", None,
     "fable_spawn_guard.py", "Checking fable-mode delegation gate"),
    ("PostToolUse", "^Bash$",
     "fable_fail_streak.py", "Tracking fable-mode command failures"),
    ("Stop", None,
     "fable_close_guard.py", "Checking fable-mode ledger"),
)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def hook_block(skill_dir: Path) -> str:
    lines = [BEGIN]
    for event, matcher, filename, status in HOOKS:
        script = (skill_dir / "hooks" / filename).resolve()
        posix_script = script.as_posix()
        lines.extend(["", f"[[hooks.{event}]]"])
        if matcher is not None:
            lines.append(f"matcher = {toml_string(matcher)}")
        lines.extend([
            "",
            f"[[hooks.{event}.hooks]]",
            'type = "command"',
            f"command = {toml_string(f'python3 \"{posix_script}\"')}",
            f"command_windows = {toml_string(f'py -3 \"{script}\"')}",
            "timeout = 30",
            f"statusMessage = {toml_string(status)}",
        ])
    lines.extend(["", END, ""])
    return "\n".join(lines)


def remove_marked_block(text: str) -> str:
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(BEGIN)}\r?\n.*?^\s*{re.escape(END)}\s*\r?\n?"
    )
    return pattern.sub("", text).rstrip() + "\n"


def enable_hooks_feature(text: str) -> str:
    lines = text.splitlines()
    table_start = None
    for i, line in enumerate(lines):
        if line.strip() == "[features]":
            table_start = i
            break

    if table_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[features]", "hooks = true", ""])
        return "\n".join(lines).rstrip() + "\n"

    table_end = len(lines)
    for i in range(table_start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            table_end = i
            break

    hook_line = re.compile(r"^\s*hooks\s*=")
    for i in range(table_start + 1, table_end):
        if hook_line.match(lines[i]):
            indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = indent + "hooks = true"
            return "\n".join(lines).rstrip() + "\n"

    lines.insert(table_end, "hooks = true")
    return "\n".join(lines).rstrip() + "\n"


def validated(text: str, label: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{label} is not valid TOML: {exc}") from exc


def install(config: Path, skill_dir: Path, uninstall: bool) -> bool:
    config.parent.mkdir(parents=True, exist_ok=True)
    original = config.read_text(encoding="utf-8") if config.exists() else ""
    if original.strip():
        validated(original, str(config))

    updated = remove_marked_block(original)
    if not uninstall:
        updated = enable_hooks_feature(updated)
        updated = updated.rstrip() + "\n\n" + hook_block(skill_dir)
    validated(updated, "generated Codex config")

    if updated == original:
        return False

    if config.exists():
        backup = config.with_name(config.name + ".fable-mode.bak")
        if not backup.exists():
            shutil.copy2(config, backup)
    config.write_text(updated, encoding="utf-8", newline="\n")
    return True


def parse_args() -> argparse.Namespace:
    default_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path,
                        default=default_home / "config.toml")
    parser.add_argument("--skill-dir", type=Path,
                        default=Path(__file__).resolve().parent)
    parser.add_argument("--uninstall", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    changed = install(args.config.resolve(), args.skill_dir.resolve(),
                      args.uninstall)
    action = "removed" if args.uninstall else "installed"
    state = action if changed else "already up to date"
    print(f"fable-mode Codex hooks: {state} in {args.config.resolve()}")
    if not args.uninstall:
        print("Review and trust the new commands with /hooks in Codex.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
