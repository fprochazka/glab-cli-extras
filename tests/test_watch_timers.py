"""`quiet` and `pipeline_stuck`: computed in the watch client from the snapshots it receives, on a fake clock."""

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
from glab_cli_extras.watch.timers import Thresholds, WatchTimers, format_duration

MR = ResourceRef("mr", "gitlab.example.com", "group/project", 42)
PIPELINE = ResourceRef("pipeline", "gitlab.example.com", "group/project", 5001)
HEAD = "b" * 40


def mr_snapshot(
    *, head: str = HEAD, pipeline_id: int | None = 5001, status: str | None = "success", sha: str | None = None
) -> dict[str, Any]:
    return {
        "head_sha": head,
        "head_pipeline_id": pipeline_id,
        "head_pipeline_status": status,
        "head_pipeline_sha": sha if sha is not None else head,
        "head_pipeline_web_url": f"https://gitlab.example.com/group/project/-/pipelines/{pipeline_id}",
    }


def pipeline_snapshot(status: str, jobs: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "id": 5001,
        "status": status,
        "sha": HEAD,
        "web_url": "https://gitlab.example.com/group/project/-/pipelines/5001",
        "jobs": [{"name": name, "status": job_status} for name, job_status in jobs],
    }


def event(kind: str) -> Event:
    return Event(kind, MR.key, 1, "2026-11-09T10:00:00.000Z", {})


def kinds(due: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, str]]:
    return [(kind, str(data["after"])) for kind, data in due]


def test_quiet_fires_once_per_threshold_after_green_on_the_head() -> None:
    timers = WatchTimers("mr", Thresholds(quiet_after=(120.0, 900.0)), now=0)
    timers.snapshot(mr_snapshot(status="running"), now=0)
    timers.snapshot(mr_snapshot(status="success"), now=60)
    assert timers.due(179) == []
    assert timers.seconds_until_next(179) == 1
    due = timers.due(180)
    assert kinds(due) == [("quiet", "2m")]
    assert due[0][1] == {"after": "2m", "head_sha": HEAD, "pipeline_id": 5001}
    assert timers.due(500) == []
    assert kinds(timers.due(960)) == [("quiet", "15m")]
    assert timers.due(5000) == []


def test_a_reported_event_restarts_the_quiet_count_but_a_quiet_event_does_not() -> None:
    timers = WatchTimers("mr", Thresholds(quiet_after=(120.0,)), now=0)
    timers.snapshot(mr_snapshot(), now=0)
    timers.reported([event("thread_created")], now=100)
    assert timers.due(200) == []
    assert kinds(timers.due(220)) == [("quiet", "2m")]
    timers.reported([event("quiet")], now=230)
    assert timers.seconds_until_next(230) is None


def test_a_push_starts_the_count_again_and_quiet_fires_for_the_new_head() -> None:
    timers = WatchTimers("mr", Thresholds(quiet_after=(120.0,)), now=0)
    timers.snapshot(mr_snapshot(), now=0)
    assert kinds(timers.due(120)) == [("quiet", "2m")]
    new_head = "c" * 40
    # Until a pipeline runs on the new head, GitLab keeps the old green one as the head pipeline: not quiet.
    timers.snapshot(mr_snapshot(head=new_head, sha=HEAD), now=130)
    assert timers.due(1000) == []
    timers.snapshot(mr_snapshot(head=new_head, pipeline_id=5002, status="running"), now=1010)
    timers.snapshot(mr_snapshot(head=new_head, pipeline_id=5002, status="success"), now=1100)
    assert timers.due(1219) == []
    due = timers.due(1220)
    assert due[0][1]["head_sha"] == new_head and due[0][1]["pipeline_id"] == 5002


def test_no_quiet_without_a_green_pipeline() -> None:
    timers = WatchTimers("mr", Thresholds(quiet_after=(60.0,)), now=0)
    timers.snapshot(mr_snapshot(status="failed"), now=0)
    timers.snapshot(mr_snapshot(pipeline_id=None, status=None), now=10)
    assert timers.due(10_000) == []


def test_a_snapshot_without_the_pipeline_sha_counts_the_pipeline_as_on_the_head() -> None:
    timers = WatchTimers("mr", Thresholds(quiet_after=(60.0,)), now=0)
    snapshot = mr_snapshot()
    del snapshot["head_pipeline_sha"]
    timers.snapshot(snapshot, now=0)
    assert kinds(timers.due(60)) == [("quiet", "1m")]


def test_the_mr_head_pipeline_is_reported_stuck_once() -> None:
    timers = WatchTimers("mr", Thresholds(stuck_after=1800.0), now=0)
    timers.snapshot(mr_snapshot(status="pending"), now=10)
    timers.snapshot(mr_snapshot(status="running"), now=300)
    assert timers.due(1809) == []
    due = timers.due(1810)
    assert due == [
        (
            "pipeline_stuck",
            {
                "after": "30m",
                "pipeline_id": 5001,
                "status": "running",
                "web_url": "https://gitlab.example.com/group/project/-/pipelines/5001",
                "watch_command": "glab-watch pipeline https://gitlab.example.com/group/project/-/pipelines/5001",
            },
        )
    ]
    assert timers.due(10_000) == []


def test_a_finished_pipeline_is_not_stuck_and_a_new_one_gets_its_own_count() -> None:
    timers = WatchTimers("mr", Thresholds(stuck_after=600.0), now=0)
    timers.snapshot(mr_snapshot(status="running"), now=0)
    timers.snapshot(mr_snapshot(status="failed"), now=500)
    assert timers.due(700) == []
    timers.snapshot(mr_snapshot(pipeline_id=5002, status="running"), now=800)
    assert timers.due(1399) == []
    assert timers.due(1400)[0][1]["pipeline_id"] == 5002


