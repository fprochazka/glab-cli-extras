"""glab's pollers on cli-watch-kit's engine: sharing, filters, the budget, the fetch ops, and the restart cases."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from cli_watch_kit.daemon.engine import Engine, EngineError
from cli_watch_kit.daemon.server import handle
from cli_watch_kit.source import Target

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.events import KINDS_BY_RESOURCE
from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources import branch as branch_res
from glab_cli_extras.resources import pipeline as pipeline_res
from glab_cli_extras.resources.base import PollContext, PollerOptions, PushFeedCache
from glab_cli_extras.serde import to_jsonable
from glab_cli_extras.sync.pipeline import parse_pipeline
from glab_cli_extras.watch.source import GlabSource, target_of
from shapes import HOST, PROJECT, gql_job, gql_mr, gql_pipeline

PIPELINE = target_of(ResourceRef("pipeline", HOST, PROJECT, 5001))
MR = target_of(ResourceRef("mr", HOST, PROJECT, 42))
ALL_PIPELINE = frozenset(KINDS_BY_RESOURCE["pipeline"]) - {"pipeline_stuck"}
ALL_MR = frozenset(KINDS_BY_RESOURCE["mr"]) - {"quiet", "pipeline_stuck"}
NO_LOGS = {"logs": "none"}


def pipeline_route(status: str, *jobs: dict[str, Any], etag: str = 'W/"1"') -> Route:
    return Route("PipelineTree", {"data": gql_pipeline(5001, status, list(jobs))}, etag=etag)


def make_engine(fake: FakeGlab, clock: Clock) -> Engine:
    return Engine(GlabSource(runner=fake, clock=clock), clock=clock)


def events_of(response: dict[str, Any], sid: str) -> list[dict[str, Any]]:
    return list(response["events"].get(sid, []))


def kinds_of(response: dict[str, Any], sid: str) -> list[str]:
    return [e["kind"] for e in events_of(response, sid)]


def resource(engine: Engine, target: Target) -> Any:
    found = engine.find(HOST, target)
    assert found is not None
    return found


def test_two_subscribers_share_one_poller_and_get_their_own_filter(clock: Clock) -> None:
    fake = FakeGlab([pipeline_route("running", gql_job(1, "test", "running"))])
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        a = await engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        b = await engine.subscribe("client-b", PIPELINE, frozenset({"finished"}), options=NO_LOGS)
        assert len(engine.resources) == 1
        await engine.poll_due_once()
        poll_a = engine.poll("client-a", {a["subscription_id"]: a["cursor"]})
        started = events_of(poll_a, a["subscription_id"])
        assert [e["kind"] for e in started] == ["watch_started"]
        assert started[0]["data"]["summary"]["jobs"] == {"running": 1}
        assert a["subscription_id"] in poll_a["snapshots"]

        fake.routes[0] = pipeline_route("success", gql_job(1, "test", "success"), etag='W/"2"')
        clock.advance(5)
        await engine.poll_due_once()
        poll_a = engine.poll("client-a", {a["subscription_id"]: 0})
        poll_b = engine.poll("client-b", {b["subscription_id"]: 0})
        assert kinds_of(poll_a, a["subscription_id"]) == ["job_finished", "status_changed", "finished", "watch_ended"]
        assert kinds_of(poll_b, b["subscription_id"]) == ["watch_started", "finished", "watch_ended"]
        # It ended while watched, so it is not marked as ended before the watch started.
        assert events_of(poll_a, a["subscription_id"])[-1]["data"] == {"reason": "finished", "status": "success"}
        assert sum(1 for c in fake.calls if c.endpoint == "graphql") == 2

    asyncio.run(scenario())


def test_late_subscriber_gets_watch_started_and_no_backlog(clock: Clock) -> None:
    fake = FakeGlab([pipeline_route("running", gql_job(1, "build", "running"))])
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        await engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        await engine.poll_due_once()
        fake.routes[0] = pipeline_route("running", gql_job(1, "build", "success"), etag='W/"2"')
        clock.advance(5)
        await engine.poll_due_once()
        late = await engine.subscribe("client-c", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        assert late["cursor"] == 1
        response = engine.poll("client-c", {late["subscription_id"]: late["cursor"]})
        assert kinds_of(response, late["subscription_id"]) == ["watch_started"]

    asyncio.run(scenario())


def test_an_mr_watch_reports_its_head_pipeline_from_the_mr_snapshot_alone(clock: Clock) -> None:
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": gql_mr(threads=[], pipeline_id=5001)})])
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        # The external statuses are the one pipeline-group kind that costs a request of its own.
        sub = await engine.subscribe("client-a", MR, ALL_MR - {"external_status_changed"})
        await engine.poll_due_once()
        fake.routes[0] = Route(
            "MergeRequestSnapshot", {"data": gql_mr(threads=[], pipeline_id=5001, pipeline_status="FAILED")}
        )
        clock.advance(16)
        await engine.poll_due_once()
        assert [r.target for r in engine.resources.values()] == [MR]
        response = engine.poll("client-a", {sub["subscription_id"]: 0})
        assert kinds_of(response, sub["subscription_id"]) == [
            "watch_started",
            "pipeline_status_changed",
            "pipeline_finished",
        ]
        assert all(call.endpoint == "graphql" for call in fake.calls)

    asyncio.run(scenario())


def test_status_only_mr_never_polls_the_pipeline(clock: Clock) -> None:
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": gql_mr(pipeline_id=None)})])
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        await engine.subscribe("client-a", MR, frozenset({"state_changed", "draft_changed"}))
        await engine.poll_due_once()
        assert [r.target for r in engine.resources.values()] == [MR]

    asyncio.run(scenario())


def test_not_found_ends_the_watch_and_rate_limits_degrade_once(clock: Clock) -> None:
    fake = FakeGlab(
        [
            Route("PipelineTree", {"data": {"project": {"pipeline": None}}}),
            Route("pipelines/5001", {"message": "404 Not found"}, status=404),
        ]
    )
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        sub = await engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        await engine.poll_due_once()
        events = events_of(engine.poll("client-a", {sub["subscription_id"]: 0}), sub["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_ended"]
        assert events[0]["data"]["reason"] == "not_found"

        other = target_of(ResourceRef("pipeline", HOST, PROJECT, 5002))
        fake.routes = [Route("PipelineTree", {"message": "Retry later"}, status=429, headers={"Retry-After": "1"})]
        sub2 = await engine.subscribe("client-a", other, ALL_PIPELINE, options=NO_LOGS)
        await engine.poll_due_once()
        # The second 429 keeps the same reason, so it is not reported again. Renew the lease while waiting.
        engine.poll("client-a", {sub2["subscription_id"]: 0})
        clock.advance(15)
        engine.poll("client-a", {})
        await engine.poll_due_once()
        assert resource(engine, other).failures == 2
        events = events_of(engine.poll("client-a", {sub2["subscription_id"]: 0}), sub2["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_degraded"]
        assert events[0]["data"]["reason"] == "rate_limited"

    asyncio.run(scenario())


def test_seeded_baseline_reports_what_changed_while_the_daemon_was_down(clock: Clock) -> None:
    fake = FakeGlab([pipeline_route("running", gql_job(1, "test", "running"))])
    first = make_engine(fake, clock)

    async def scenario() -> None:
        sub = await first.subscribe("client-a", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        await first.poll_due_once()
        stored = first.poll("client-a", {sub["subscription_id"]: 0})["snapshots"][sub["subscription_id"]]["data"]

        fake.routes[0] = pipeline_route("running", gql_job(1, "test", "success"), etag='W/"2"')
        second = make_engine(fake, clock)
        sub2 = await second.subscribe("client-a", PIPELINE, ALL_PIPELINE, stored, options=NO_LOGS)
        await second.poll_due_once()
        response = second.poll("client-a", {sub2["subscription_id"]: 0})
        assert kinds_of(response, sub2["subscription_id"]) == ["watch_started", "job_finished"]

    asyncio.run(scenario())


def test_end_on_the_first_poll_is_already_ended_unless_a_seed_saw_the_target_running(clock: Clock) -> None:
    fake = FakeGlab([pipeline_route("success", gql_job(1, "test", "success"))])
    engine = make_engine(fake, clock)
    running = parse_pipeline(gql_pipeline(5001, "running", [gql_job(1, "test", "running")]), HOST, PROJECT)

    async def scenario() -> None:
        sub = await engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        await engine.poll_due_once()
        events = events_of(engine.poll("client-a", {sub["subscription_id"]: 0}), sub["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_started", "watch_ended"]
        assert events[-1]["data"] == {"reason": "finished", "status": "success", "already_ended": True}

        # The pipeline finished while the daemon was down: it ended during the watch.
        other = make_engine(fake, clock)
        sub = await other.subscribe("client-a", PIPELINE, ALL_PIPELINE, to_jsonable(running), options=NO_LOGS)
        await other.poll_due_once()
        events = events_of(other.poll("client-a", {sub["subscription_id"]: 0}), sub["subscription_id"])
        assert events[-1]["kind"] == "watch_ended"
        assert events[-1]["data"] == {"reason": "finished", "status": "success"}

    asyncio.run(scenario())


def test_status_reports_requests_per_host_and_an_etag_304_costs_a_tenth(clock: Clock) -> None:
    fake = FakeGlab([pipeline_route("running")])
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        sub = await engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        await engine.poll_due_once()
        clock.advance(5)
        engine.poll("client-a", {sub["subscription_id"]: 0})
        await engine.poll_due_once()

    asyncio.run(scenario())
    status = engine.status()
    assert status["source"]["hosts"][HOST]["requests"] == 2
    assert status["source"]["hosts"][HOST]["not_modified"] == 1
    assert status["resources"][0]["resource"] == PIPELINE.key
    # The engine gave back its estimate: the account paid one request and one 304.
    assert engine.accounts[HOST].budget.dimension("requests").spent == pytest.approx(1.1)


def test_the_fetch_ops_run_in_the_daemon_from_its_cache(clock: Clock) -> None:
    fake = FakeGlab(
        [
            pipeline_route("failed", gql_job(1, "t", "failed")),
            Route("version", {"version": "18.11.0"}),
            Route("jobs/1/trace", "FAILED\n"),
        ]
    )
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        await engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        await engine.poll_due_once()
        request = {"op": "fetch_job_log", "host": HOST, "project": PROJECT, "job_id": 1}
        first = await handle(engine, dict(request), asyncio.Event(), {})
        second = await handle(engine, dict(request), asyncio.Event(), {})
        assert first["ok"] and first["log"] == second["log"]
        assert sum(1 for c in fake.calls if "jobs/1/trace" in c.endpoint) == 1

    asyncio.run(scenario())


def test_a_bad_logs_option_is_refused_at_subscribe(clock: Clock) -> None:
    engine = make_engine(FakeGlab([]), clock)
    with pytest.raises(EngineError, match="options.logs must be one of"):
        asyncio.run(engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options={"logs": "some"}))


def test_the_target_key_is_the_resource_of_the_events() -> None:
    assert Target.from_wire(PIPELINE.to_wire()) == PIPELINE
    assert PIPELINE.key == f"pipeline {HOST}/{PROJECT}!5001"
    assert MR.key == ResourceRef("mr", HOST, PROJECT, 42).key


def test_a_poller_that_fails_on_a_malformed_response_degrades_and_polls_again(clock: Clock) -> None:
    """Bug 3 of the move to cli-watch-kit: an exception after the poll left the resource unscheduled for good."""
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": {"project": {"mergeRequest": "oops"}}})])
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        sub = await engine.subscribe("client-a", MR, frozenset({"state_changed"}))
        await engine.poll_due_once()
        events = events_of(engine.poll("client-a", {sub["subscription_id"]: 0}), sub["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_degraded"]
        assert events[0]["data"]["reason"] == "error"
        assert resource(engine, MR).target in [r.target for r in engine.resources.values()]
        assert engine.scheduler.due_at(resource(engine, MR).rid) is not None

        cursor = events[0]["seq"]
        fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(pipeline_id=None)})
        for _ in range(40):
            clock.advance(15)
            engine.poll("client-a", {sub["subscription_id"]: cursor})
            await engine.poll_due_once()
            if engine.find(HOST, MR) is not None and resource(engine, MR).polled:
                break
        response = engine.poll("client-a", {sub["subscription_id"]: cursor})
        assert kinds_of(response, sub["subscription_id"]) == ["watch_started"]

    asyncio.run(scenario())


def test_an_mr_seeded_after_a_restart_that_answers_404_degrades(clock: Clock) -> None:
    """Bug 11: a seeded watch ended with not_found on its first poll after a daemon restart."""
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": gql_mr(pipeline_id=None)})])
    first = make_engine(fake, clock)

    async def scenario() -> None:
        sub = await first.subscribe("client-a", MR, frozenset({"state_changed"}))
        await first.poll_due_once()
        stored = first.poll("client-a", {sub["subscription_id"]: 0})["snapshots"][sub["subscription_id"]]["data"]

        fake.routes = [Route("MergeRequestSnapshot", {"message": "404 Not found"}, status=404)]
        second = make_engine(fake, clock)
        sub2 = await second.subscribe("client-a", MR, frozenset({"state_changed"}), stored)
        await second.poll_due_once()
        events = events_of(second.poll("client-a", {sub2["subscription_id"]: 0}), sub2["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_degraded"]
        assert events[0]["data"]["reason"] == "not_found"

    asyncio.run(scenario())


def test_two_mr_watchers_that_resubscribe_after_a_push_both_get_pushed(clock: Clock) -> None:
    """Bug 2: only the first resubscriber of a shared resource got the gap. The second one comes to a resource the
    new daemon already polls, and gets its gap from `GlabSource.diff`, without asking GitLab whether it was forced."""
    old_head, new_head = "a" * 40, "b" * 40
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": gql_mr(pipeline_id=None, head_sha=old_head)})])
    first = make_engine(fake, clock)
    kinds = frozenset({"pushed", "title_changed"})

    async def scenario() -> None:
        sub = await first.subscribe("client-a", MR, kinds)
        await first.poll_due_once()
        stored = first.poll("client-a", {sub["subscription_id"]: 0})["snapshots"][sub["subscription_id"]]["data"]

        fake.routes = [
            Route("MergeRequestSnapshot", {"data": gql_mr(pipeline_id=None, head_sha=new_head)}),
            Route("repository/merge_base", {"id": old_head}),
        ]
        second = make_engine(fake, clock)
        a = await second.subscribe("client-a", MR, kinds, stored)
        await second.poll_due_once()
        b = await second.subscribe("client-b", MR, kinds, stored)
        poll_a = events_of(second.poll("client-a", {a["subscription_id"]: 0}), a["subscription_id"])
        poll_b = events_of(second.poll("client-b", {b["subscription_id"]: b["cursor"]}), b["subscription_id"])
        assert [e["kind"] for e in poll_a] == ["watch_started", "pushed"]
        assert [e["kind"] for e in poll_b] == ["watch_started", "pushed"]
        assert poll_b[1]["data"] == {"from": old_head, "to": new_head, "forced": None}
        assert poll_a[1]["data"] == {"from": old_head, "to": new_head, "forced": False}

    asyncio.run(scenario())


def test_a_429_on_a_held_back_job_log_blocks_the_host_and_is_no_success(clock: Clock) -> None:
    fake = FakeGlab(
        [
            pipeline_route("running", gql_job(1, "test", "running")),
            Route("version", {"version": "18.11.0"}),
            Route("jobs/1/trace", {"message": "Retry later"}, status=429, headers={"Retry-After": "60"}),
        ]
    )
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        sub = await engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options={"logs": "failed"})
        await engine.poll_due_once()
        budget = engine.accounts[HOST].budget
        backoff = budget.backoff
        fake.routes[0] = pipeline_route("failed", gql_job(1, "test", "failed"), etag='W/"2"')
        # The failed job's log is held back for a moment, then fetched in a later poll, which gets the 429.
        for _ in range(4):
            clock.advance(6)
            engine.poll("client-a", {sub["subscription_id"]: 0})
            await engine.poll_due_once()
        assert sum(1 for c in fake.calls if "jobs/1/trace" in c.endpoint) == 1
        assert budget.rate_limited_count == 1
        assert budget.blocked_until - clock() > 30
        # A poll that counted as a success would have reset the backoff.
        assert budget.backoff == backoff * 2

    asyncio.run(scenario())


def test_the_interval_is_the_pollers_own_delay(clock: Clock) -> None:
    fake = FakeGlab([pipeline_route("pending", gql_job(1, "test", "created"))])
    engine = make_engine(fake, clock)

    async def scenario() -> None:
        assert engine.source.interval(PIPELINE, None) == pipeline_res.INTERVAL_RUNNING
        sub = await engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, options=NO_LOGS)
        await engine.poll_due_once()
        assert resource(engine, PIPELINE).interval == pipeline_res.INTERVAL_QUEUED
        fake.routes[0] = pipeline_route("running", gql_job(1, "test", "running"), etag='W/"2"')
        # Renew the lease while waiting for the next poll.
        for _ in range(2):
            clock.advance(pipeline_res.INTERVAL_QUEUED / 2)
            engine.poll("client-a", {sub["subscription_id"]: 0})
        await engine.poll_due_once()
        assert resource(engine, PIPELINE).interval == pipeline_res.INTERVAL_RUNNING

    asyncio.run(scenario())

    # A branch watch polls every 10 s when it reports pipelines, and every 30 s for pushes alone.
    source = GlabSource(runner=fake, clock=clock)
    ref = ResourceRef("branch", HOST, PROJECT, branch="main")
    ctx = PollContext(GitLabClient(HOST, runner=fake, clock=clock), clock, PushFeedCache())
    for events, expected in (
        (frozenset(KINDS_BY_RESOURCE["branch"]), branch_res.PIPELINE_INTERVAL),
        (frozenset({"pushed"}), branch_res.PUSH_INTERVAL),
    ):
        poller = branch_res.BranchPoller(ref, ctx, PollerOptions(events))
        source.pollers[target_of(ref).key] = poller
        assert source.interval(target_of(ref), None) == expected
