from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.events import GROUPS, KINDS_BY_RESOURCE
from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources.base import PollContext, PollerOptions
from glab_cli_extras.resources.mr import (
    STATUS_REFRESH_POLLS,
    MrPoller,
    diff_mr,
    parse_mr,
    parse_threads,
    settle,
    thread_cache_key,
)
from glab_cli_extras.sync.mr import parse_external_statuses, thread_sig
from shapes import HOST, PROJECT, gql_mr, gql_thread, rest_commit_status, rest_discussion

REF = ResourceRef("mr", HOST, PROJECT, 42)
ALL = PollerOptions(frozenset(KINDS_BY_RESOURCE["mr"]), "failed")
SHA1, SHA2 = "1" * 40, "2" * 40


def snap(**kwargs: Any) -> Any:
    node = gql_mr(**kwargs)["project"]["mergeRequest"]
    threads = parse_threads(node["discussions"]["nodes"]) if "discussions" in node else None
    return parse_mr(node, PROJECT, threads)


def kinds(events: list[Any]) -> list[str]:
    return [e.kind for e in events]


def _status_route(*statuses: dict[str, Any]) -> Route:
    return Route("/statuses", list(statuses))


def test_diff_covers_the_status_fields_in_order() -> None:
    prev = snap(state="closed", draft=True, approvals_left=1, reviewers=["reviewer-one"])
    cur = snap(
        state="opened",
        draft=False,
        title="Add the login form, take two",
        description="New text.",
        head_sha=SHA2,
        merge_status="NOT_APPROVED",
        approved_by=["reviewer-one"],
        approvals_left=0,
        reviewers=["reviewer-one", "reviewer-two"],
        pipeline_id=5002,
    )
    events = diff_mr(prev, cur)
    assert kinds(events) == [
        "state_changed",
        "draft_changed",
        "title_changed",
        "description_changed",
        "pushed",
        "merge_status_changed",
        "approvals_changed",
        "reviewers_changed",
        "head_pipeline_changed",
    ]
    assert events[0].data == {"from": "closed", "to": "opened", "reopened": True}
    assert events[6].data["added"] == ["reviewer-one"] and events[6].data["approvals_left"] == 0
    assert events[7].data["added"] == ["reviewer-two"]
    url = f"https://{HOST}/{PROJECT}/-/pipelines/5002"
    assert events[8].data == {
        "pipeline_id": 5002,
        "web_url": url,
        "watch_command": f"glab-watch pipeline {url}",
        "status": "running",
        "previous_pipeline_id": 5001,
    }


def test_transient_merge_status_keeps_the_settled_value() -> None:
    prev = snap(merge_status="MERGEABLE")
    for transient in ("CHECKING", "UNCHECKED", "PREPARING"):
        cur = settle(prev, snap(merge_status=transient))
        assert cur.merge_status == "mergeable"
        assert diff_mr(prev, cur) == []


def test_a_merged_or_closed_mr_reports_no_merge_status_change() -> None:
    prev = snap(state="opened", merge_status="CI_STILL_RUNNING")
    for terminal in ("merged", "closed"):
        assert kinds(diff_mr(prev, snap(state=terminal, merge_status="NOT_OPEN"))) == ["state_changed"]
    merged = snap(state="merged", merge_status="NOT_OPEN")
    assert diff_mr(merged, snap(state="merged", merge_status="MERGEABLE")) == []


def test_thread_diff_tells_new_notes_from_resolves_and_edits() -> None:
    base = [
        gql_thread("aaa", [(1, "2026-11-09T09:00:00Z", "reviewer-one")]),
        gql_thread("bbb", [(2, "2026-11-09T09:00:00Z", "reviewer-two")]),
        gql_thread("ccc", [(3, "2026-11-09T09:00:00Z", "reviewer-one")]),
        gql_thread("sys", [(4, "2026-11-09T09:00:00Z", "author-one")], system=True),
    ]
    prev = snap(threads=base)
    assert [t.id for t in prev.threads] == ["aaa", "bbb", "ccc"]
    cur = snap(
        threads=[
            # A reply.
            gql_thread("aaa", [(1, "2026-11-09T09:00:00Z", "reviewer-one"), (5, "2026-11-09T10:00:00Z", "author-one")]),
            # Resolved: GitLab bumps updatedAt on every note of the thread; that is not an edit.
            gql_thread("bbb", [(2, "2026-11-09T10:00:00Z", "reviewer-two")], resolved=True),
            # An edit.
            gql_thread("ccc", [(3, "2026-11-09T10:00:00Z", "reviewer-one")]),
            gql_thread("ddd", [(6, "2026-11-09T10:00:00Z", "reviewer-two")]),
        ]
    )
    events = diff_mr(prev, cur)
    assert [(e.kind, e.data["discussion_id"]) for e in events] == [
        ("thread_updated", "aaa"),
        ("thread_resolved", "bbb"),
        ("thread_updated", "ccc"),
        ("thread_created", "ddd"),
    ]
    assert events[0].data["new_notes"] == 1 and events[0].data["last_author"] == "author-one"
    assert events[2].data["edited_notes"] == 1
    unresolved = diff_mr(cur, replace(cur, threads=snap(threads=[gql_thread("bbb", [(2, "x", "r")])]).threads))
    assert ("thread_unresolved", "bbb") in [(e.kind, e.data.get("discussion_id")) for e in unresolved]


