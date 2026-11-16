"""Where a watcher delivers events, and where its own status lines go.

Status lines (the startup header, the end line, errors) go to stderr, so stdout carries only events. Claude Code puts
a background Bash task's stderr into the task output next to its stdout, so an agent reads both. Status lines start
with `glab-watch: ` and never with `{`, so a reader of the merged output can still tell them from events.

The sinks themselves come from cc-blackbox: `StdoutSink` prints one JSON event per line, and `watch.claude` builds the
`claude-message` sink.
"""

from __future__ import annotations

import sys
from typing import TextIO

from cc_blackbox import EventSink
from cc_blackbox import StdoutEventSink as StdoutSink

from glab_cli_extras.events import Event

__all__ = ["STATUS_PREFIX", "Sink", "StatusOutput", "StdoutSink"]

STATUS_PREFIX = "glab-watch: "

type Sink = EventSink[Event]


class StatusOutput:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    @property
    def stream(self) -> TextIO:
        if self._stream is not None:
            return self._stream
        return sys.stderr

    def line(self, text: str) -> None:
        self.stream.write(f"{STATUS_PREFIX}{text}\n")
        self.stream.flush()
