"""Choosing the events stream, and the default changelog location that comes with it.

`auto` picks `claude-message` when the watcher runs under a Claude Code session that accepts messages, else `stdout`.
Under a Claude session the changelog defaults to `<session scratchpad>/glab-watch` in either mode, so the agent finds
it next to its other files.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cc_blackbox import ActiveSession, Priority

from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.watch.changelog import Changelog
from glab_cli_extras.watch.claude import ClaudeMessageSink, find_session
from glab_cli_extras.watch.sinks import Sink, StatusOutput, StdoutSink

STREAM_MODES = ("auto", "stdout", "claude-message")
MESSAGE_PRIORITIES = ("now", "next", "later")


class StreamError(RuntimeError):
    pass


@dataclass
class Stream:
    sink: Sink
    changelog: Changelog
    fallback: str | None = None
    """Why `auto` picked stdout although the watcher runs under a Claude Code session; printed in the header."""


def scratchpad_root(session: ActiveSession | None) -> Path | None:
    if session is None or session.scratchpad_dir is None:
        return None
    return session.scratchpad_dir / "glab-watch"


def messaging_problem(session: ActiveSession | None) -> str | None:
    """Why the session cannot take messages from this watcher, or None when it can."""
    if sys.platform == "win32":
        return "the claude-message stream is not supported on Windows"
    if session is None:
        return "no Claude Code session found (CLAUDE_CODE_MESSAGING_SOCKET is unset, or the session record is gone)"
    if session.can_receive_messages:
        return None
    reasons = [r for r in session.compatibility.reasons if r.startswith("messaging")]
    if reasons:
        return "the session's messaging is not compatible with this cc-blackbox version: " + " ".join(reasons)
    if session.messaging_socket is None:
        return "the session has messaging turned off"
    return f"the session's messaging socket {session.messaging_socket} does not exist"


def open_stream(
    mode: str,
    ref: ResourceRef,
    priority: str,
    changelog_dir: Path | None,
    default_root: Path,
    status: StatusOutput,
) -> Stream:
    session = find_session()
    root = changelog_dir or scratchpad_root(session) or default_root
    changelog = Changelog(root, ref)
    problem = messaging_problem(session)
    if mode == "claude-message" and problem is not None:
        raise StreamError(f"--events-stream=claude-message is not possible: {problem}; use --events-stream=stdout")
    if mode == "claude-message" or (mode == "auto" and problem is None):
        assert session is not None
        sink = ClaudeMessageSink(session, ref, changelog.events_path, cast(Priority, priority), status)
        return Stream(sink, changelog)
    # Under a Claude Code session, say why auto did not message it; outside one, stdout is simply the default.
    fallback = problem if mode == "auto" and session is not None else None
    return Stream(StdoutSink(), changelog, fallback)
