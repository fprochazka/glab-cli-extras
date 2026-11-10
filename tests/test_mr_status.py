"""glab-mr-status: the contract of mr-state.py, which it replaces: the change ids, their lines, the scorecard, the
external statuses, the stored baseline, the probes, and the exit codes."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.cli import mr_status
from glab_cli_extras.gitlab import sync_api
from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.gitlab.refs import RefError, ResourceRef
from glab_cli_extras.status import diff, read
from glab_cli_extras.status.diff import Change, Details, details_lines, diff_snapshots, scorecard, settle
from glab_cli_extras.status.read import EXIT_OK, EXIT_READ_FAILED, MrRead, ReadOptions, run
from shapes import HOST, PROJECT, gql_mr, gql_thread, rest_discussion
from test_inspect import routes as pipeline_routes

MR_URL = f"https://{HOST}/{PROJECT}/-/merge_requests/42"
REF = ResourceRef("mr", HOST, PROJECT, 42)


def mr_object(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iid": 42,
        "project_id": 1,
        "target_project_id": 1,
        "web_url": MR_URL,
        "title": "Add thing",
        "author": {"username": "jane"},
        "state": "opened",
        "draft": True,
        "sha": "aaaaaaaa1111",
        "source_branch": "feature/login-form",
        "target_branch": "master",
        "diverged_commits_count": 0,
        "rebase_in_progress": False,
        "merge_status": "can_be_merged",
        "detailed_merge_status": "mergeable",
        "has_conflicts": False,
        "blocking_discussions_resolved": True,
        "assignees": [{"username": "jane"}],
        "reviewers": [],
        "labels": [],
        "user_notes_count": 3,
        "updated_at": "2026-09-21T10:00:00.000Z",
        "head_pipeline": {
            "id": 100,
            "project_id": 1,
            "status": "success",
            "sha": "aaaaaaaa1111",
            "web_url": "u",
            "created_at": "c",
            "updated_at": "u",
        },
    }
    base.update(overrides)
    return base


def snap(**overrides: Any) -> dict[str, Any]:
    s = diff.snapshot_from_mr(mr_object(**overrides), HOST, PROJECT)
    s["newest_note"] = {"id": 7, "updated_at": "2026-09-21T09:00:00.000Z", "author": "bot", "system": False}
    s["notes_probe"] = "exact"
    s["approvals"] = {"approved_by": [], "approvals_left": 1, "approved": False}
    return s


def count_only(s: dict[str, Any]) -> dict[str, Any]:
    s["notes_probe"], s["newest_note"] = "count-only", None
    return s


def ids(changes: list[Change]) -> list[str]:
    return [c.id for c in changes]


class TestDiff:
    def test_no_changes_without_a_baseline(self) -> None:
        assert diff_snapshots(None, snap()) == []

    def test_identical_snapshots_yield_nothing(self) -> None:
        assert diff_snapshots(snap(), snap()) == []

    def test_draft_and_state(self) -> None:
        assert ids(diff_snapshots(snap(), snap(draft=False, state="merged"))) == ["STATE_CHANGED", "DRAFT_CHANGED"]

    def test_pipeline_status_change_same_id(self) -> None:
        changes = diff_snapshots(snap(), snap(head_pipeline={"id": 100, "status": "failed", "sha": "aaaaaaaa1111"}))
        assert ids(changes) == ["PIPELINE_CHANGED"]
        assert "success -> failed" in changes[0].detail

    def test_new_pipeline_on_new_head_names_both(self) -> None:
        new = snap(sha="bbbbbbbb2222", head_pipeline={"id": 101, "status": "running", "sha": "bbbbbbbb2222"})
        changes = diff_snapshots(snap(), new)
        assert ids(changes) == ["HEAD_CHANGED", "PIPELINE_CHANGED"]
        assert "new pipeline 101" in changes[1].detail

    def test_failed_while_behind_is_called_out(self) -> None:
        new = snap(diverged_commits_count=4, head_pipeline={"id": 100, "status": "failed", "sha": "aaaaaaaa1111"})
        changes = diff_snapshots(snap(), new)
        assert "superseded base" in next(c for c in changes if c.id == "PIPELINE_CHANGED").detail
        assert "BEHIND_CHANGED" in ids(changes)

    def test_head_change_with_divergence_drop_hints_rebase(self) -> None:
        changes = diff_snapshots(snap(diverged_commits_count=3), snap(sha="bbbbbbbb2222", diverged_commits_count=0))
        assert "rebase likely" in next(c for c in changes if c.id == "HEAD_CHANGED").detail

    def test_transient_merge_status_is_not_a_change(self) -> None:
        assert diff_snapshots(snap(), snap(merge_status="checking", detailed_merge_status="checking")) == []

    def test_rebase_in_progress_holds_divergence_and_conflicts(self) -> None:
        new = snap(rebase_in_progress=True, diverged_commits_count=9, has_conflicts=True)
        assert diff_snapshots(snap(), new) == []

    def test_note_edit_same_id_is_reported_as_edit(self) -> None:
        new = snap()
        new["newest_note"] = {"id": 7, "updated_at": "2026-09-21T09:30:00.000Z", "author": "bot", "system": False}
        changes = diff_snapshots(snap(), new)
        assert ids(changes) == ["NOTES_CHANGED"]
        assert "edited or resolved" in changes[0].detail

    def test_new_note_is_reported_with_author(self) -> None:
        new = snap()
        new["newest_note"] = {"id": 8, "updated_at": "2026-09-21T09:30:00.000Z", "author": "alice", "system": False}
        assert "new note by @alice" in diff_snapshots(snap(), new)[0].detail

    def test_exact_probe_still_notices_a_deleted_note(self) -> None:
        # A note further down the list is deleted: the count drops while the newest note stays the same.
        changes = diff_snapshots(snap(), snap(user_notes_count=2))
        assert ids(changes) == ["NOTES_CHANGED"]
        assert "newest note stayed the same" in changes[0].detail

    def test_exact_probe_does_not_double_report_a_new_note(self) -> None:
        new = snap(user_notes_count=4)
        new["newest_note"] = {"id": 8, "updated_at": "2026-09-21T09:30:00.000Z", "author": "alice", "system": False}
        changes = diff_snapshots(snap(), new)
        assert ids(changes) == ["NOTES_CHANGED"]
        assert "new note by @alice" in changes[0].detail

    def test_count_only_probe_uses_count_then_updated_at(self) -> None:
        old = count_only(snap())
        assert ids(diff_snapshots(old, count_only(snap(user_notes_count=4)))) == ["NOTES_CHANGED"]
        changes = diff_snapshots(old, count_only(snap(updated_at="2026-09-21T11:00:00.000Z")))
        assert ids(changes) == ["NOTES_CHANGED"]
        assert "no other field explaining it" in changes[0].detail

    def test_count_only_probe_does_not_blame_notes_for_a_reviewer_assignment(self) -> None:
        new = count_only(snap(reviewers=[{"username": "bob"}], updated_at="2026-09-21T11:00:00.000Z"))
        assert ids(diff_snapshots(count_only(snap()), new)) == ["REVIEWERS_CHANGED"]

    def test_count_only_probe_does_not_blame_notes_for_a_label_or_merge_status_move(self) -> None:
        new = count_only(snap(labels=["blocked"], has_conflicts=True, updated_at="2026-09-21T11:00:00.000Z"))
        assert "NOTES_CHANGED" not in ids(diff_snapshots(count_only(snap()), new))

    def test_count_only_probe_does_not_blame_notes_for_a_push(self) -> None:
        new = count_only(snap(sha="bbbbbbbb2222", updated_at="2026-09-21T11:00:00.000Z", head_pipeline=None))
        assert "NOTES_CHANGED" not in ids(diff_snapshots(count_only(snap()), new))

    def test_a_deleted_newest_note_is_not_reported_as_a_new_one(self) -> None:
        new = snap()
        new["newest_note"] = {"id": 5, "updated_at": "2026-09-21T08:00:00.000Z", "author": "bot", "system": False}
        changes = diff_snapshots(snap(), new)
        assert ids(changes) == ["NOTES_CHANGED"]
        assert "newest note deleted" in changes[0].detail

    def test_head_changed_names_the_full_shas(self) -> None:
        old, new = snap(sha="1" * 40), snap(sha="2" * 40)
        head = next(c for c in diff_snapshots(old, new) if c.id == "HEAD_CHANGED")
        assert f"{'1' * 40} -> {'2' * 40}" in head.detail

    def test_unavailable_approvals_never_produce_an_approvals_change(self) -> None:
        old, new = snap(), snap()
        old["approvals"] = dict(diff.APPROVALS_UNAVAILABLE)
        new["approvals"] = {"approved_by": ["carol"], "approvals_left": 0, "approved": True}
        assert "APPROVALS_CHANGED" not in ids(diff_snapshots(old, new))

    def test_reviewers_labels_approvals(self) -> None:
        new = snap(reviewers=[{"username": "bob"}], labels=["blocked"])
        new["approvals"] = {"approved_by": ["carol"], "approvals_left": 0, "approved": True}
        changes = diff_snapshots(snap(), new)
        assert ids(changes) == ["REVIEWERS_CHANGED", "LABELS_CHANGED", "APPROVALS_CHANGED"]
        assert "added ['bob']" in changes[0].detail
        assert "+['carol']" in changes[2].detail

    def test_settle_keeps_last_settled_merge_status(self) -> None:
        assert settle(snap(), snap(merge_status="checking"))["merge_status"] == "can_be_merged"

    def test_first_read_records_a_transient_merge_status_verbatim(self) -> None:
        settled = settle(None, snap(merge_status="checking", detailed_merge_status="checking"))
        assert (settled["merge_status"], settled["detailed_merge_status"]) == ("checking", "checking")

    def test_a_settled_value_after_checking_is_reported(self) -> None:
        old = snap(merge_status="checking", detailed_merge_status="checking")
        new = snap(merge_status="cannot_be_merged", detailed_merge_status="not_approved")
        assert ids(diff_snapshots(old, new)) == ["MERGE_STATUS_CHANGED"]


class TestScorecard:
    def test_stacked_when_target_is_not_default(self) -> None:
        s = snap(target_branch="feature/base")
        s["default_branch"] = "master"
        assert "stacked on feature/base" in scorecard(s)

    def test_older_head_flag(self) -> None:
        assert "older head" in scorecard(snap(sha="bbbbbbbb2222"))

    def test_unavailable_approvals_are_not_rendered_as_nobody_approved(self) -> None:
        s = snap()
        s["approvals"] = None
        assert "approved by ?" in scorecard(s)
        s["approvals"] = dict(diff.APPROVALS_UNAVAILABLE)
        assert "approved by ?" in scorecard(s)
        s["approvals"] = {"approved_by": [], "approvals_left": 1, "approved": False}
        assert "approved by -" in scorecard(s)

    def test_the_whole_line(self) -> None:
        assert scorecard(snap()) == (
            f"{HOST}/{PROJECT}!42 · opened · draft · head aaaaaaaa · pipeline success · behind 0 · conflicts no"
            " · notes 3 · approved by - · updated 2026-09-21T10:00:00.000Z"
        )


def status(name: str, *, pipeline_id: int | None = None, state: str = "success") -> dict[str, Any]:
    return {"name": name, "status": state, "pipeline_id": pipeline_id}


class TestExternalStatuses:
    def test_pipeline_id_excludes_the_pipelines_own_statuses(self) -> None:
        rows = [status("build", pipeline_id=7), status("gate")]
        assert diff.external_statuses(rows, 7, set()) == [rows[1]]

    def test_trigger_jobs_are_excluded_even_though_jobs_json_omits_them(self) -> None:
        rows = [status("release-to-test: [a]", state="failed"), status("gate")]
        assert diff.external_statuses(rows, 7, {"build", "release-to-test: [a]"}) == [rows[1]]

    def test_a_status_from_an_older_pipeline_on_the_same_commit_is_external(self) -> None:
        rows = [status("build", pipeline_id=6)]
        assert diff.external_statuses(rows, 7, set()) == rows

    def test_job_names_union_jobs_and_bridges(self, tmp_path: Path) -> None:
        (tmp_path / "jobs.json").write_text(json.dumps([{"name": "build"}, {"name": "test"}]))
        (tmp_path / "bridges.json").write_text(json.dumps([{"name": "trigger"}]))
        assert diff.pipeline_job_names(tmp_path) == {"build", "test", "trigger"}

    def test_job_names_tolerate_a_missing_or_broken_dump(self, tmp_path: Path) -> None:
        (tmp_path / "jobs.json").write_text("{ not json")
        assert diff.pipeline_job_names(tmp_path) == set()


class TestDiscussionSummary:
    def render(self, summary: list[str]) -> str:
        return "\n".join(details_lines(Details(discussions_dir="/d", discussions_summary=summary)))

    def test_changed_files_are_listed(self) -> None:
        out = self.render(["  updated: a.txt", "  new: b.txt", "  deleted: c.txt", "  (3 discussions up to date)"])
        for line in ("    updated: a.txt", "    new: b.txt", "    deleted: c.txt"):
            assert line in out

    def test_an_unchanged_dump_reports_its_last_line(self) -> None:
        assert "    (5 discussions up to date)" in self.render(["  (5 discussions up to date)"])

    def test_a_failed_external_status_is_named(self) -> None:
        d = Details(pipeline_dir="/p", pipeline_summary_file="/p/summary.txt")
        d.failed_jobs = ["unit tests [test] script_failure"]
        d.external_statuses = [{"name": "gate", "status": "failed", "description": "2 issues", "target_url": "u"}]
        assert details_lines(d) == [
            "  pipeline dump: /p/ (summary.json, jobs.json, job-logs/) · summary text: /p/summary.txt",
            "    failed: unit tests [test] script_failure",
            "    failed external status: gate 2 issues u",
        ]


class TestStoredBaseline:
    def read(self, tmp_path: Path) -> MrRead:
        mr = MrRead(REF, tmp_path / "mr")
        mr.load(reset=False)
        return mr

    def test_commit_writes_the_settled_snapshot_and_a_reload_reads_it(self, tmp_path: Path) -> None:
        mr = self.read(tmp_path)
        assert mr.last is None
        mr.snapshot = snap()
        mr.commit()
        mr.snapshot = snap(merge_status="checking")
        mr.commit()
        saved = json.loads((tmp_path / "mr" / "status-baseline.json").read_text())
        assert saved["snapshot"]["merge_status"] == "can_be_merged"
        assert not [p for p in (tmp_path / "mr").iterdir() if p.name.endswith(".tmp")]
        assert self.read(tmp_path).last == saved["snapshot"]
        state = json.loads((tmp_path / "mr" / "state.json").read_text())
        assert state["resource"] == REF.key and state["status_read"]["snapshot"] == saved["snapshot"]

    def test_a_corrupt_baseline_starts_a_new_one_instead_of_crashing(self, tmp_path: Path) -> None:
        (tmp_path / "mr").mkdir()
        (tmp_path / "mr" / "status-baseline.json").write_text("{truncated")
        assert self.read(tmp_path).last is None

    def test_reset_forgets_the_baseline(self, tmp_path: Path) -> None:
        mr = self.read(tmp_path)
        mr.snapshot = snap()
        mr.commit()
        again = MrRead(REF, tmp_path / "mr")
        again.load(reset=True)
        assert again.last is None


class Gitlab:
    """A fake GitLab for whole runs: the REST probe, the threads, and the pipeline dump."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mr = mr_object(head_pipeline={**mr_object()["head_pipeline"], "id": 5001, "status": "failed"})
        self.notes: Any = [{"id": 7, "updated_at": "2026-09-21T09:00:00Z", "author": {"username": "bot"}}]
        self.notes_status = 200
        self.approvals: Any = {"approved_by": [{"user": {"username": "carol"}}], "approvals_left": 0, "approved": True}
        self.approvals_status = 200
        self.fake = FakeGlab()
        monkeypatch.setattr(sync_api, "RUNNER", self.fake.sync)
        self.clock = Clock()

    def refresh(self) -> None:
        sha = "a" * 40
        self.fake.routes = [
            Route("merge_requests/42/notes", self.notes, status=self.notes_status),
            Route("merge_requests/42/approvals", self.approvals, status=self.approvals_status),
            Route("MergeRequestSnapshot", {"data": gql_mr(threads=[gql_thread(sha, [(1, "t1", "reviewer-one")])])}),
            Route("currentUser", {"data": {"currentUser": {"username": "me", "name": "Me", "bot": False}}}),
            Route("draft_notes", []),
            Route(f"discussions/{sha}", rest_discussion(sha, [(1, "reviewer-one", "Please add a test.")])),
            Route("repository/commits/", [status("gate", state="failed"), status("unit tests", pipeline_id=5001)]),
            Route("merge_requests/42", self.mr),
            *pipeline_routes(),
            Route(f"projects/{PROJECT.replace('/', '%2F')}", {"default_branch": "master"}),
        ]

    def client(self, host: str) -> GitLabClient:
        return GitLabClient(host, runner=self.fake, clock=self.clock)

    def run(self, tmp_path: Path, **options: Any) -> tuple[int, str]:
        self.refresh()
        out = io.StringIO()
        opts = ReadOptions(base=tmp_path / "base", **{"comments": True, "pipeline": True, **options})
        return run([REF], opts, self.client, out), out.getvalue()