def _poller(fake: FakeGlab, clock: Clock, tmp_path: Path, options: PollerOptions = ALL) -> MrPoller:
    ctx = PollContext(GitLabClient(HOST, runner=fake, clock=clock), clock)
    return MrPoller(REF, ctx, options)


def test_poller_fetches_only_the_threads_that_changed_into_the_cache(clock: Clock, tmp_path: Path) -> None:
    thread_a = gql_thread("aaa111", [(1, "2026-11-09T09:00:00Z", "reviewer-one")])
    fake = FakeGlab(
        [
            Route("MergeRequestSnapshot", {"data": gql_mr(threads=[thread_a], head_sha=SHA1)}),
            Route(
                "merge_requests/42/discussions/bbb222",
                rest_discussion("bbb222", [(7, "reviewer-two", "Please rename this.")], diff=True),
            ),
            Route("repository/merge_base", {"id": SHA1}),
            _status_route(),
        ]
    )
    poller = _poller(fake, clock, tmp_path)
    # The baseline fetches no thread at all: each client asks for the threads its own files lack.
    assert asyncio.run(poller.poll()) == []
    assert [e for e in fake.endpoints() if "/statuses" not in e] == ["graphql"]
    assert poller.summary()["threads"]["unresolved"] == 1
    assert poller.children() == frozenset()

    thread_b = gql_thread("bbb222", [(7, "2026-11-09T10:00:00Z", "reviewer-two")])
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(threads=[thread_a, thread_b], head_sha=SHA2)})
    fake.calls.clear()
    events = asyncio.run(poller.poll())
    assert kinds(events) == ["pushed", "thread_created"]
    assert events[0].data == {"from": SHA1, "to": SHA2, "forced": False}
    assert events[1].artifacts == ()
    rest_calls = [e for e in fake.endpoints() if e != "graphql" and "/statuses" not in e]
    assert rest_calls == [
        f"projects/group%2Fproject/repository/merge_base?refs[]={SHA1}&refs[]={SHA2}",
        "projects/group%2Fproject/merge_requests/42/discussions/bbb222",
    ]
    assert poller.snapshot is not None and poller.snapshot.threads is not None
    sig = thread_sig(poller.snapshot.threads[1])
    cached = poller.ctx.threads.get(thread_cache_key(REF, "bbb222", sig))
    assert cached is not None and cached["id"] == "bbb222"


def test_force_push_when_the_old_head_is_gone(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab(
        [
            Route("MergeRequestSnapshot", {"data": gql_mr(head_sha=SHA1)}),
            Route("repository/merge_base", {"message": "404 Commit Not Found"}, status=404),
        ]
    )
    poller = _poller(fake, clock, tmp_path, PollerOptions(frozenset({"pushed"})))
    asyncio.run(poller.poll())
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(head_sha=SHA2)})
    events = asyncio.run(poller.poll())
    assert events[0].data["forced"] is True and "no longer" in events[0].data["forced_reason"]


def test_filter_without_comments_or_pipeline_saves_the_work(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": gql_mr(threads=None, pipeline_id=None)})])
    status_only = frozenset(GROUPS["status"]) & frozenset(KINDS_BY_RESOURCE["mr"])
    poller = _poller(fake, clock, tmp_path, PollerOptions(status_only))
    asyncio.run(poller.poll())
    sent = fake.calls[0].variables
    assert sent["withThreads"] is False and sent["withPipeline"] is False
    assert poller.children() == frozenset()
    assert len(fake.calls) == 1


def test_merged_mr_is_terminal(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": gql_mr()})])
    poller = _poller(fake, clock, tmp_path, PollerOptions(frozenset({"state_changed"})))
    asyncio.run(poller.poll())
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(state="merged")})
    assert kinds(asyncio.run(poller.poll())) == ["state_changed"]
    assert poller.terminal_reason == "merged"


def test_premium_level_budget_halves_the_interval(clock: Clock, tmp_path: Path) -> None:
    poller = _poller(FakeGlab(), clock, tmp_path)
    assert poller.next_delay() == 30.0
    poller.ctx.client.budget.limit_per_minute = 480
    assert poller.next_delay() == 15.0


