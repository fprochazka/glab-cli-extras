"""Rules the plugin's skills must keep, checked on their text."""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "coding-agent-plugins" / "claude-code"
CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def test_no_skill_example_passes_events_stream() -> None:
    """A watcher started with --events-stream=stdout under an agent talks to an output file nobody reads; the
    default reaches the session. No example may teach the option."""
    offenders = [
        f"{path.relative_to(PLUGIN)}: {line.strip()}"
        for path in sorted(PLUGIN.rglob("*.md"))
        for block in CODE_BLOCK.findall(path.read_text(encoding="utf-8"))
        for line in block.splitlines()
        if "--events-stream" in line
    ]
    assert offenders == []
