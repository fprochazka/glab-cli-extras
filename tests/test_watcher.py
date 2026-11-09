"""The watcher's own event numbering: strictly increasing across everything it emits, daemon restarts included."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from glab_cli_extras.events import Event
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.sync.job_logs import LogContent
from glab_cli_extras.watch.changelog import Changelog
from glab_cli_extras.watch.client import Watcher, WatchSettings
from glab_cli_extras.watch.sinks import StatusOutput, StdoutSink

REF = ResourceRef("pipeline", "gitlab.example.com", "group/project", 5001)
ALL = frozenset({"watch_started", "watch_degraded", "watch_ended", "job_finished", "status_changed", "finished"})


def daemon_event(kind: str, seq: int, **data: Any) -> dict[str, Any]:
    return Event(kind, REF.key, seq, "2026-11-09T10:00:00.000Z", data).to_json()


class ScriptedBackend:
    """Answers polls from a script. `"restart"` makes the current subscription unknown, like a new daemon."""

    def __init__(self, script: list[Any]) -> None:
        self.script: Iterator[Any] = iter(script)
        self.subscriptions = 0
        self.cursors: list[int] = []

    def describe(self) -> str:
        return "scripted"

    def subscribe(
        self, client_id: str, ref: ResourceRef, events: frozenset[str], logs: str, baseline: Any
    ) -> tuple[str, int]:
        self.subscriptions += 1
        return f"sub-{self.subscriptions}", 0

    def poll(self, client_id: str, cursors: dict[str, int]) -> dict[str, Any]:
        ((sid, cursor),) = cursors.items()
        self.cursors.append(cursor)
        step: Any = next(self.script, [])
        if step == "restart":
            return {"unknown": [sid], "next_poll_ms": 0}
        return {"events": {sid: step} if step else {}, "next_poll_ms": 0}

    def unsubscribe(self, client_id: str, subscription_id: str) -> None:
        pass

    def fetch_job_log(self, host: str, project: str, job_id: int) -> LogContent:
        raise AssertionError("no log expected")

    def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> dict[str, dict[str, Any]]:
        raise AssertionError("no thread expected")

    def begin_shutdown(self) -> None:
        pass

    def close(self) -> None:
        pass


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def run(backend: ScriptedBackend, changelog: Changelog, out: io.StringIO, timeout: float = 40) -> int:
    watcher = Watcher(
        WatchSettings(REF, ALL, "none", None, timeout),
        backend,
        StdoutSink(out),
        changelog,
        StatusOutput(io.StringIO()),
        clock=Ticks(),
    )
    return watcher.run()


def seqs(lines: list[str]) -> list[int]:
    return [json.loads(line)["seq"] for line in lines]


def test_seq_is_strictly_increasing_across_a_daemon_restart_and_client_events(tmp_path: Path) -> None:
    backend = ScriptedBackend(
        [
            # The first daemon: its private watch_started carries seq 0, like the live bug showed.
            [daemon_event("watch_started", 0, summary={}, snapshot_hash="h1")],
            [daemon_event("job_finished", 1, job_id=1), daemon_event("status_changed", 2)],
            "restart",
            # The new daemon numbers from scratch; its watch_started changed snapshot, so it is reported.
            [daemon_event("watch_started", 0, summary={}, snapshot_hash="h2"), daemon_event("job_finished", 1)],
        ]
    )
    changelog = Changelog(tmp_path, REF)
    out = io.StringIO()
    assert run(backend, changelog, out) == 0
    lines = out.getvalue().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds == ["watch_started", "job_finished", "status_changed", "watch_started", "job_finished", "watch_ended"]
    assert seqs(lines) == [1, 2, 3, 4, 5, 6]
    assert changelog.events_path.read_text().splitlines() == lines
    assert backend.subscriptions == 2
    # The cursor still follows the daemon's own numbering, and resets with the new daemon.
    assert backend.cursors[:4] == [0, 0, 2, 0]


def test_a_new_watcher_continues_the_changelog_numbering(tmp_path: Path) -> None:
    changelog = Changelog(tmp_path, REF)
    run(ScriptedBackend([[daemon_event("watch_started", 0, summary={})]]), changelog, io.StringIO(), timeout=3)
    second = io.StringIO()
    run(ScriptedBackend([[daemon_event("watch_started", 0, summary={})]]), changelog, second, timeout=3)
    assert seqs(second.getvalue().splitlines()) == [3, 4]
    assert seqs(changelog.events_path.read_text().splitlines()) == [1, 2, 3, 4]
    assert changelog.last_seq() == 4
