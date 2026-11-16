"""Choosing the events stream, and the base dir that comes with the session.

`auto` picks `claude-message` when the watcher runs under a Claude Code session that accepts messages, else `stdout`;
cc-blackbox makes that choice and gives the reason for a fallback. Under a Claude session every file goes to
`<session scratchpad>/glab-cli-extras` in either mode, so the agent finds it next to its other files; see
`paths.base_dir`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cc_blackbox import EVENT_STREAM_MODES, EventStreamMode, MessagingUnavailable, Priority, choose_event_stream

from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.paths import base_dir, find_session
from glab_cli_extras.watch.changelog import Changelog
from glab_cli_extras.watch.claude import claude_message_sink
from glab_cli_extras.watch.sinks import Sink, StatusOutput, StdoutSink

STREAM_MODES: tuple[str, ...] = EVENT_STREAM_MODES
MESSAGE_PRIORITIES = ("now", "next", "later")


class StreamError(RuntimeError):
    pass


@dataclass
class Stream:
    sink: Sink
    changelog: Changelog
    fallback: str | None = None
    """Why `auto` picked stdout although the watcher runs under a Claude Code session; printed in the header."""


def open_stream(
    mode: str,
    ref: ResourceRef,
    priority: str,
    base_dir_override: Path | None,
    status: StatusOutput,
) -> Stream:
    session = find_session()
    changelog = Changelog(base_dir(base_dir_override, session, detect=False), ref)
    try:
        choice = choose_event_stream(cast(EventStreamMode, mode), session)
    except MessagingUnavailable as exc:
        raise StreamError(f"--events-stream=claude-message is not possible: {exc}; use --events-stream=stdout") from exc
    if choice.mode == "claude-message":
        assert choice.session is not None
        sink = claude_message_sink(choice.session, ref, changelog.events_path, cast(Priority, priority), status)
        return Stream(sink, changelog)
    return Stream(StdoutSink(), changelog, choice.fallback)
