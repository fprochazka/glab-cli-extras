from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.daemon.engine import Engine
from glab_cli_extras.events import KINDS_BY_RESOURCE
from glab_cli_extras.gitlab.refs import ResourceRef
from shapes import HOST, PROJECT, gql_job, gql_mr, gql_pipeline

PIPELINE = ResourceRef("pipeline", HOST, PROJECT, 5001)
MR = ResourceRef("mr", HOST, PROJECT, 42)
ALL_PIPELINE = frozenset(KINDS_BY_RESOURCE["pipeline"])
ALL_MR = frozenset(KINDS_BY_RESOURCE["mr"])


def pipeline_route(status: str, *jobs: dict[str, Any], etag: str = 'W/"1"') -> Route:
    return Route("PipelineTree", {"data": gql_pipeline(5001, status, list(jobs))}, etag=etag)


def make_engine(fake: FakeGlab, clock: Clock, tmp_path: Path) -> Engine:
    return Engine(cache_root=tmp_path / "cache", runner=fake, clock=clock)


def events_of(response: dict[str, Any], sid: str) -> list[dict[str, Any]]:
    return list(response["events"].get(sid, []))


def test_two_subscribers_share_one_poller_and_get_their_own_filter(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([pipeline_route("running", gql_job(1, "test", "running"))])
    engine = make_engine(fake, clock, tmp_path)

    async def scenario() -> None:
        a = engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, "none")
        b = engine.subscribe("client-b", PIPELINE, frozenset({"finished"}), "none")
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
        assert [e["kind"] for e in events_of(poll_a, a["subscription_id"])] == [
            "job_finished",
            "status_changed",
            "finished",
            "watch_ended",
        ]
        assert [e["kind"] for e in events_of(poll_b, b["subscription_id"])] == [
            "watch_started",
            "finished",
            "watch_ended",
        ]
        assert sum(1 for c in fake.calls if c.endpoint == "graphql") == 2

    asyncio.run(scenario())


def test_late_subscriber_gets_watch_started_and_no_backlog(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([pipeline_route("running", gql_job(1, "build", "running"))])
    engine = make_engine(fake, clock, tmp_path)

    async def scenario() -> None:
        engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, "none")
        await engine.poll_due_once()
        fake.routes[0] = pipeline_route("running", gql_job(1, "build", "success"), etag='W/"2"')
        clock.advance(5)
        await engine.poll_due_once()
        late = engine.subscribe("client-c", PIPELINE, ALL_PIPELINE, "none")
        assert late["cursor"] == 1
        response = engine.poll("client-c", {late["subscription_id"]: late["cursor"]})
        assert [e["kind"] for e in events_of(response, late["subscription_id"])] == ["watch_started"]

    asyncio.run(scenario())


