from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.events import KINDS_BY_RESOURCE
from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources.base import PollContext, PollerOptions, PushFeedCache
from glab_cli_extras.resources.branch import BranchPoller, PipelineEntry, diff_pipelines, new_branch_pushes
from glab_cli_extras.resources.mr_list import MrListPoller, MrListSnapshot, diff_mr_list
from shapes import HOST, PROJECT, gql_branch_pipelines, rest_mr, rest_push_event

OLD, NEW, OTHER = "1" * 40, "2" * 40, "3" * 40


def kinds(events: list[Any]) -> list[str]:
    return [e.kind for e in events]


def test_mr_list_diff_classifies_changes() -> None:
    prev = MrListSnapshot(PROJECT, "2026-11-09T10:00:00Z", "2026-11-09T10:00:00Z")
    prev, events = diff_mr_list(
        prev,
        [rest_mr(1, "opened", "2026-11-09T09:59:00Z"), rest_mr(2, "opened", "2026-11-09T09:59:30Z")],
    )
    # Both changed before the watch started; the overlap window only seeds them.
    assert kinds(events) == []
    _, events = diff_mr_list(
        prev,
        [
            rest_mr(1, "merged", "2026-11-09T10:01:00Z"),
            rest_mr(2, "opened", "2026-11-09T09:59:30Z"),
            rest_mr(3, "opened", "2026-11-09T10:02:00Z", created_at="2026-11-09T10:02:00Z", title="Add caching"),
        ],
    )
    assert [(e.kind, e.data["iid"]) for e in events] == [("mr_merged", 1), ("mr_opened", 3)]
    assert events[1].data["title"] == "Add caching" and events[1].data["author"] == "author-one"


def test_an_unknown_mr_changed_after_the_start_is_an_update() -> None:
    prev = MrListSnapshot(PROJECT, "2026-11-09T10:00:00Z", "2026-11-09T10:00:00Z")
    _, events = diff_mr_list(prev, [rest_mr(5, "opened", "2026-11-09T10:00:30Z")])
    assert kinds(events) == ["mr_updated"]


def test_mr_updated_says_which_list_fields_moved() -> None:
    prev = MrListSnapshot(PROJECT, "2026-11-09T10:00:00Z", "2026-11-09T10:00:00Z")
    draft = rest_mr(7, "opened", "2026-11-09T10:01:00Z") | {
        "draft": True,
        "sha": OLD,
        "detailed_merge_status": "draft_status",
        "labels": ["backend"],
        "reviewers": [{"username": "reviewer-one"}],
    }
    prev, _ = diff_mr_list(prev, [draft])
    ready = draft | {
        "updated_at": "2026-11-09T10:05:00Z",
        "draft": False,
        "detailed_merge_status": "checking",
        "labels": ["backend", "ready-for-review"],
        "reviewers": [{"username": "reviewer-two"}, {"username": "reviewer-one"}],
    }
    prev, events = diff_mr_list(prev, [ready])
    assert kinds(events) == ["mr_updated"]
    assert events[0].data["changes"] == {
        "draft": [True, False],
        "labels": [["backend"], ["backend", "ready-for-review"]],
        "reviewers": [["reviewer-one"], ["reviewer-one", "reviewer-two"]],
    }
    # The transient "checking" is never reported; the settled value that follows is.
    _, events = diff_mr_list(
        prev, [ready | {"updated_at": "2026-11-09T10:06:00Z", "detailed_merge_status": "mergeable"}]
    )
    assert events[0].data["changes"] == {"detailed_merge_status": ["draft_status", "mergeable"]}


def test_mr_list_reopen_and_close() -> None:
    prev = MrListSnapshot(PROJECT, "2026-11-09T10:00:00Z", "2026-11-09T10:00:00Z")
    prev, _ = diff_mr_list(prev, [rest_mr(1, "closed", "2026-11-09T10:00:00Z")])
    prev, events = diff_mr_list(prev, [rest_mr(1, "opened", "2026-11-09T10:05:00Z")])
    assert kinds(events) == ["mr_reopened"]
    _, events = diff_mr_list(prev, [rest_mr(1, "closed", "2026-11-09T10:06:00Z")])
    assert kinds(events) == ["mr_closed"]