@pytest.fixture
def gitlab(monkeypatch: pytest.MonkeyPatch) -> Gitlab:
    return Gitlab(monkeypatch)


def mr_dir(tmp_path: Path) -> Path:
    return tmp_path / "base" / HOST / "group" / "project" / "-" / "merge_requests" / "42"


def test_a_first_read_is_a_baseline_with_the_snapshot_threads_pipeline_and_scorecard(
    gitlab: Gitlab, tmp_path: Path
) -> None:
    code, out = gitlab.run(tmp_path)
    assert code == EXIT_OK
    directory = mr_dir(tmp_path)
    pipeline = tmp_path / "base" / HOST / "group" / "project" / "-" / "pipelines" / "5001"
    lines = out.splitlines()
    assert lines[:3] == [
        f"== {HOST}/{PROJECT}!42",
        "  baseline established (no earlier state for this MR)",
        "  title: Add thing",
    ]
    assert f"  discussions: {directory / 'threads'}/ (one file per thread" in out
    assert "    synced 1 discussion threads" in lines
    dump = f"  pipeline dump: {pipeline}/ (summary.json, jobs.json, job-logs/) · summary text: {pipeline}/summary.txt"
    assert dump in lines
    assert "    failed: unit tests [test] script_failure" in lines
    assert "    failed external status: gate" in lines
    assert lines[-2].startswith(f"  scorecard: {HOST}/{PROJECT}!42 · opened · draft · head aaaaaaaa · pipeline failed")
    assert "approved by carol" in lines[-2]
    assert lines[-1] == f"state: {directory}"
    for name in ("mr.json", "status-baseline.json", "status-events.log", "state.json", "external-statuses.json"):
        assert (directory / name).exists(), name
    assert (pipeline / "summary.txt").exists()
    assert not (directory / "baseline.json").exists(), "the watcher's baseline belongs to the watcher"


