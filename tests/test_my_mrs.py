"""The merge request dashboard: its list rules, `glab-my-mrs`, and the `glab-watch my-mrs` poller, diff and writer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cli_watch_kit.sinks import StatusOutput
from typer.testing import CliRunner

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.cli import my_mrs as my_mrs_cli
from glab_cli_extras.cli.watch import app as watch_app
from glab_cli_extras.dashboard.lists import (
    ACTION_BASED,
    ROLE_BASED,
    ListFilter,
    ListFilterError,
    classify,
    effective_layout,
    parse_list_filter,
)
from glab_cli_extras.dashboard.model import DashboardMr, Lists, mr_from_node
from glab_cli_extras.dashboard.query import merge_sets
from glab_cli_extras.events import Event
from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.gitlab.glab import ProcessResult
from glab_cli_extras.gitlab.refs import RefError, ResourceRef, parse_dashboard_ref
from glab_cli_extras.resources.base import PollContext, PollerOptions
from glab_cli_extras.resources.my_mrs import MyMrsPoller, MyMrsSnapshot, classify_all, diff_my_mrs
from glab_cli_extras.serde import from_jsonable, to_jsonable
from glab_cli_extras.store.layout import resource_dir
from glab_cli_extras.watch.source import GlabSource, target_of
from glab_cli_extras.watch.writer import ResourceWriter, filter_my_mrs_events
from shapes import HOST

ME = "alice"
REF = ResourceRef("my-mrs", HOST, "")
STARTED = "2026-11-20T10:00:00Z"


def node(
    iid: int,
    *,
    author: str = ME,
    reviewers: Sequence[tuple[str, str]] = (),
    draft: bool = False,
    updated_at: str = "2026-11-20T09:00:00Z",
    created_at: str = "2026-11-01T09:00:00Z",
    pipeline: str | None = "SUCCESS",
    state: str = "opened",
    merged_at: str | None = None,
    project: str = "group/project",
) -> dict[str, Any]:
    """A `MergeRequest` node of the dashboard query, as GitLab 19.5 returns it."""
    return {
        "id": f"gid://gitlab/MergeRequest/{1000 + iid}",
        "iid": str(iid),
        "reference": f"{project}!{iid}",
        "title": f"Change {iid}",
        "webUrl": f"https://{HOST}/{project}/-/merge_requests/{iid}",
        "draft": draft,
        "state": state,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "mergedAt": merged_at,
        "sourceBranch": f"feature-{iid}",
        "targetBranch": "main",
        "diffHeadSha": "1" * 40,
        "detailedMergeStatus": "MERGEABLE",
        "author": {"username": author},
        "assignees": {"nodes": [{"username": author}]},
        "labels": {"nodes": [{"title": "backend"}]},
        "headPipeline": {"status": pipeline} if pipeline else None,
        "reviewers": {
            "nodes": [
                {"username": name, "mergeRequestInteraction": {"reviewState": rs, "updatedAt": updated_at}}
                for name, rs in reviewers
            ]
        },
    }


def mine(n: dict[str, Any]) -> DashboardMr:
    return mr_from_node(n, mine=True)


def to_review(n: dict[str, Any]) -> DashboardMr:
    return mr_from_node(n, review_requested=True)


# --------------------------------------------------------------------------------------------------------------------
# The list rules


@pytest.mark.parametrize(
    ("reviewers", "expected"),
    [
        ((), "assigned_to_you"),
        ((("GitLabDuo", "REVIEWED"),), "assigned_to_you"),
        ((("bob", "REVIEWED"), ("carol", "UNREVIEWED")), "returned_to_you"),
        ((("bob", "REQUESTED_CHANGES"), ("GitLabDuo", "APPROVED")), "returned_to_you"),
        ((("bob", "UNREVIEWED"), ("carol", "APPROVED")), "waiting_for_approvals"),
        ((("bob", "REVIEW_STARTED"),), "waiting_for_approvals"),
        ((("bob", "UNAPPROVED"), ("GitLabDuo", "REVIEWED")), "waiting_for_approvals"),
        ((("bob", "APPROVED"), ("carol", "APPROVED")), "approved_by_others"),
        ((("bob", "APPROVED"), ("GitLabDuo", "REQUESTED_CHANGES")), "approved_by_others"),
    ],
)
def test_the_author_side_follows_the_other_reviewers_states(
    reviewers: tuple[tuple[str, str], ...], expected: str
) -> None:
    assert classify(mine(node(1, reviewers=reviewers)), ME, ACTION_BASED, True) == Lists(expected, None)


@pytest.mark.parametrize(
    ("state", "action", "role"),
    [
        ("UNREVIEWED", "reviews_requested", "reviews"),
        ("REVIEW_STARTED", "reviews_requested", "reviews"),
        ("UNAPPROVED", "reviews_requested", "reviews"),
        ("REVIEWED", "waiting_for_assignee", "reviews_inactive"),
        ("REQUESTED_CHANGES", "waiting_for_assignee", "reviews_inactive"),
        ("APPROVED", "approved_by_you", "reviews_inactive"),
    ],
)
def test_the_reviewer_side_follows_my_own_state(state: str, action: str, role: str) -> None:
    mr = to_review(node(2, author="bob", reviewers=[(ME, state), ("carol", "REVIEWED")]))
    assert classify(mr, ME, ACTION_BASED, True) == Lists(None, action)
    assert classify(mr, ME, ROLE_BASED, True) == Lists(None, role)


def test_hidden_drafts_leave_only_the_lists_without_reviewers() -> None:
    draft = mine(node(3, draft=True))
    reviewed_draft = mine(node(4, draft=True, reviewers=[("bob", "REVIEWED")]))
    assert classify(draft, ME, ACTION_BASED, False) == Lists()
    assert classify(draft, ME, ROLE_BASED, False) == Lists()
    assert classify(draft, ME, ROLE_BASED, True) == Lists("assigned", None)
    assert classify(reviewed_draft, ME, ACTION_BASED, False) == Lists("returned_to_you", None)


def test_an_mr_in_both_sets_is_in_one_list_per_side() -> None:
    both = node(5, reviewers=[(ME, "UNREVIEWED"), ("bob", "APPROVED")])
    [mr] = merge_sets([both], [both])
    assert mr.mine and mr.review_requested
    assert classify(mr, ME, ACTION_BASED, True) == Lists("waiting_for_approvals", "reviews_requested")


def test_the_layout_follows_the_preference_unless_pinned() -> None:
    assert effective_layout(None, "ROLE_BASED") == ROLE_BASED
    assert effective_layout(None, "ACTION_BASED") == ACTION_BASED
    assert effective_layout(None, None) == ACTION_BASED
    assert effective_layout("action", "ROLE_BASED") == ACTION_BASED
    assert effective_layout("role", None) == ROLE_BASED


# --------------------------------------------------------------------------------------------------------------------
# The list filter


def test_the_list_filter_selects_by_id_and_side() -> None:
    both = parse_list_filter(["returned_to_you,reviews_requested", "approved_by_you"], None)
    assert [d.id for d in both.selected(ACTION_BASED, include_merged=False)] == [
        "returned_to_you",
        "reviews_requested",
        "approved_by_you",
    ]
    reviewer = parse_list_filter(None, "reviewer")
    assert [d.id for d in reviewer.selected(ACTION_BASED, include_merged=False)] == [
        "reviews_requested",
        "waiting_for_assignee",
        "approved_by_you",
    ]
    assert [d.id for d in reviewer.selected(ROLE_BASED, include_merged=True)][-1] == "merged_recently_reviews"
    merged = parse_list_filter(["merged_recently_assigned"], None)
    assert [d.id for d in merged.selected(ROLE_BASED, include_merged=False)] == ["merged_recently_assigned"]


@pytest.mark.parametrize(
    ("lists", "side", "message"),
    [
        (["returned"], None, "unknown list returned"),
        (["returned_to_you"], "reviewer", "select no list"),
        ([], "owner", "--side must be one of"),
    ],
)
def test_a_bad_list_filter_is_refused(lists: list[str], side: str | None, message: str) -> None:
    with pytest.raises(ListFilterError, match=message):
        parse_list_filter(lists, side)


def test_a_list_of_the_other_layout_is_refused_with_the_layouts_lists() -> None:
    with pytest.raises(ListFilterError, match="not in the role_based layout, whose lists are reviews, assigned"):
        parse_list_filter(["returned_to_you"], None).selected(ROLE_BASED, include_merged=False)


# --------------------------------------------------------------------------------------------------------------------
# The diff


def snapshot(*mrs: DashboardMr, layout: str = ACTION_BASED, started_at: str = STARTED) -> MyMrsSnapshot:
    return MyMrsSnapshot(HOST, started_at, ME, layout, True, True, classify_all(mrs, ME, layout, True))


def kinds(events: Sequence[Any]) -> list[str]:
    return [e.kind for e in events]


def test_an_older_mr_that_becomes_mine_enters_its_lists_and_a_new_one_is_opened() -> None:
    prev = snapshot()
    later = snapshot(
        mine(node(1)),
        to_review(node(2, author="bob", reviewers=[(ME, "UNREVIEWED")])),
        mine(node(3, created_at="2026-11-20T10:05:00Z")),
    )
    _, events = diff_my_mrs(prev, later, {})
    assert [(e.kind, e.data["reference"], e.data.get("to")) for e in events] == [
        ("list_changed", "group/project!1", "assigned_to_you"),
        ("list_changed", "group/project!2", "reviews_requested"),
        ("mr_opened", "group/project!3", None),
    ]
    assert events[0].data["from"] is None and events[0].data["side"] == "author"
    assert events[2].data["lists"] == {"author": "assigned_to_you", "reviewer": None}


def test_a_review_state_change_reports_the_reviewer_and_the_list_it_moves_the_mr_to() -> None:
    before = mine(node(1, reviewers=[("bob", "UNREVIEWED")]))
    after = mine(node(1, reviewers=[("bob", "REQUESTED_CHANGES")], updated_at="2026-11-20T10:10:00Z"))
    _, events = diff_my_mrs(snapshot(before), snapshot(after), {})
    assert kinds(events) == ["review_state_changed", "list_changed", "mr_updated"]
    assert (events[0].data["reviewer"], events[0].data["from"], events[0].data["to"]) == (
        "bob",
        "unreviewed",
        "requested_changes",
    )
    assert (events[1].data["from"], events[1].data["to"]) == ("waiting_for_approvals", "returned_to_you")
    assert "changes" not in events[2].data


def test_mr_updated_names_the_head_pipeline_status_and_keeps_the_settled_merge_status() -> None:
    before = mine(node(1, pipeline="RUNNING"))
    after = replace(mine(node(1, pipeline="FAILED")), detailed_merge_status="checking")
    new, events = diff_my_mrs(snapshot(before), snapshot(after), {})
    assert kinds(events) == ["mr_updated"]
    assert events[0].data["changes"] == {"pipeline_status": ["running", "failed"]}
    assert new.entries[0].mr.detailed_merge_status == "mergeable"


def test_an_mr_that_left_the_open_set_is_merged_closed_or_gone() -> None:
    merged = mine(node(1))
    closed = to_review(node(2, author="bob", reviewers=[(ME, "APPROVED")]))
    unassigned = to_review(node(3, author="bob", reviewers=[(ME, "UNREVIEWED")]))
    deleted = mine(node(4))
    departed = {
        merged.gid: ("merged", "2026-11-20T10:20:00Z"),
        closed.gid: ("closed", None),
        unassigned.gid: ("opened", None),
    }
    new, events = diff_my_mrs(snapshot(merged, closed, unassigned, deleted), snapshot(), departed)
    assert new.entries == ()
    assert [(e.kind, e.data.get("reason") or e.data.get("merged_list")) for e in events] == [
        ("mr_merged", "merged_recently_assigned"),
        ("mr_closed", None),
        ("list_changed", "review_request_removed"),
        ("list_changed", "gone"),
    ]
    assert events[0].data["merged_at"] == "2026-11-20T10:20:00Z" and events[0].data["state"] == "merged"
    assert events[2].data["from"] == "reviews_requested" and events[2].data["to"] is None


def test_without_asking_gitlab_an_mr_that_left_is_kept_for_the_next_poll() -> None:
    gone = mine(node(1))
    new, events = diff_my_mrs(snapshot(gone), snapshot(), None)
    assert events == [] and [e.mr.reference for e in new.entries] == ["group/project!1"]


def test_the_snapshot_survives_the_round_trip_through_json() -> None:
    snap = snapshot(mine(node(1, reviewers=[("bob", "APPROVED")])), to_review(node(2, reviewers=[(ME, "REVIEWED")])))
    assert from_jsonable(MyMrsSnapshot, json.loads(json.dumps(to_jsonable(snap)))) == snap


# --------------------------------------------------------------------------------------------------------------------
# The poller


def dashboard_page(
    authored: list[dict[str, Any]], reviewing: list[dict[str, Any]], *, list_type: str = "ACTION_BASED"
) -> dict[str, Any]:
    page = {"pageInfo": {"hasNextPage": False, "endCursor": "c1"}}
    return {
        "data": {
            "duoBot": {"username": "GitLabDuo"},
            "currentUser": {
                "username": ME,
                "userPreferences": {"listType": list_type, "showDrafts": True},
                "authored": {**page, "nodes": authored},
                "reviewing": {**page, "nodes": reviewing},
            },
        }
    }


def merged_page(authored: list[dict[str, Any]], reviewing: list[dict[str, Any]]) -> dict[str, Any]:
    page = {"pageInfo": {"hasNextPage": False, "endCursor": None}}
    return {
        "data": {"currentUser": {"authored": {**page, "nodes": authored}, "reviewing": {**page, "nodes": reviewing}}}
    }


def test_the_poller_reads_the_baseline_then_reports_what_moved(fake: FakeGlab, ctx: PollContext) -> None:
    review = node(2, author="bob", reviewers=[(ME, "UNREVIEWED")])
    fake.routes = [
        Route("query MyMergeRequests(", dashboard_page([node(1)], [review])),
        Route("MyMergedMergeRequests", merged_page([node(9, state="merged", merged_at="2026-11-19T10:00:00Z")], [])),
    ]
    poller = MyMrsPoller(REF, ctx, PollerOptions(frozenset()))
    assert asyncio.run(poller.poll()) == []
    assert len(fake.calls) == 2
    summary = poller.summary()
    assert summary["lists"]["assigned_to_you"] == 1 and summary["lists"]["reviews_requested"] == 1
    assert summary["merged_2_weeks"] == {"merged_recently_assigned": 1, "merged_recently_reviews": 0}
    assert "warning" not in summary

    fake.calls.clear()
    approved = node(2, author="bob", reviewers=[(ME, "APPROVED")], updated_at="2026-11-20T11:00:00Z")
    fake.routes = [
        Route("query MyMergeRequests(", dashboard_page([], [approved])),
        Route("MyMergeRequestStates", {"data": {"m0": {"id": "gid://gitlab/MergeRequest/1001", "state": "merged"}}}),
    ]
    events = asyncio.run(poller.poll())
    assert kinds(events) == ["review_state_changed", "list_changed", "mr_updated", "mr_merged"]
    assert len(fake.calls) == 2
    assert poller.expected_cost() == 2.0
    assert poller.next_delay() == 900.0
    poller.set_options(PollerOptions(frozenset(), interval=120.0))
    assert poller.next_delay() == 120.0


def test_a_side_with_more_pages_is_read_alone_on_the_next_request(fake: FakeGlab, ctx: PollContext) -> None:
    first = dashboard_page([node(1)], [])
    first["data"]["currentUser"]["authored"]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
    second = {"data": {"currentUser": {"username": ME, "authored": {"pageInfo": {}, "nodes": [node(2)]}}}}
    fake.routes = [
        Route("query MyMergeRequests(", second, contains='"authoredAfter":"next"'),
        Route("query MyMergeRequests(", first),
        Route("MyMergedMergeRequests", merged_page([], [])),
    ]
    poller = MyMrsPoller(REF, ctx, PollerOptions(frozenset()))
    asyncio.run(poller.poll())
    follow_up = fake.calls[1].variables
    assert (follow_up["withAuthored"], follow_up["withReviewing"], follow_up["withUser"]) == (True, False, False)
    assert poller.snapshot is not None
    assert [e.mr.iid for e in poller.snapshot.entries] == [1, 2]


def test_the_source_takes_the_shortest_interval_and_refuses_one_below_a_minute() -> None:
    source = GlabSource()
    target = target_of(REF)
    assert source.check_options(target, {"interval": 120}) == {"interval": 120.0}
    assert source.check_options(target, {}) == {"interval": 900.0}
    with pytest.raises(ValueError, match="at least 60"):
        source.check_options(target, {"interval": 30})
    with pytest.raises(ValueError, match="at least 60"):
        source.check_options(target, {"logs": "all"})


# --------------------------------------------------------------------------------------------------------------------
# The refs


def test_the_dashboard_ref_takes_a_host_a_url_or_the_cwd_then_glabs_default() -> None:
    assert parse_dashboard_ref("gitlab.example.com").key == "my-mrs gitlab.example.com"
    url = parse_dashboard_ref("https://gitlab.example.com/dashboard/merge_requests?assignee=x", "role")
    assert url.key == "my-mrs gitlab.example.com layout=role"
    assert ResourceRef.from_wire(url.to_wire()) == url
    with pytest.raises(RefError, match="expected a GitLab host"):
        parse_dashboard_ref("group/project")

    def outside_a_repo(args: Sequence[str]) -> ProcessResult:
        if list(args[:2]) == ["repo", "view"]:
            return ProcessResult(1, b"", b"no git remote")
        return ProcessResult(0, b"gitlab.example.org\n", b"")

    assert parse_dashboard_ref(None, runner=outside_a_repo).host == "gitlab.example.org"
    in_a_repo = FakeGlab(repo_view={"web_url": "https://gitlab.example.net/group/project", "path_with_namespace": "x"})
    assert parse_dashboard_ref(None, runner=in_a_repo.sync).host == "gitlab.example.net"


def test_the_dashboard_dir_mirrors_the_url(tmp_path: Path) -> None:
    assert resource_dir(tmp_path, REF) == tmp_path / HOST / "dashboard" / "merge_requests"
    pinned = ResourceRef("my-mrs", HOST, "", layout="role")
    assert resource_dir(tmp_path, pinned) == tmp_path / HOST / "dashboard" / "merge_requests" / "layout-role"


# --------------------------------------------------------------------------------------------------------------------
# glab-my-mrs


@pytest.fixture
def one_shot(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> FakeGlab:
    fake = FakeGlab(
        [
            Route(
                "query MyMergeRequests(",
                dashboard_page(
                    [node(1, reviewers=[("bob", "REVIEWED")]), node(3)],
                    [node(2, author="bob", reviewers=[(ME, "UNREVIEWED")], pipeline=None)],
                ),
            ),
            Route("MyMergedMergeRequests", merged_page([], [node(7, author="bob", merged_at="2026-11-18T08:00:00Z")])),
        ]
    )
    monkeypatch.setattr(my_mrs_cli, "standalone_client", lambda host: GitLabClient(host, runner=fake, clock=clock))
    return fake


def test_glab_my_mrs_prints_every_open_list(one_shot: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    assert my_mrs_cli.main([HOST]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"{HOST} · {ME} · action_based · drafts shown"
    assert "== returned_to_you (author) · 1 · Returned to you" in out
    assert "  group/project!1 · by alice · pipeline success · reviewers bob reviewed · updated " in out
    assert "== reviews_requested (reviewer) · 1 · Review requested" in out
    assert "group/project!2 · by bob · pipeline none · reviewers alice unreviewed" in out
    assert "== approved_by_others (author) · 0" in out
    assert "merged_recently" not in out
    assert len(one_shot.calls) == 1


def test_glab_my_mrs_shows_only_the_lists_asked_for_as_json(
    one_shot: FakeGlab, capsys: pytest.CaptureFixture[str]
) -> None:
    assert my_mrs_cli.main([HOST, "--list", "returned_to_you,merged_recently_reviews", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(entry["id"], entry["count"]) for entry in data["lists"]] == [
        ("returned_to_you", 1),
        ("merged_recently_reviews", 1),
    ]
    mr = data["lists"][0]["merge_requests"][0]
    assert mr["reference"] == "group/project!1" and mr["lists"] == {"author": "returned_to_you", "reviewer": None}
    assert mr["reviewers"] == [{"username": "bob", "state": "reviewed"}]
    assert data["lists"][1]["merge_requests"][0]["merged_at"] == "2026-11-18T08:00:00Z"
    assert len(one_shot.calls) == 2


def test_glab_my_mrs_side_filter(one_shot: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    assert my_mrs_cli.main([HOST, "--side", "reviewer"]) == 0
    headers = [line.split(" · ")[0] for line in capsys.readouterr().out.splitlines() if line.startswith("==")]
    assert headers == [
        "== reviews_requested (reviewer)",
        "== waiting_for_assignee (reviewer)",
        "== approved_by_you (reviewer)",
    ]


def test_glab_my_mrs_refuses_a_list_of_the_other_layout(one_shot: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    assert my_mrs_cli.main([HOST, "--list", "reviews_inactive"]) == 2
    assert "pass --layout" in capsys.readouterr().err
    one_shot.calls.clear()
    assert my_mrs_cli.main([HOST, "--layout", "action", "--list", "reviews_inactive"]) == 2
    assert one_shot.calls == []


def test_glab_my_mrs_role_layout(one_shot: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    assert my_mrs_cli.main([HOST, "--layout", "role", "--list", "assigned,reviews"]) == 0
    out = capsys.readouterr().out
    assert "== reviews (reviewer) · 1" in out and "== assigned (author) · 2" in out


# --------------------------------------------------------------------------------------------------------------------
# glab-watch my-mrs


def event(kind: str, reference: str, **data: Any) -> Event:
    return Event(kind, REF.key, 1, STARTED, {"reference": reference, **data})


def test_the_watch_reports_only_mrs_in_the_selected_lists_before_or_after() -> None:
    lists = parse_list_filter(["returned_to_you"], None)
    events = [
        event("watch_started", "-"),
        event("list_changed", "g/p!1", lists={"author": "waiting_for_approvals"}, **{"from": "returned_to_you"}),
        event("review_state_changed", "g/p!1", lists={"author": "waiting_for_approvals"}, to="unreviewed"),
        event("mr_updated", "g/p!2", lists={"author": "assigned_to_you", "reviewer": None}),
        event("mr_merged", "g/p!3", lists={"author": None}, merged_list="merged_recently_assigned"),
        event("mr_updated", "g/p!4", lists={"author": "returned_to_you"}),
    ]
    kept = filter_my_mrs_events(events, lists)
    assert [(e.kind, e.data["reference"]) for e in kept] == [
        ("watch_started", "-"),
        ("list_changed", "g/p!1"),
        ("review_state_changed", "g/p!1"),
        ("mr_updated", "g/p!4"),
    ]


class NoFetches:
    def fetch_job_log(self, host: str, project: str, job_id: int) -> Any:
        raise AssertionError

    def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> Any:
        raise AssertionError

    def fetch_dump(self, ref: ResourceRef) -> Any:
        raise AssertionError


def test_the_watch_writes_the_selected_lists_into_state_json(tmp_path: Path) -> None:
    status = StatusOutput("glab-watch: ")
    writer = ResourceWriter(tmp_path, REF, NoFetches(), "none", status, ListFilter(side="author"))
    snap = snapshot(mine(node(1)), to_review(node(2, author="bob", reviewers=[(ME, "UNREVIEWED")])))
    writer.snapshot(to_jsonable(snap))
    state = json.loads((resource_dir(tmp_path, REF) / "state.json").read_text(encoding="utf-8"))
    assert state["resource"] == "my-mrs gitlab.example.com"
    assert [(entry["id"], entry["count"]) for entry in state["dashboard"]["lists"]] == [
        ("returned_to_you", 0),
        ("assigned_to_you", 1),
        ("waiting_for_approvals", 0),
        ("approved_by_others", 0),
    ]
    started = Event("watch_started", REF.key, 1, STARTED, {"summary": MyMrsPoller.summarize(REF, snap)})
    [attached] = writer.attach([started])
    assert attached.data["summary"]["lists"] == {
        "returned_to_you": 0,
        "assigned_to_you": 1,
        "waiting_for_approvals": 0,
        "approved_by_others": 0,
    }
    assert attached.data["summary"]["state"].endswith("state.json")


runner = CliRunner()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["my-mrs", HOST, "--interval=30s"], "--interval: at least 1m"),
        (["my-mrs", HOST, "--interval=soon"], "--interval: cannot read the duration"),
        (["my-mrs", HOST, "--layout=grid"], "--layout must be one of"),
        (["my-mrs", HOST, "--list=returned"], "unknown list returned"),
        (["my-mrs", HOST, "--side=owner"], "--side must be one of"),
        (["my-mrs", HOST, "--until=pushed"], "never produces 'pushed'"),
    ],
)
def test_bad_my_mrs_options_fail_before_anything_starts(args: list[str], message: str) -> None:
    result = runner.invoke(watch_app, args)
    assert result.exit_code == 2
    assert message in result.output
