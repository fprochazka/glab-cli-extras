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
from glab_cli_extras.resources.mr import MrPoller, diff_mr, parse_mr, parse_threads, settle, thread_cache_key
from glab_cli_extras.sync.mr import thread_sig
from shapes import HOST, PROJECT, gql_mr, gql_thread, rest_discussion

REF = ResourceRef("mr", HOST, PROJECT, 42)
ALL = PollerOptions(frozenset(KINDS_BY_RESOURCE["mr"]), "failed")
SHA1, SHA2 = "1" * 40, "2" * 40


def snap(**kwargs: Any) -> Any:
    node = gql_mr(**kwargs)["project"]["mergeRequest"]
    threads = parse_threads(node["discussions"]["nodes"]) if "discussions" in node else None
    return parse_mr(node, PROJECT, threads)


def kinds(events: list[Any]) -> list[str]:
    return [e.kind for e in events]


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
    assert events[8].data == {"pipeline_id": 5002, "status": "running", "previous_pipeline_id": 5001}


def test_transient_merge_status_keeps_the_settled_value() -> None:
    prev = snap(merge_status="MERGEABLE")
    for transient in ("CHECKING", "UNCHECKED", "PREPARING"):
        cur = settle(prev, snap(merge_status=transient))
        assert cur.merge_status == "mergeable"
        assert diff_mr(prev, cur) == []


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
        ]
    )
    poller = _poller(fake, clock, tmp_path)
    # The baseline fetches no thread at all: each client asks for the threads its own files lack.
    assert asyncio.run(poller.poll()) == []
    assert fake.endpoints() == ["graphql"]
    assert poller.summary()["threads"]["unresolved"] == 1
    assert poller.children() == frozenset({ResourceRef("pipeline", HOST, PROJECT, 5001)})

    thread_b = gql_thread("bbb222", [(7, "2026-11-09T10:00:00Z", "reviewer-two")])
    fake.routes[0] = Route("MergeRequestSnapshot", {"data": gql_mr(threads=[thread_a, thread_b], head_sha=SHA2)})
    fake.calls.clear()
    events = asyncio.run(poller.poll())
    assert kinds(events) == ["pushed", "thread_created"]
    assert events[0].data == {"from": SHA1, "to": SHA2, "forced": False}
    assert events[1].artifacts == ()
    rest_calls = [e for e in fake.endpoints() if e != "graphql"]
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
