"""Choosing the events stream: where events go, and where the changelog lives by default."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.watch.sinks import Sink, StdoutSink

STREAM_MODES = ("auto", "stdout", "claude-message")
MESSAGE_PRIORITIES = ("now", "next", "later")


class StreamError(RuntimeError):
    pass


@dataclass
class Stream:
    sink: Sink
    changelog_root: Path | None = None


def open_stream(mode: str, ref: ResourceRef, priority: str) -> Stream:
    if mode == "claude-message":
        raise StreamError("the claude-message stream is not available in this build")
    return Stream(StdoutSink())