def test_mr_relays_its_head_pipeline_events(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab(
        [
            Route("MergeRequestSnapshot", {"data": gql_mr(threads=[], pipeline_id=5001)}),
            Route("merge_requests/42/discussions", []),
            pipeline_route("running", gql_job(1, "test", "running")),
        ]
    )
    engine = make_engine(fake, clock, tmp_path)

    async def scenario() -> None:
        sub = engine.subscribe("client-a", MR, ALL_MR, "none")
        await engine.poll_due_once()
        assert PIPELINE.key in engine.resources
        assert engine.resources[PIPELINE.key].parents == {MR.key}
        await engine.poll_due_once()
        fake.routes[2] = pipeline_route("running", gql_job(1, "test", "failed"), etag='W/"2"')
        clock.advance(5)
        await engine.poll_due_once()
        response = engine.poll("client-a", {sub["subscription_id"]: 0})
        events = events_of(response, sub["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_started", "job_finished"]
        assert events[1]["resource"] == MR.key and events[1]["data"]["pipeline_id"] == 5001
        engine.unsubscribe("client-a", sub["subscription_id"])
        assert engine.resources == {}

    asyncio.run(scenario())


def test_status_only_mr_never_polls_the_pipeline(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": gql_mr(pipeline_id=None)})])
    engine = make_engine(fake, clock, tmp_path)

    async def scenario() -> None:
        engine.subscribe("client-a", MR, frozenset({"state_changed", "draft_changed"}), "none")
        await engine.poll_due_once()
        assert list(engine.resources) == [MR.key]

    asyncio.run(scenario())


def test_leases_expire_and_the_engine_goes_idle(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([pipeline_route("running")])
    engine = make_engine(fake, clock, tmp_path)
    sub = engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, "none")
    assert engine.idle_for(clock()) == 0
    clock.advance(15)
    engine.poll("client-a", {sub["subscription_id"]: 0})
    clock.advance(15)
    engine.reap(clock())
    assert engine.subscriptions
    clock.advance(21)
    engine.reap(clock())
    assert not engine.subscriptions and not engine.resources
    clock.advance(30)
    assert engine.idle_for(clock()) == 30
    response = engine.poll("client-a", {sub["subscription_id"]: 0})
    assert response["unknown"] == [sub["subscription_id"]]


def test_not_found_ends_the_watch_and_rate_limits_degrade_once(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([Route("PipelineTree", {"data": {"project": {"pipeline": None}}})])
    engine = make_engine(fake, clock, tmp_path)

    async def scenario() -> None:
        sub = engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, "none")
        await engine.poll_due_once()
        events = events_of(engine.poll("client-a", {sub["subscription_id"]: 0}), sub["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_ended"]
        assert events[0]["data"]["reason"] == "not_found"

        other = ResourceRef("pipeline", HOST, PROJECT, 5002)
        fake.routes = [Route("PipelineTree", {"message": "Retry later"}, status=429, headers={"Retry-After": "1"})]
        sub2 = engine.subscribe("client-a", other, ALL_PIPELINE, "none")
        await engine.poll_due_once()
        # The second 429 keeps the same reason, so it is not reported again. Renew the lease while waiting.
        engine.poll("client-a", {sub2["subscription_id"]: 0})
        clock.advance(15)
        engine.poll("client-a", {})
        await engine.poll_due_once()
        assert engine.resources[other.key].failures == 2
        events = events_of(engine.poll("client-a", {sub2["subscription_id"]: 0}), sub2["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_degraded"]
        assert events[0]["data"]["reason"] == "rate_limited"

    asyncio.run(scenario())


def test_seeded_baseline_reports_what_changed_while_the_daemon_was_down(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([pipeline_route("running", gql_job(1, "test", "running"))])
    first = make_engine(fake, clock, tmp_path)

    async def scenario() -> None:
        sub = first.subscribe("client-a", PIPELINE, ALL_PIPELINE, "none")
        await first.poll_due_once()
        stored = first.poll("client-a", {sub["subscription_id"]: 0})["snapshots"][sub["subscription_id"]]["data"]

        fake.routes[0] = pipeline_route("running", gql_job(1, "test", "success"), etag='W/"2"')
        second = make_engine(fake, clock, tmp_path)
        sub2 = second.subscribe("client-a", PIPELINE, ALL_PIPELINE, "none", baseline=stored)
        await second.poll_due_once()
        events = events_of(second.poll("client-a", {sub2["subscription_id"]: 0}), sub2["subscription_id"])
        assert [e["kind"] for e in events] == ["watch_started", "job_finished"]

    asyncio.run(scenario())


def test_status_reports_requests_per_host(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([pipeline_route("running")])
    engine = make_engine(fake, clock, tmp_path)

    async def scenario() -> None:
        engine.subscribe("client-a", PIPELINE, ALL_PIPELINE, "none")
        await engine.poll_due_once()
        clock.advance(5)
        await engine.poll_due_once()

    asyncio.run(scenario())
    status = engine.status()
    assert status["hosts"][HOST]["requests"]["requests"] == 2
    assert status["hosts"][HOST]["requests"]["not_modified"] == 1
    assert status["resources"][0]["resource"] == PIPELINE.key