def test_a_watched_pipeline_names_its_running_jobs_when_stuck() -> None:
    timers = WatchTimers("pipeline", Thresholds(stuck_after=60.0), now=0)
    timers.snapshot(pipeline_snapshot("running", [("build", "success"), ("e2e", "running"), ("lint", "pending")]), 0)
    ((kind, data),) = timers.due(60)
    assert kind == "pipeline_stuck"
    assert data["running_jobs"] == ["e2e", "lint"]
    assert "watch_command" not in data


def test_durations_print_as_the_options_take_them() -> None:
    assert [format_duration(s) for s in (45, 120, 5400, 7200, 3661)] == ["45s", "2m", "1h30m", "2h", "1h1m1s"]


class SnapshotBackend:
    """Answers each poll with the next step: (events, snapshot data or None)."""

    def __init__(self, ref: ResourceRef, steps: list[tuple[list[dict[str, Any]], Any]]) -> None:
        self.ref = ref
        self.steps: Iterator[tuple[list[dict[str, Any]], Any]] = iter(steps)
        self.subscribed: frozenset[str] = frozenset()
        self.hash = 0

    def describe(self) -> str:
        return "scripted"

    def subscribe(
        self, client_id: str, ref: ResourceRef, events: frozenset[str], logs: str, baseline: Any
    ) -> tuple[str, int]:
        self.subscribed = events
        return "sub", 0

    def poll(self, client_id: str, cursors: dict[str, int]) -> dict[str, Any]:
        events, snapshot = next(self.steps, ([], None))
        response: dict[str, Any] = {"events": {"sub": events}, "next_poll_ms": 0}
        if snapshot is not None:
            self.hash += 1
            response["snapshots"] = {"sub": {"hash": str(self.hash), "data": snapshot}}
        return response

    def unsubscribe(self, client_id: str, subscription_id: str) -> None:
        pass

    def fetch_job_log(self, host: str, project: str, job_id: int) -> LogContent:
        raise AssertionError("no log expected")

    def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> dict[str, dict[str, Any]]:
        raise AssertionError("no thread expected")

    def fetch_dump(self, ref: ResourceRef) -> dict[str, Any]:
        raise AssertionError("no dump expected")

    def begin_shutdown(self) -> None:
        pass

    def close(self) -> None:
        pass


class Clock:
    """Advances 30 s per reading, as if every poll took that long."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 30.0
        return self.now


def test_the_watcher_reports_quiet_without_asking_the_daemon_for_it(tmp_path: Path) -> None:
    started = Event("watch_started", MR.key, 0, "2026-11-09T10:00:00.000Z", {"summary": {}}).to_json()
    backend = SnapshotBackend(MR, [([started], mr_snapshot())])
    selected = frozenset({"watch_started", "watch_degraded", "watch_ended", "state_changed", "quiet"})
    out = io.StringIO()
    watcher = Watcher(
        WatchSettings(MR, selected, "none", "quiet", 600, Thresholds(quiet_after=(120.0,))),
        backend,
        StdoutSink(out),
        Changelog(tmp_path, MR),
        StatusOutput(io.StringIO()),
        clock=Clock(),
    )
    assert watcher.run() == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line["kind"] for line in lines] == ["watch_started", "quiet", "watch_ended"]
    assert lines[1]["data"] == {"after": "2m", "head_sha": HEAD, "pipeline_id": 5001}
    assert lines[2]["data"]["reason"] == "until"
    assert [line["seq"] for line in lines] == [1, 2, 3]
    # The daemon is asked for the head pipeline, which the snapshot needs, and never for the client's own kinds.
    assert "quiet" not in backend.subscribed
    assert {"head_pipeline_changed", "pipeline_status_changed", "pipeline_finished"} <= backend.subscribed


def test_idle_counts_from_the_last_reported_event_other_than_a_threshold_event() -> None:
    timers = WatchTimers("mrs", Thresholds(idle_after=3600.0), now=0)
    timers.reported([event("mr_updated")], now=1000)
    assert not timers.idle(4599)
    assert timers.seconds_until_next(4000) == 600
    timers.reported([event("quiet"), event("pipeline_stuck")], now=4000)
    assert timers.idle(4600)


def test_the_watcher_ends_idle_after_nothing_was_reported(tmp_path: Path) -> None:
    started = Event("watch_started", MR.key, 0, "2026-11-09T10:00:00.000Z", {"summary": {}}).to_json()
    changed = Event("draft_changed", MR.key, 1, "2026-11-09T10:01:00.000Z", {"draft": False}).to_json()
    backend = SnapshotBackend(MR, [([started], mr_snapshot(status="running")), ([changed], None)])
    selected = frozenset({"watch_started", "watch_degraded", "watch_ended", "draft_changed"})
    out = io.StringIO()
    clock = Clock()
    watcher = Watcher(
        WatchSettings(MR, selected, "none", None, None, Thresholds(idle_after=300.0)),
        backend,
        StdoutSink(out),
        Changelog(tmp_path, MR),
        StatusOutput(io.StringIO()),
        clock=clock,
    )
    assert watcher.run() == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line["kind"] for line in lines] == ["watch_started", "draft_changed", "watch_ended"]
    assert lines[-1]["data"] == {"reason": "idle", "idle_after": "5m"}
