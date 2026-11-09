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

from claude_code_blackbox import ActiveSession, Priority

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


def scratchpad_root(session: ActiveSession | None) -> Path | None:
    if session is None or session.scratchpad_dir is None:
        return None
    return session.scratchpad_dir / "glab-watch"


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
    can_message = session is not None and session.can_receive_messages and sys.platform != "win32"
    if mode == "claude-message" and not can_message:
        if sys.platform == "win32":
            raise StreamError("the claude-message stream is not supported on Windows; use --events-stream=stdout")
        raise StreamError(
            "--events-stream=claude-message needs a Claude Code session that accepts messages, and none was found "
            "(CLAUDE_CODE_MESSAGING_SOCKET is unset, or the session record is gone); use --events-stream=stdout"
        )
    if mode == "claude-message" or (mode == "auto" and can_message):
        assert session is not None
        sink = ClaudeMessageSink(session, ref, changelog.events_path, cast(Priority, priority), status)
        return Stream(sink, changelog)
    return Stream(StdoutSink(), changelog)
