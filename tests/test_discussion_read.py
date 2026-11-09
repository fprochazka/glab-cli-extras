"""glab-discussion read: incremental thread files in the MR's dir, the same dir glab-watch mr writes."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.cli.discussion import main as discussion_main
from glab_cli_extras.discussion import read
from glab_cli_extras.discussion.models import MrContext
from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.serde import to_jsonable
from glab_cli_extras.sync.mr import parse_mr, parse_threads
from glab_cli_extras.watch.sinks import StatusOutput
from glab_cli_extras.watch.writer import ResourceWriter
from shapes import HOST, PROJECT, gql_mr, gql_thread, rest_discussion

MR_URL = f"https://{HOST}/{PROJECT}/-/merge_requests/42"
CTX = MrContext(HOST, "group%2Fproject", PROJECT, 42, MR_URL)
A = "a" * 40
B = "b" * 40


def thread(sha: str, *notes: tuple[int, str, str], resolved: bool = False) -> dict[str, Any]:
    return gql_thread(sha, list(notes), resolved=resolved)


def draft(draft_id: int, note: str, discussion_id: str | None = None) -> dict[str, Any]:
    empty = {"base_sha": None, "new_path": None, "old_path": None}
    return {"id": draft_id, "author_id": 5, "note": note, "discussion_id": discussion_id, "position": empty}


class Gitlab:
    def __init__(self) -> None:
        self.threads = [thread(A, (1, "2026-11-09T09:00:00Z", "reviewer-one")), thread(B, (7, "t1", "reviewer-two"))]
        self.discussions = {
            A: rest_discussion(A, [(1, "reviewer-one", "Please add a test.")]),
            B: rest_discussion(B, [(7, "reviewer-two", "Rename this.")], diff=True),
        }
        self.drafts: list[dict[str, Any]] = []
        self.fake = FakeGlab()
        self.refresh()

    def refresh(self) -> None:
        self.fake.routes = [
            Route("MergeRequestSnapshot", {"data": gql_mr(threads=self.threads)}),
            Route("currentUser", {"data": {"currentUser": {"username": "me", "name": "Me", "bot": False}}}),
            Route("draft_notes", self.drafts),
            *[Route(f"discussions/{sha}", body) for sha, body in self.discussions.items()],
            Route("merge_requests/42/discussions", list(self.discussions.values())),
        ]


def run_read(gitlab: Gitlab, clock: Clock, capsys: pytest.CaptureFixture[str], dump: bool = True) -> list[str]:
    gitlab.refresh()
    gitlab.fake.calls.clear()
    capsys.readouterr()
    client = GitLabClient(HOST, runner=gitlab.fake, clock=clock)
    read.run_with_context(Namespace(dump=dump, no_dump=not dump, full=False), CTX, client)
    return capsys.readouterr().out.splitlines()


@pytest.fixture
def gitlab() -> Gitlab:
    return Gitlab()


def threads_dir(tmp_path: Path) -> Path:
    return tmp_path / "base" / HOST / "group" / "project" / "-" / "merge_requests" / "42" / "threads"


def test_first_run_writes_every_thread_and_a_second_run_fetches_nothing(
    gitlab: Gitlab, clock: Clock, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = run_read(gitlab, clock, capsys)
    assert out == [f"Discussions: {threads_dir(tmp_path)}/", "  synced 2 discussion threads"]
    files = sorted(p.name for p in threads_dir(tmp_path).glob("*.txt"))
    assert len(files) == 2
    state = json.loads((threads_dir(tmp_path).parent / "state.json").read_text())
    assert state["resource"] == f"mr {HOST}/{PROJECT}!42"

    out = run_read(gitlab, clock, capsys)
    assert out == [f"Discussions: {threads_dir(tmp_path)}/", "  (2 discussions up to date)"]
    assert [c.endpoint for c in gitlab.fake.calls] == [
        "graphql",
        "projects/group%2Fproject/merge_requests/42/draft_notes?per_page=100&page=1",
    ]


def test_a_changed_thread_is_the_only_one_fetched(
    gitlab: Gitlab, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    run_read(gitlab, clock, capsys)
    gitlab.threads[0] = thread(A, (1, "2026-11-09T09:00:00Z", "reviewer-one"), (2, "2026-11-09T10:00:00Z", "me"))
    gitlab.discussions[A]["notes"].append(rest_discussion("x", [(2, "me", "Added.")])["notes"][0])
    out = run_read(gitlab, clock, capsys)
    assert out[1].startswith("  updated: ") and out[2] == "  (1 discussions up to date)"
    rest = [c.endpoint for c in gitlab.fake.calls if c.endpoint != "graphql" and "draft_notes" not in c.endpoint]
    assert rest == [f"projects/group%2Fproject/merge_requests/42/discussions/{A}"]


def test_drafts_render_into_their_thread_and_as_their_own_file(
    gitlab: Gitlab, clock: Clock, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    run_read(gitlab, clock, capsys)
    gitlab.drafts = [draft(31, "My reply.", A), draft(32, "A new point.")]
    out = run_read(gitlab, clock, capsys)
    assert "  new: draft-32.txt" in out and any(line.startswith("  updated: ") for line in out)
    reply_thread = next(p for p in threads_dir(tmp_path).glob("*.txt") if A[:12] in p.name).read_text()
    assert "@me [DRAFT] (draft:31):\nMy reply." in reply_thread
    assert "@me [DRAFT] (draft:32):" in (threads_dir(tmp_path) / "draft-32.txt").read_text()
    # The draft is published: its file goes, and the thread file shows the note instead.
    gitlab.drafts = []
    out = run_read(gitlab, clock, capsys)
    assert "  deleted: draft-32.txt" in out


def test_a_deleted_thread_loses_its_file(gitlab: Gitlab, clock: Clock, capsys: pytest.CaptureFixture[str]) -> None:
    run_read(gitlab, clock, capsys)
    gitlab.threads = gitlab.threads[:1]
    out = run_read(gitlab, clock, capsys)
    assert any(line.startswith("  deleted: ") and B[:12] in line for line in out)


def test_no_dump_prints_the_threads(gitlab: Gitlab, clock: Clock, capsys: pytest.CaptureFixture[str]) -> None:
    out = run_read(gitlab, clock, capsys, dump=False)
    assert out[0] == f"Discussion: {A}" and f"Discussion: {B}" in out


def test_partial_context_flags_are_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        discussion_main(["read", "--mr-iid", "42"])
    assert exit_info.value.code == 2
    assert "--mr-iid needs --hostname and --project too, or pass --mr-url" in capsys.readouterr().err


def test_a_read_after_a_watch_finds_the_watchs_files_current(
    gitlab: Gitlab, clock: Clock, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    class Daemon:
        def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> dict[str, dict[str, Any]]:
            return {i: gitlab.discussions[i] for i in ids}

    node = gql_mr(threads=gitlab.threads)["project"]["mergeRequest"]
    snapshot = to_jsonable(parse_mr(node, PROJECT, parse_threads(node["discussions"]["nodes"])))
    watch = ResourceWriter(tmp_path / "base", ResourceRef("mr", HOST, PROJECT, 42), Daemon(), "failed", StatusOutput())  # type: ignore[arg-type]
    watch.snapshot(snapshot)
    written = {p.name: p.read_bytes() for p in threads_dir(tmp_path).glob("*.txt")}

    out = run_read(gitlab, clock, capsys)
    assert out == [f"Discussions: {threads_dir(tmp_path)}/", "  (2 discussions up to date)"]
    assert {p.name: p.read_bytes() for p in threads_dir(tmp_path).glob("*.txt")} == written
    assert [c.endpoint for c in gitlab.fake.calls if "discussions" in c.endpoint] == []


def test_a_body_with_a_signature_line_reaches_the_thread_file_byte_for_byte(
    gitlab: Gitlab, clock: Clock, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    signed = "Fixed in the last push.\n\n<!-- sdlc:mr-babysit -->\n"
    gitlab.discussions[A] = rest_discussion(A, [(1, "reviewer-one", signed)])
    run_read(gitlab, clock, capsys)
    content = next(p for p in threads_dir(tmp_path).glob("*.txt") if A[:12] in p.name).read_bytes()
    assert f"(note:1):\n{signed}\n".encode() in content