def test_a_second_read_reports_the_changes_and_logs_them(gitlab: Gitlab, tmp_path: Path) -> None:
    gitlab.run(tmp_path, comments=False, pipeline=False)
    gitlab.mr = {**gitlab.mr, "draft": False, "updated_at": "2026-09-21T11:00:00.000Z"}
    code, out = gitlab.run(tmp_path, comments=False, pipeline=False)
    assert code == EXIT_OK
    assert out.splitlines()[:2] == [f"== {HOST}/{PROJECT}!42", "  DRAFT_CHANGED: ready"]
    log = (mr_dir(tmp_path) / "status-events.log").read_text().splitlines()
    assert [line.split("\t")[1:] for line in log] == [[f"{HOST}/{PROJECT}!42", "DRAFT_CHANGED", "ready"]]
    code, out = gitlab.run(tmp_path, comments=False, pipeline=False)
    assert "  no change since the last run" in out.splitlines()


def test_a_refused_notes_probe_reads_on_the_note_count_and_is_not_retried(gitlab: Gitlab, tmp_path: Path) -> None:
    gitlab.notes, gitlab.notes_status = {"message": "403 Forbidden"}, 403
    _, out = gitlab.run(tmp_path, comments=False, pipeline=False)
    assert "  newest note: unavailable (notes endpoint refused; probing on note count and updated_at only)" in out
    assert sum("/notes" in endpoint for endpoint in gitlab.fake.endpoints()) == 1


