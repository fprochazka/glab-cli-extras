"""Where a watcher delivers events, and where its own status lines go.

Status lines (the startup header, the end line, errors) go to stderr, so stdout carries only events. Claude Code puts
a background Bash task's stderr into the task output next to its stdout, so an agent reads both. Status lines start
with `glab-watch: ` and never with `{`, so a reader of the merged output can still tell them from events.
"""

from __future__ import annotations

import json
import sys
from typing import Protocol, TextIO

from glab_cli_extras.events import Event

STATUS_PREFIX = "glab-watch: "


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


class Sink(Protocol):
    name: str

    def describe(self) -> str:
        """One line for the startup header: the stream mode and its target."""
        ...

    def emit(self, events: list[Event]) -> None: ...

    def alive(self) -> bool:
        """False once the target is gone, which ends the watch."""
        ...

    def close(self) -> None: ...


class StdoutSink:
    name = "stdout"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def describe(self) -> str:
        return "stdout (one JSON event per line)"

    def emit(self, events: list[Event]) -> None:
        stream = self._stream or sys.stdout
        for event in events:
            stream.write(json.dumps(event.to_json(), ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()

    def alive(self) -> bool:
        return True

    def close(self) -> None:
        pass
