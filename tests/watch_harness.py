"""A scripted stand-in for the daemon, for running cli-watch-kit's `Watcher` with glab's writer and timers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

from cli_watch_kit.source import Target


class ScriptedBackend:
    """Answers each poll with the next step of a script.

    A step is a list of events (as JSON), a `(events, snapshot data or None)` pair, or `"restart"`, which makes the
    subscription unknown, like a new daemon. `ops` answers the source's extra ops (the writer's fetches).
    """

    def __init__(self, script: list[Any], ops: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None):
        self.script: Iterator[Any] = iter(script)
        self.ops = dict(ops or {})
        self.subscriptions = 0
        self.subscribed: frozenset[str] = frozenset()
        self.source_options: Mapping[str, Any] = {}
        self.baselines: list[Any] = []
        self.cursors: list[int] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.hash = 0

    def describe(self) -> str:
        return "scripted"

    def subscribe(
        self,
        client_id: str,
        target: Target,
        events: frozenset[str],
        baseline: Any,
        params: Mapping[str, Any],
        source_options: Mapping[str, Any],
    ) -> tuple[str, int]:
        self.subscriptions += 1
        self.subscribed = events
        self.source_options = source_options
        self.baselines.append(baseline)
        return f"sub-{self.subscriptions}", 0

    def poll(self, client_id: str, cursors: dict[str, int], acks: dict[str, int]) -> dict[str, Any]:
        ((sid, cursor),) = cursors.items()
        self.cursors.append(cursor)
        step: Any = next(self.script, [])
        if step == "restart":
            return {"unknown": [sid], "next_poll_ms": 0}
        events, snapshot = step if isinstance(step, tuple) else (step, None)
        response: dict[str, Any] = {"events": {sid: events} if events else {}, "next_poll_ms": 0}
        if snapshot is not None:
            self.hash += 1
            response["snapshots"] = {sid: {"hash": str(self.hash), "data": snapshot}}
        return response

    def call(self, op: str, payload: Mapping[str, Any], params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((op, dict(payload)))
        handler = self.ops.get(op)
        if handler is None:
            raise AssertionError(f"no {op} expected")
        return handler(dict(payload))

    def unsubscribe(self, client_id: str, subscription_id: str) -> None:
        pass

    def begin_shutdown(self) -> None:
        pass

    def close(self) -> None:
        pass


class Ticks:
    """A clock that advances `step` seconds per reading, as if every poll took that long."""

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now