def test_an_mr_with_no_notes_is_an_exact_probe(gitlab: Gitlab, tmp_path: Path) -> None:
    gitlab.notes = []
    gitlab.run(tmp_path, comments=False, pipeline=False)
    baseline = json.loads((mr_dir(tmp_path) / "status-baseline.json").read_text())["snapshot"]
    assert (baseline["notes_probe"], baseline["newest_note"]) == ("exact", None)


def approvals_calls(gitlab: Gitlab) -> int:
    return sum("/approvals" in endpoint for endpoint in gitlab.fake.endpoints())


def test_refused_approvals_are_stored_as_unavailable_and_asked_again_only_when_the_mr_moves(
    gitlab: Gitlab, tmp_path: Path
) -> None:
    gitlab.approvals, gitlab.approvals_status = {"message": "403 Forbidden"}, 403
    _, out = gitlab.run(tmp_path, comments=False, pipeline=False)
    assert "approved by ?" in out
    gitlab.run(tmp_path, comments=False, pipeline=False)
    assert approvals_calls(gitlab) == 1
    gitlab.mr = {**gitlab.mr, "updated_at": "2026-09-21T11:00:00.000Z"}
    gitlab.approvals, gitlab.approvals_status = {"approved_by": [], "approvals_left": 1, "approved": False}, 200
    _, out = gitlab.run(tmp_path, comments=False, pipeline=False)
    assert approvals_calls(gitlab) == 2
    assert "approved by -" in out


