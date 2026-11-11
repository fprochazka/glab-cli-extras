"""Choosing the events stream, and the base dir that comes with the session.

`auto` picks `claude-message` when the watcher runs under a Claude Code session that accepts messages, else `stdout`.
Under a Claude session every file goes to `<session scratchpad>/glab-cli-extras` in either mode, so the agent finds
it next to its other files; see `paths.base_dir`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cc_blackbox import ActiveSession, Priority

from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.paths import base_dir
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


def messaging_problem(session: ActiveSession | None) -> str | None:
    """Why the session cannot take messages from this watcher, or None when it can."""
    if sys.platform == "win32":
        return "the claude-message stream is not supported on Windows"
    if session is None:
        if os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"):
            return (
                "CLAUDE_CODE_MESSAGING_SOCKET is set, but cc-blackbox found no live session record for it"
                " (the record is gone, or cc-blackbox judged its process not running)"
            )
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
    base_dir_override: Path | None,
    status: StatusOutput,
) -> Stream:
    session = find_session()
    changelog = Changelog(base_dir(base_dir_override, session, detect=False), ref)
    problem = messaging_problem(session)
    if mode == "claude-message" and problem is not None:
        raise StreamError(f"--events-stream=claude-message is not possible: {problem}; use --events-stream=stdout")
    if mode == "claude-message" or (mode == "auto" and problem is None):
        assert session is not None
        sink = ClaudeMessageSink(session, ref, changelog.events_path, cast(Priority, priority), status)
        return Stream(sink, changelog)
    # Under a Claude Code session, say why auto did not message it; outside one, stdout is simply the default.
    # A messaging socket in the environment means the watcher runs under a session even when no record was found.
    under_session = session is not None or bool(os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"))
    fallback = problem if mode == "auto" and under_session else None
    return Stream(StdoutSink(), changelog, fallback)
