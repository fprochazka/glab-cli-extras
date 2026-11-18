"""The watcher's own event numbering: strictly increasing across everything it emits, daemon restarts included."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from cli_watch_kit.changelog import Changelog
from cli_watch_kit.client import Watcher, WatchSettings
from cli_watch_kit.sinks import StatusOutput, StdoutSink

from glab_cli_extras.events import Event
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.watch.source import target_of
from watch_harness import ScriptedBackend, Ticks

REF = ResourceRef("pipeline", "gitlab.example.com", "group/project", 5001)
ALL = frozenset({"watch_started", "watch_degraded", "watch_ended", "job_finished", "status_changed", "finished"})


def daemon_event(kind: str, seq: int, **data: Any) -> dict[str, Any]:
    return Event(kind, REF.key, seq, "2026-11-09T10:00:00.000Z", data).to_json()


def run(backend: ScriptedBackend, changelog: Changelog, out: io.StringIO, timeout: float = 40) -> int:
    watcher = Watcher(
        WatchSettings(target_of(REF), ALL, timeout=timeout),
        backend,
        StdoutSink(out),
        changelog,
        StatusOutput("glab-watch: ", io.StringIO()),
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
    changelog = Changelog(tmp_path, REF.key)
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
    changelog = Changelog(tmp_path, REF.key)
    run(ScriptedBackend([[daemon_event("watch_started", 0, summary={})]]), changelog, io.StringIO(), timeout=3)
    second = io.StringIO()
    run(ScriptedBackend([[daemon_event("watch_started", 0, summary={})]]), changelog, second, timeout=3)
    assert seqs(second.getvalue().splitlines()) == [3, 4]
    assert seqs(changelog.events_path.read_text().splitlines()) == [1, 2, 3, 4]
    assert changelog.last_seq() == 4