def test_every_mr_failing_exits_five_with_a_read_failed_line(gitlab: Gitlab, tmp_path: Path) -> None:
    gitlab.refresh()
    gitlab.fake.routes = [Route("merge_requests/42", {"message": "404 Not found"}, status=404)]
    out = io.StringIO()
    code = run([REF], ReadOptions(tmp_path / "base", False, False), gitlab.client, out)
    assert code == EXIT_READ_FAILED
    lines = out.getvalue().splitlines()
    assert lines[0] == f"== {HOST}/{PROJECT}!42"
    assert lines[1].startswith("  READ_FAILED: probe failed:")
    assert lines[-1] == "result: nothing could be read; every MR failed (see the READ_FAILED lines above)"


def test_one_failed_mr_in_a_set_still_exits_zero(gitlab: Gitlab, tmp_path: Path) -> None:
    gitlab.refresh()
    other = ResourceRef("mr", HOST, "group/other", 9)
    out = io.StringIO()
    code = run([REF, other], ReadOptions(tmp_path / "base", False, False), gitlab.client, out)
    assert code == EXIT_OK
    assert "READ_FAILED" in out.getvalue()
    assert out.getvalue().splitlines()[-1] == f"state: {tmp_path / 'base'}"


def fail_current_branch(message: str) -> Callable[[], Any]:
    def fail() -> Any:
        raise RefError(message)

    return fail