def test_approvals_gitlab_does_not_return_are_unknown_not_removed() -> None:
    known = snap(approved_by=["reviewer-one"], approvals_left=0)
    node = gql_mr(approved_by=["reviewer-one"])["project"]["mergeRequest"]
    node["approvedBy"] = None
    missing = parse_mr(node, PROJECT, None)
    assert missing.approved_by is None and missing.approved is None
    settled = settle(known, missing)
    assert diff_mr(known, settled) == []
    assert settled.approved_by == ("reviewer-one",)
    # A snapshot that never knew the approvals compares with nothing.
    assert [e.kind for e in diff_mr(missing, known)] == []
    later = snap(approved_by=[], approvals_left=1)
    assert [e.kind for e in diff_mr(settled, later)] == ["approvals_changed"]


def test_an_unresolve_right_after_a_push_is_marked(clock: Clock, tmp_path: Path) -> None:
    resolved = gql_thread("aaa111", [(1, "2026-11-09T09:00:00Z", "reviewer-one")], resolved=True)
    unresolved = gql_thread("aaa111", [(1, "2026-11-09T09:10:00Z", "reviewer-one")], resolved=False)
    fake = FakeGlab(
        [
            Route("MergeRequestSnapshot", {"data": gql_mr(threads=[resolved], head_sha=SHA1)}),
            Route("repository/merge_base", {"id": SHA1}),
            Route("merge_requests/42/discussions/aaa111", rest_discussion("aaa111", [(1, "reviewer-one", "x")])),
            _status_route(),
        ]
    )
    poller = _poller(fake, clock, tmp_path)
    asyncio.run(poller.poll())
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(threads=[resolved], head_sha=SHA2)})
    assert kinds(asyncio.run(poller.poll())) == ["pushed"]
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(threads=[unresolved], head_sha=SHA2)})
    events = asyncio.run(poller.poll())
    unresolve = next(e for e in events if e.kind == "thread_unresolved")
    assert unresolve.data["after_push"] is True
    for _ in range(3):
        asyncio.run(poller.poll())
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(threads=[resolved], head_sha=SHA2)})
    asyncio.run(poller.poll())
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(threads=[unresolved], head_sha=SHA2)})
    later = next(e for e in asyncio.run(poller.poll()) if e.kind == "thread_unresolved")
    assert "after_push" not in later.data


def test_the_head_pipeline_is_reported_by_status_never_by_job() -> None:
    running = snap(pipeline_id=5001, pipeline_status="RUNNING")
    failed = snap(pipeline_id=5001, pipeline_status="FAILED", failed_jobs=["unit tests", "lint"])
    url = f"https://{HOST}/{PROJECT}/-/pipelines/5001"
    command = f"glab-watch pipeline {url}"
    events = diff_mr(running, failed)
    assert kinds(events) == ["pipeline_status_changed", "pipeline_finished"]
    assert events[0].data == {
        "pipeline_id": 5001,
        "web_url": url,
        "watch_command": command,
        "from": "running",
        "to": "failed",
    }
    assert events[1].data["failed_jobs"] == ["lint", "unit tests"] and events[1].data["watch_command"] == command
    # A status change that does not end the pipeline is exactly one event.
    pending = snap(pipeline_id=5001, pipeline_status="PENDING")
    assert kinds(diff_mr(pending, running)) == ["pipeline_status_changed"]
    # No job event kind is ever produced for an MR.
    assert not {"job_started", "job_finished", "downstream_created"} & set(KINDS_BY_RESOURCE["mr"])


def test_diff_reports_target_branch_assignees_labels_and_blocking_threads() -> None:
    prev = snap(target_branch="main", assignees=["author-one"], labels=["backend"], threads_resolved=False)
    cur = snap(
        target_branch="feature/base",
        assignees=["author-one", "author-two"],
        labels=["frontend"],
        threads_resolved=True,
    )
    events = diff_mr(prev, cur)
    assert kinds(events) == [
        "target_branch_changed",
        "assignees_changed",
        "labels_changed",
        "blocking_threads_resolved_changed",
    ]
    assert events[0].data == {"from": "main", "to": "feature/base"}
    assert events[1].data == {"assignees": ["author-one", "author-two"], "added": ["author-two"], "removed": []}
    assert events[2].data == {"labels": ["frontend"], "added": ["frontend"], "removed": ["backend"]}
    assert events[3].data == {"resolved": True}
    for kind in ("target_branch_changed", "assignees_changed", "labels_changed", "blocking_threads_resolved_changed"):
        assert kind in GROUPS["status"]
    assert "external_status_changed" in GROUPS["pipeline"]


def test_a_snapshot_from_an_older_version_compares_with_nothing() -> None:
    # A client keeps its last snapshot across daemon restarts; one written before these fields existed has none.
    old = replace(snap(), assignees=None, labels=None, blocking_threads_resolved=None, external_statuses=None)
    assert diff_mr(old, snap(assignees=["author-two"], labels=["frontend"], threads_resolved=False)) == []


