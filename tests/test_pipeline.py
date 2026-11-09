from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.events import KINDS_BY_RESOURCE, RawEvent
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources.base import PollContext, PollerOptions
from glab_cli_extras.resources.pipeline import PipelinePoller, diff_pipeline, parse_pipeline
from glab_cli_extras.sync.job_logs import TAIL_BYTES
from shapes import HOST, PROJECT, gql_downstream, gql_job, gql_pipeline

REF = ResourceRef("pipeline", HOST, PROJECT, 5001)
ALL = PollerOptions(frozenset(KINDS_BY_RESOURCE["pipeline"]), "failed")


def snapshot(status: str, *jobs: dict[str, object], downstream: list[dict[str, object]] | None = None) -> object:
    return parse_pipeline(gql_pipeline(5001, status, list(jobs), downstream), HOST, PROJECT)


def kinds(events: Sequence[RawEvent]) -> list[str]:
    return [e.kind for e in events]


def test_diff_reports_job_transitions_then_pipeline_status() -> None:
    prev = parse_pipeline(
        gql_pipeline(5001, "running", [gql_job(1, "build", "running"), gql_job(2, "test", "pending")]), HOST, PROJECT
    )
    cur = parse_pipeline(
        gql_pipeline(5001, "failed", [gql_job(1, "build", "success"), gql_job(2, "test", "failed")]), HOST, PROJECT
    )
    events = diff_pipeline(prev, cur)
    assert kinds(events) == ["job_finished", "job_finished", "status_changed", "finished"]
    assert events[1].data["name"] == "test" and events[1].data["status"] == "failed"
    assert events[2].data == {"pipeline_id": 5001, "from": "running", "to": "failed"}


def test_diff_ignores_skipped_jobs_and_reports_starts_and_downstream() -> None:
    prev = parse_pipeline(
        gql_pipeline(5001, "running", [gql_job(1, "lint", "created"), gql_job(2, "e2e", "pending")]), HOST, PROJECT
    )
    cur = parse_pipeline(
        gql_pipeline(
            5001,
            "running",
            [gql_job(1, "lint", "skipped"), gql_job(2, "e2e", "running")],
            [gql_downstream(6001, "pending", [gql_job(9, "deploy", "created")])],
        ),
        HOST,
        PROJECT,
    )
    events = diff_pipeline(prev, cur)
    assert kinds(events) == ["downstream_created", "job_started"]
    assert events[0].data["project"] == "group/deploy-tools"
    assert cur.jobs[-1].pipeline_id == 6001 and cur.jobs[-1].project == "group/deploy-tools"


def test_retried_job_is_a_new_job() -> None:
    prev = parse_pipeline(gql_pipeline(5001, "running", [gql_job(1, "test", "failed")]), HOST, PROJECT)
    cur = parse_pipeline(gql_pipeline(5001, "running", [gql_job(3, "test", "running")]), HOST, PROJECT)
    assert kinds(diff_pipeline(prev, cur)) == ["job_started"]


def _poller(fake: FakeGlab, clock: Clock, tmp_path: Path, options: PollerOptions = ALL) -> PipelinePoller:
    from glab_cli_extras.gitlab.client import GitLabClient

    ctx = PollContext(GitLabClient(HOST, runner=fake, clock=clock), tmp_path / "cache", clock)
    return PipelinePoller(REF, ctx, options)


def test_poller_uses_the_etag_and_fetches_the_failed_log_tail(clock: Clock, tmp_path: Path) -> None:
    big = TAIL_BYTES + 1000
    fake = FakeGlab(
        [
            Route(
                "PipelineTree", {"data": gql_pipeline(5001, "running", [gql_job(1, "test", "running")])}, etag='W/"v1"'
            ),
            Route("version", {"version": "19.5.3-ee"}),
            Route("jobs/1/trace", "...last lines of the log\nERROR: tests failed\n"),
        ]
    )
    poller = _poller(fake, clock, tmp_path)
    assert asyncio.run(poller.poll()) == []
    assert poller.next_delay() == 5.0
    assert asyncio.run(poller.poll()) == []
    assert fake.calls[-1].headers["If-None-Match"] == 'W/"v1"'

    fake.routes[0] = Route(
        "PipelineTree",
        {"data": gql_pipeline(5001, "failed", [gql_job(1, "test", "failed", trace_size=big)])},
        etag='W/"v2"',
    )
    events = asyncio.run(poller.poll())
    # job_finished waits for its log; the pipeline's own status events go out at once.
    assert kinds(events) == ["status_changed", "finished"]
    assert not poller.terminal
    assert 0.5 <= poller.next_delay() <= 5.0

    clock.advance(6)
    events = asyncio.run(poller.poll())
    assert kinds(events) == ["job_finished"]
    log_path = Path(events[0].artifacts[0])
    assert log_path.read_text().endswith("ERROR: tests failed\n")
    assert events[0].data["log_truncated"] is True
    trace_call = next(c for c in fake.calls if "trace" in c.endpoint)
    assert f"byte_offset={big - TAIL_BYTES}" in trace_call.endpoint
    assert f"byte_limit={TAIL_BYTES}" in trace_call.endpoint
    assert poller.terminal and poller.terminal_data == {"status": "failed"}


def test_old_gitlab_downloads_the_full_trace_and_logs_none_skips_it(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab(
        [
            Route(
                "PipelineTree", {"data": gql_pipeline(5001, "running", [gql_job(1, "test", "running")])}, etag='W/"v1"'
            ),
            Route("version", {"version": "18.11.0"}),
            Route("jobs/1/trace", "full log\n"),
        ]
    )
    poller = _poller(fake, clock, tmp_path)
    asyncio.run(poller.poll())
    fake.routes[0] = Route(
        "PipelineTree",
        {"data": gql_pipeline(5001, "failed", [gql_job(1, "test", "failed", trace_size=10**7)])},
        etag='W/"v2"',
    )
    asyncio.run(poller.poll())
    clock.advance(6)
    events = asyncio.run(poller.poll())
    trace_call = next(c for c in fake.calls if "trace" in c.endpoint)
    assert "byte_offset" not in trace_call.endpoint
    assert events[0].data["log_truncated"] is False


def test_logs_none_reports_job_finished_at_once_without_a_log(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([Route("PipelineTree", {"data": gql_pipeline(5001, "running", [gql_job(1, "t", "running")])})])
    poller = _poller(fake, clock, tmp_path, PollerOptions(ALL.events, "none"))
    asyncio.run(poller.poll())
    fake.routes[0] = Route("PipelineTree", {"data": gql_pipeline(5001, "running", [gql_job(1, "t", "failed")])})
    events = asyncio.run(poller.poll())
    assert kinds(events) == ["job_finished"] and events[0].artifacts == ()
    assert not any("trace" in e for e in fake.endpoints())


def test_a_server_without_etag_falls_back_to_slower_plain_polling(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([Route("PipelineTree", {"data": gql_pipeline(5001, "running", [])})])
    poller = _poller(fake, clock, tmp_path)
    asyncio.run(poller.poll())
    assert poller.next_delay() == 15.0
    asyncio.run(poller.poll())
    assert "X-GITLAB-GRAPHQL-RESOURCE-ETAG" not in fake.calls[-1].headers


def test_finished_pipeline_at_start_ends_at_once(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab(
        [Route("PipelineTree", {"data": gql_pipeline(5001, "success", [gql_job(1, "t", "success")])}, etag='W/"v"')]
    )
    poller = _poller(fake, clock, tmp_path)
    assert asyncio.run(poller.poll()) == []
    assert poller.terminal
    assert poller.summary()["jobs"] == {"success": 1}