def test_no_mr_for_the_current_branch_gets_the_guidance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = 'glab mr view failed: ERROR\n\n  No open merge request available for "master".'
    monkeypatch.setattr(mr_status, "current_branch_mr", fail_current_branch(message))
    assert mr_status.main([]) == 1
    assert "push the branch and open one" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--wait-for-state-change-timeout-minutes", "2"], ["not a ref at all!"]])
def test_usage_errors(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    if args[0].startswith("--"):
        with pytest.raises(SystemExit) as raised:
            mr_status.main(args)
        assert raised.value.code == 2
    else:
        assert mr_status.main(args) == 1
        assert "cannot read" in capsys.readouterr().err


def test_version_names_the_package(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        mr_status.main(["--version"])
    assert capsys.readouterr().out.startswith("glab-cli-extras ")


def test_the_help_names_the_files_and_the_output_lines_an_agent_reads() -> None:
    """The commands and skills that call glab-mr-status rely on --help for its output; no other doc describes it."""
    text = mr_status.build_parser().format_help()
    for name in (
        read.MR_FILE,
        read.EXTERNAL_STATUSES_FILE,
        "threads/",
        read.BASELINE_FILE,
        read.EVENTS_LOG,
        "state:",
        "discussions:",
        "pipeline dump:",
        "failed external status:",
        "read only the thread files",
    ):
        assert name in text