def test_external_statuses_skip_the_head_pipelines_jobs() -> None:
    statuses = [
        rest_commit_status("unit tests", "success", pipeline_id=5001),
        # An older pipeline's run of a job the head pipeline also has.
        rest_commit_status("unit tests", "failed", pipeline_id=4999),
        rest_commit_status("quality-gate", "failed", target_url="https://quality.example.com/42"),
        rest_commit_status("quality-gate", "success"),
        rest_commit_status("security-scan", "pending", pipeline_id=5100),
    ]
    parsed = parse_external_statuses(statuses, 5001)
    assert [(s.name, s.status) for s in parsed] == [("quality-gate", "failed"), ("security-scan", "pending")]
    assert parsed[0].target_url == "https://quality.example.com/42"


def test_external_statuses_are_fetched_only_when_something_moved(clock: Clock, tmp_path: Path) -> None:
    gate_running = rest_commit_status("quality-gate", "running")
    gate_failed = rest_commit_status("quality-gate", "failed", target_url="https://quality.example.com/42")
    fake = FakeGlab(
        [
            Route("MergeRequestSnapshot", {"data": gql_mr(head_sha=SHA1, pipeline_status="RUNNING")}),
            _status_route(rest_commit_status("unit tests", "running", pipeline_id=5001)),
            Route("repository/merge_base", {"id": SHA1}),
        ]
    )
    options = PollerOptions(frozenset({"external_status_changed", "pushed"}))
    poller = _poller(fake, clock, tmp_path, options)

    def statuses_calls() -> int:
        return sum(1 for e in fake.endpoints() if "/statuses" in e)

    # The baseline fetches them once and reports nothing.
    assert asyncio.run(poller.poll()) == []
    assert statuses_calls() == 1
    assert fake.calls[0].variables["withPipeline"] is True
    assert poller.summary()["external_statuses"] == []

    # Nothing moved: the statuses are not fetched again.
    assert asyncio.run(poller.poll()) == []
    assert statuses_calls() == 1

    # The pipeline ended: they are, and a new external status is reported.
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(head_sha=SHA1, pipeline_status="SUCCESS")})
    fake.routes[1] = _status_route(gate_running)
    # The poller reports every kind; the engine filters per subscriber.
    events = [e for e in asyncio.run(poller.poll()) if e.kind == "external_status_changed"]
    assert len(events) == 1
    assert events[0].data == {
        "name": "quality-gate",
        "from": None,
        "to": "running",
        "allow_failure": False,
        "description": "quality-gate says running",
    }
    assert statuses_calls() == 2

    # While one is unfinished, every poll fetches them.
    fake.routes[1] = _status_route(gate_failed)
    events = asyncio.run(poller.poll())
    assert [(e.kind, e.data["from"], e.data["to"]) for e in events] == [
        ("external_status_changed", "running", "failed")
    ]
    assert events[0].data["target_url"] == "https://quality.example.com/42"
    assert statuses_calls() == 3

    # Settled: only the periodic refresh fetches them, every STATUS_REFRESH_POLLS polls.
    for _ in range(STATUS_REFRESH_POLLS - 1):
        assert asyncio.run(poller.poll()) == []
    assert statuses_calls() == 3
    asyncio.run(poller.poll())
    assert statuses_calls() == 4

    # A push fetches the new head's statuses.
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(head_sha=SHA2, pipeline_status="SUCCESS")})
    fake.routes[1] = _status_route()
    assert kinds(asyncio.run(poller.poll())) == ["pushed"]
    assert statuses_calls() == 5


def test_without_external_status_events_no_status_is_fetched(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": gql_mr()})])
    poller = _poller(fake, clock, tmp_path, PollerOptions(frozenset({"pipeline_finished"})))
    asyncio.run(poller.poll())
    asyncio.run(poller.poll())
    assert fake.endpoints() == ["graphql", "graphql"]
    assert poller.snapshot is not None and poller.snapshot.external_statuses is None
    assert "external_statuses" not in poller.summary()


def test_the_summary_names_labels_assignees_and_blocking_threads(clock: Clock, tmp_path: Path) -> None:
    node = gql_mr(labels=["backend", "bug"], threads_resolved=False)
    fake = FakeGlab([Route("MergeRequestSnapshot", {"data": node})])
    poller = _poller(fake, clock, tmp_path, PollerOptions(frozenset({"labels_changed"})))
    asyncio.run(poller.poll())
    summary = poller.summary()
    assert summary["labels"] == ["backend", "bug"]
    assert summary["assignees"] == ["author-one"]
    assert summary["blocking_threads_resolved"] is False
    assert summary["target_branch"] == "main"
