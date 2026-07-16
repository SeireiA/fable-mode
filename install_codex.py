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
import stat
import sys
import tempfile
import tomllib


BEGIN = "# BEGIN fable-mode Codex hooks"
END = "# END fable-mode Codex hooks"
PREVIOUS_HOOKS = "# fable-mode previous features.hooks: "
STRICT_PROFILE_NAME = "fable-strict.config.toml"
STRICT_PROFILE_MARKER = "# fable-mode managed strict runner profile"

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


def hook_block(skill_dir: Path, previous_hooks: str) -> str:
    lines = [BEGIN, PREVIOUS_HOOKS + previous_hooks]
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


def hooks_feature_state(text: str) -> str:
    data = tomllib.loads(text) if text.strip() else {}
    features = data.get("features", {})
    if "hooks" not in features:
        return "missing"
    return "true" if features["hooks"] is True else "false"


def saved_hooks_feature_state(text: str) -> str | None:
    pattern = re.compile(
        rf"(?m)^\s*{re.escape(PREVIOUS_HOOKS)}(true|false|missing)\s*$"
    )
    match = pattern.search(text)
    return match.group(1) if match else None


def restore_hooks_feature(text: str, state: str) -> str:
    if state == "true":
        return enable_hooks_feature(text)
    if state == "false":
        lines = enable_hooks_feature(text).splitlines()
        table_start = next(
            i for i, line in enumerate(lines) if line.strip() == "[features]"
        )
        table_end = next(
            (i for i in range(table_start + 1, len(lines))
             if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        for i in range(table_start + 1, table_end):
            if re.match(r"^\s*hooks\s*=", lines[i]):
                indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                lines[i] = indent + "hooks = false"
                break
        return "\n".join(lines).rstrip() + "\n"

    lines = text.splitlines()
    table_start = next(
        (i for i, line in enumerate(lines) if line.strip() == "[features]"),
        None,
    )
    if table_start is None:
        return text.rstrip() + "\n"
    table_end = next(
        (i for i in range(table_start + 1, len(lines))
         if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    lines[table_start + 1:table_end] = [
        line for line in lines[table_start + 1:table_end]
        if not re.match(r"^\s*hooks\s*=", line)
    ]
    return "\n".join(lines).rstrip() + "\n"


def validated(text: str, label: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{label} is not valid TOML: {exc}") from exc


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target_mode = mode
    if target_mode is None and path.exists():
        target_mode = stat.S_IMODE(path.stat().st_mode)
    if target_mode is None:
        target_mode = 0o600
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.{os.getpid()}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, target_mode)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if not hasattr(os, "fchmod"):
            temporary.chmod(target_mode)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def backup_current(path: Path) -> None:
    backup = path.with_name(path.name + ".fable-mode.bak")
    source_mode = stat.S_IMODE(path.stat().st_mode)
    atomic_write(
        backup,
        path.read_text(encoding="utf-8"),
        mode=source_mode,
    )


def is_managed_strict_profile(text: str) -> bool:
    return STRICT_PROFILE_MARKER in text.splitlines()


def strict_profile_text(skill_dir: Path) -> str:
    python_paths = [str(skill_dir.resolve())]
    inherited = os.environ.get("PYTHONPATH")
    if inherited:
        python_paths.append(inherited)
    python_path = os.pathsep.join(python_paths)
    return (
        f"{STRICT_PROFILE_MARKER}\n"
        "# Use with: codex -p fable-strict\n\n"
        "[features]\n"
        "multi_agent = false\n\n"
        "[shell_environment_policy]\n"
        f"set = {{ PYTHONPATH = {toml_string(python_path)} }}\n"
    )


def ensure_strict_profile_installable(profile: Path) -> None:
    if not profile.exists():
        return
    original = profile.read_text(encoding="utf-8")
    if not is_managed_strict_profile(original):
        raise SystemExit(
            f"{profile} is not managed by fable-mode; refusing to overwrite"
        )
    validated(original, str(profile))


def install_strict_profile(profile: Path, skill_dir: Path) -> bool:
    ensure_strict_profile_installable(profile)
    original = profile.read_text(encoding="utf-8") if profile.exists() else ""
    generated = strict_profile_text(skill_dir)
    validated(generated, "generated Codex strict runner profile")
    if original == generated:
        return False

    profile.parent.mkdir(parents=True, exist_ok=True)
    if profile.exists():
        backup_current(profile)
    atomic_write(profile, generated)
    return True


def uninstall_strict_profile(profile: Path) -> bool:
    if not profile.exists():
        return False

    original = profile.read_text(encoding="utf-8")
    if not is_managed_strict_profile(original):
        return False

    backup_current(profile)
    profile.unlink()
    return True


def install(config: Path, skill_dir: Path, uninstall: bool) -> bool:
    config.parent.mkdir(parents=True, exist_ok=True)
    original = config.read_text(encoding="utf-8") if config.exists() else ""
    if original.strip():
        validated(original, str(config))

    saved_state = saved_hooks_feature_state(original)
    updated = remove_marked_block(original)
    if uninstall:
        if saved_state is not None:
            updated = restore_hooks_feature(updated, saved_state)
    else:
        previous_state = saved_state or hooks_feature_state(updated)
        updated = enable_hooks_feature(updated)
        updated = updated.rstrip() + "\n\n" + hook_block(
            skill_dir, previous_state)
    validated(updated, "generated Codex config")

    if updated == original:
        return False

    if config.exists():
        backup_current(config)
    atomic_write(config, updated)
    return True


def restore_snapshot(path: Path, existed: bool, text: str) -> None:
    if existed:
        atomic_write(path, text)
    elif path.exists():
        path.unlink()


def apply_installation(
    config: Path,
    skill_dir: Path,
    uninstall: bool,
    with_strict_runner: bool,
) -> tuple[bool, bool]:
    strict_profile = config.parent / STRICT_PROFILE_NAME
    config_existed = config.exists()
    config_original = (
        config.read_text(encoding="utf-8") if config_existed else ""
    )
    profile_existed = strict_profile.exists()
    profile_original = (
        strict_profile.read_text(encoding="utf-8")
        if profile_existed else ""
    )
    try:
        changed = install(config, skill_dir, uninstall)
        if uninstall:
            strict_changed = uninstall_strict_profile(strict_profile)
        elif with_strict_runner:
            strict_changed = install_strict_profile(strict_profile, skill_dir)
        else:
            strict_changed = False
        return changed, strict_changed
    except BaseException:
        restore_snapshot(config, config_existed, config_original)
        restore_snapshot(strict_profile, profile_existed, profile_original)
        raise


def parse_args() -> argparse.Namespace:
    default_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path,
                        default=default_home / "config.toml")
    parser.add_argument("--skill-dir", type=Path,
                        default=Path(__file__).resolve().parent)
    parser.add_argument("--with-strict-runner", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = args.config.resolve()
    skill_dir = args.skill_dir.resolve()
    strict_profile = config.parent / STRICT_PROFILE_NAME
    if args.with_strict_runner and not args.uninstall:
        ensure_strict_profile_installable(strict_profile)

    changed, strict_changed = apply_installation(
        config,
        skill_dir,
        args.uninstall,
        args.with_strict_runner,
    )

    action = "removed" if args.uninstall else "installed"
    state = action if changed else "already up to date"
    print(f"fable-mode Codex hooks: {state} in {config}")

    if args.uninstall:
        if strict_changed:
            print(
                "fable-mode strict runner profile: removed from "
                f"{strict_profile}"
            )
        elif strict_profile.exists():
            print(
                "fable-mode strict runner profile: left unchanged in "
                f"{strict_profile} (not managed by fable-mode)"
            )
    elif args.with_strict_runner:
        strict_state = "installed" if strict_changed else "already up to date"
        print(
            "fable-mode strict runner profile: "
            f"{strict_state} in {strict_profile}"
        )
        print("Run strict orchestration with: codex -p fable-strict")

    if not args.uninstall:
        print("Review and trust the new commands with /hooks in Codex.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