def test_mr_list_poller_uses_the_watermark_with_overlap(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([Route("merge_requests", [rest_mr(1, "opened", "2026-11-09T09:00:00Z")])])
    ctx = PollContext(GitLabClient(HOST, runner=fake, clock=clock), clock)
    poller = MrListPoller(ResourceRef("mrs", HOST, PROJECT), ctx, PollerOptions(frozenset(KINDS_BY_RESOURCE["mrs"])))
    assert asyncio.run(poller.poll()) == []
    fake.routes[0] = Route("merge_requests", [rest_mr(1, "merged", "2026-11-09T12:00:00Z")])
    assert kinds(asyncio.run(poller.poll())) == ["mr_merged"]
    assert "updated_after=" in fake.calls[-1].endpoint and "order_by=updated_at" in fake.calls[-1].endpoint
    assert poller.snapshot is not None and poller.snapshot.watermark == "2026-11-09T12:00:00Z"
    asyncio.run(poller.poll())
    assert "updated_after=2026-11-09T11%3A58%3A00Z" in fake.calls[-1].endpoint


def test_push_feed_filtering() -> None:
    feed = [
        rest_push_event(13, None, "pushed", None, None, ref_count=5),
        rest_push_event(12, "other", "pushed", OLD, NEW),
        rest_push_event(11, "main", "pushed", OLD, NEW),
        rest_push_event(10, "main", "pushed", OTHER, OLD),
    ]
    pushes, bulk = new_branch_pushes(feed, "main", after_id=10)
    assert [p["id"] for p in pushes] == [11]
    assert bulk


def test_branch_pipeline_diff() -> None:
    prev = (PipelineEntry(5001, "running", None),)
    cur = (PipelineEntry(5001, "success", None), PipelineEntry(5002, "pending", None))
    events = diff_pipelines(prev, cur, "https://gitlab.example.com/group/project")
    assert [(e.kind, e.data["pipeline_id"]) for e in events] == [
        ("pipeline_finished", 5001),
        ("pipeline_created", 5002),
    ]


def _branch_poller(
    fake: FakeGlab, clock: Clock, tmp_path: Path, feeds: PushFeedCache | None = None, branch: str = "main"
) -> BranchPoller:
    ctx = PollContext(GitLabClient(HOST, runner=fake, clock=clock), clock, feeds or PushFeedCache())
    return BranchPoller(
        ResourceRef("branch", HOST, PROJECT, branch=branch), ctx, PollerOptions(frozenset(KINDS_BY_RESOURCE["branch"]))
    )


def test_branch_poller_reports_force_push_merge_commit_and_pipelines(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab(
        [
            Route("repository/branches/main", {"name": "main", "commit": {"id": OLD}}),
            Route("events", [rest_push_event(10, "main", "pushed", OTHER, OLD)]),
            Route("BranchPipelines", {"data": gql_branch_pipelines([(5001, "running")])}, etag='W/"p1"'),
        ]
    )
    poller = _branch_poller(fake, clock, tmp_path)
    assert asyncio.run(poller.poll()) == []
    assert poller.summary() == {
        "branch": "main",
        "exists": True,
        "head_sha": OLD,
        "latest_pipeline": {"id": 5001, "status": "running"},
    }

    # Pipelines first: the ETag probe now carries the project id.
    fake.routes[2] = Route(
        "BranchPipelines", {"data": gql_branch_pipelines([(5001, "success"), (5002, "created")])}, etag='W/"p2"'
    )
    clock.advance(10)
    events = asyncio.run(poller.poll())
    assert kinds(events) == ["pipeline_finished", "pipeline_created"]
    assert fake.calls[-1].headers["X-GITLAB-GRAPHQL-RESOURCE-ETAG"] == "/api/graphql:project_pipelines/321"

    clock.advance(10)
    assert asyncio.run(poller.poll()) == []
    assert fake.calls[-1].headers["If-None-Match"] == 'W/"p2"'

    fake.routes[1] = Route(
        "events",
        [
            rest_push_event(12, "main", "pushed", NEW, OTHER, title="Merge branch 'feature/x' into 'main'"),
            rest_push_event(11, "main", "pushed", OLD, NEW),
            rest_push_event(10, "main", "pushed", OTHER, OLD),
        ],
    )
    fake.routes.insert(0, Route("repository/merge_base", {"id": "9" * 40}, contains=f"refs[]={OLD}"))
    fake.routes.insert(0, Route("repository/merge_base", {"id": NEW}, contains=f"refs[]={NEW}"))
    fake.routes.insert(0, Route("commits/" + OTHER + "/merge_requests", [{"iid": 77, "state": "merged"}]))
    clock.advance(10)
    events = asyncio.run(poller.poll())
    assert kinds(events) == ["force_pushed", "pushed"]
    assert events[0].data["from"] == OLD and events[0].data["to"] == NEW
    assert events[1].data["merge_commit"] is True and events[1].data["merged_mr_iid"] == 77
    assert poller.snapshot is not None and poller.snapshot.head_sha == OTHER


def test_one_feed_request_serves_every_branch_of_a_project(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab(
        [
            Route("repository/branches/", {"commit": {"id": OLD}}),
            Route("events", [rest_push_event(i, "main", "pushed", OLD, NEW) for i in range(30, 10, -1)]),
            Route("BranchPipelines", {"data": gql_branch_pipelines([])}, etag='W/"p"'),
        ]
    )
    feeds = PushFeedCache()
    first = _branch_poller(fake, clock, tmp_path, feeds, "main")
    second = _branch_poller(fake, clock, tmp_path, feeds, "release")
    asyncio.run(first.poll())
    asyncio.run(second.poll())
    assert sum(1 for e in fake.endpoints() if "/events" in e) == 1


def test_deleted_branch(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab(
        [
            Route("repository/branches/main", {"commit": {"id": OLD}}),
            Route("events", []),
            Route("BranchPipelines", {"data": gql_branch_pipelines([])}, etag='W/"p"'),
        ]
    )
    poller = _branch_poller(fake, clock, tmp_path)
    asyncio.run(poller.poll())
    fake.routes[1] = Route("events", [rest_push_event(40, "main", "removed", OLD, "0" * 40)])
    clock.advance(31)
    events = asyncio.run(poller.poll())
    assert kinds(events) == ["deleted"]
    assert poller.snapshot is not None and not poller.snapshot.exists
