"""The plugin's shell hooks: the guard around glab-cli-extras-cc-hook always exits 0, and the session check advises
on a missing, old or shadowed install."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from glab_cli_extras import __version__

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the plugin's hooks are POSIX shell scripts")

PLUGIN = Path(__file__).resolve().parents[1] / "coding-agent-plugins" / "claude-code"
GUARD = PLUGIN / "hooks" / "run-cc-hook.sh"
CHECK = PLUGIN / "hooks" / "check-glab-cli-extras.sh"
SYSTEM_PATH = "/usr/bin:/bin"
DENY = (
    '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"no"}}'
)


def fake(bin_dir: Path, name: str, script: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(0o755)


def run(script: Path, bin_dir: Path, stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = {"PATH": f"{bin_dir}:{SYSTEM_PATH}", "CLAUDE_PLUGIN_ROOT": str(PLUGIN), "HOME": str(bin_dir)}
    return subprocess.run([str(script)], input=stdin, capture_output=True, text=True, env=env, timeout=30)


def test_the_hook_scripts_are_executable() -> None:
    for script in (GUARD, CHECK):
        assert os.access(script, os.X_OK), script


@pytest.mark.parametrize(
    ("hook", "stdout"),
    [
        (None, ""),
        ("cat > /dev/null; echo 'not json' ; echo 'crashed' >&2; exit 2", ""),
        ("cat > /dev/null; echo 'crashed' >&2; exit 1", ""),
        (f"cat > /dev/null; echo '{DENY}'; exit 0", DENY + "\n"),
        ("cat > /dev/null; exit 0", ""),
    ],
    ids=["missing", "exits 2", "exits 1", "denies", "has no opinion"],
)
def test_the_guard_always_exits_0_and_forwards_only_a_clean_answer(
    hook: str | None, stdout: str, tmp_path: Path
) -> None:
    if hook is not None:
        fake(tmp_path, "glab-cli-extras-cc-hook", hook)
    result = run(GUARD, tmp_path, stdin='{"hook_event_name":"PreToolUse"}')
    assert (result.returncode, result.stdout) == (0, stdout)


def test_the_guard_passes_the_payload_on_stdin_and_stderr_through(tmp_path: Path) -> None:
    fake(tmp_path, "glab-cli-extras-cc-hook", "cat > \"$HOME/payload\"; echo 'a note' >&2")
    result = run(GUARD, tmp_path, stdin='{"tool_name":"Bash"}')
    assert (tmp_path / "payload").read_text() == '{"tool_name":"Bash"}'
    assert result.stderr == "a note\n"


def install(
    bin_dir: Path,
    *,
    watch: str | None = __version__,
    pipeline: str = "glab-cli-extras",
    discussion: str = "glab-cli-extras",
) -> None:
    if watch is not None:
        fake(bin_dir, "glab-watch", f"echo 'glab-watch {watch}'")
        fake(bin_dir, "glab-mr-status", "exit 0")
        fake(bin_dir, "glab-cli-extras-cc-hook", "exit 0")
    fake(bin_dir, "glab-pipeline", f"echo '{pipeline} {__version__}'")
    fake(bin_dir, "glab-discussion", f"echo '{discussion} {__version__}'")


def context(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    return str(output["additionalContext"])


def test_the_check_is_silent_when_all_is_well(tmp_path: Path) -> None:
    install(tmp_path)
    result = run(CHECK, tmp_path)
    assert (result.returncode, result.stdout) == (0, "")


def test_a_newer_patch_or_minor_is_fine(tmp_path: Path) -> None:
    major, minor, _ = __version__.split(".")
    install(tmp_path, watch=f"{major}.{int(minor) + 1}.0")
    assert run(CHECK, tmp_path).stdout == ""


def test_a_missing_install_is_reported(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    assert "uv tool install glab-cli-extras" in context(run(CHECK, tmp_path))


def test_an_older_minor_is_reported(tmp_path: Path) -> None:
    major, minor, _ = __version__.split(".")
    older = f"{major}.{int(minor) - 1}.9" if int(minor) else f"{int(major) - 1}.99.0"
    install(tmp_path, watch=older)
    text = context(run(CHECK, tmp_path))
    assert f"glab-watch {older}" in text and "uv tool upgrade glab-cli-extras" in text


def test_a_deprecated_standalone_command_is_reported(tmp_path: Path) -> None:
    install(tmp_path, discussion="glab-discussion")
    text = context(run(CHECK, tmp_path))
    assert "The glab-discussion on PATH is the deprecated standalone package" in text
    assert "uv tool uninstall glab-discussion && uv tool install --force glab-cli-extras" in text


def test_an_install_without_the_newer_commands_is_reported(tmp_path: Path) -> None:
    install(tmp_path)
    (tmp_path / "glab-cli-extras-cc-hook").unlink()
    text = context(run(CHECK, tmp_path))
    assert "lacks glab-cli-extras-cc-hook" in text and "uv tool upgrade --reinstall glab-cli-extras" in text
