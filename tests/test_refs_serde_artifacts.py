from __future__ import annotations

from pathlib import Path

import pytest

from fakeglab import FakeGlab
from glab_cli_extras.discussion.models import UserInfo
from glab_cli_extras.gitlab.refs import RefError, ResourceRef, parse_id_ref, parse_repo_ref
from glab_cli_extras.paths import project_dir, sanitize_part
from glab_cli_extras.resources.mr import MrSnapshot, parse_mr, parse_threads
from glab_cli_extras.resources.pipeline import PipelineSnapshot, parse_pipeline
from glab_cli_extras.serde import from_jsonable, stable_hash, to_jsonable
from glab_cli_extras.store.threads import ThreadStore, render_thread
from shapes import HOST, PROJECT, gql_job, gql_mr, gql_pipeline, gql_thread, rest_discussion

REPO_VIEW = {"id": 321, "path_with_namespace": PROJECT, "web_url": f"https://{HOST}/{PROJECT}"}


def test_urls_need_no_glab_call() -> None:
    fake = FakeGlab()
    ref = parse_id_ref("mr", f"https://{HOST}/group/sub/project/-/merge_requests/42#note_1", fake.sync)
    assert ref == ResourceRef("mr", HOST, "group/sub/project", 42)
    assert ref.key == f"mr {HOST}/group/sub/project!42"
    pipeline = parse_id_ref("pipeline", f"https://{HOST}/{PROJECT}/-/pipelines/5001", fake.sync)
    assert pipeline.id == 5001
    repo = parse_repo_ref("branch", f"https://{HOST}/{PROJECT}.git", "main", fake.sync)
    assert repo.key == f"branch {HOST}/{PROJECT}:main"
    assert fake.calls == []


def test_repo_names_are_resolved_by_glab() -> None:
    fake = FakeGlab(repo_view=REPO_VIEW)
    assert parse_id_ref("mr", "group/project!42", fake.sync) == ResourceRef("mr", HOST, PROJECT, 42)
    assert fake.calls[-1].args == ["repo", "view", "group/project", "-F", "json"]
    parse_id_ref("mr", "!42", fake.sync)
    assert fake.calls[-1].args == ["repo", "view", "-F", "json"]
    assert parse_repo_ref("mrs", None, runner=fake.sync).project == PROJECT


def test_ref_errors() -> None:
    with pytest.raises(RefError, match="no git remotes found"):
        parse_id_ref("mr", "42", FakeGlab().sync)
    with pytest.raises(RefError, match="not a pipeline URL"):
        parse_id_ref("pipeline", f"https://{HOST}/{PROJECT}/-/merge_requests/1", FakeGlab().sync)
    with pytest.raises(RefError, match="cannot read"):
        parse_id_ref("mr", "group/project#42", FakeGlab().sync)
    with pytest.raises(RefError):
        ResourceRef.from_wire({"kind": "mr", "host": HOST, "project": PROJECT})


def test_wire_round_trip() -> None:
    ref = ResourceRef("branch", HOST, PROJECT, branch="feature/x")
    assert ResourceRef.from_wire(ref.to_wire()) == ref


def test_snapshots_survive_json() -> None:
    pipeline = parse_pipeline(gql_pipeline(5001, "running", [gql_job(1, "t", "failed", trace_size=12)]), HOST, PROJECT)
    assert from_jsonable(PipelineSnapshot, to_jsonable(pipeline)) == pipeline
    node = gql_mr(threads=[gql_thread("aaa", [(1, "2026-11-09T09:00:00Z", "reviewer-one")])])["project"]["mergeRequest"]
    mr = parse_mr(node, PROJECT, parse_threads(node["discussions"]["nodes"]))
    restored = from_jsonable(MrSnapshot, to_jsonable(mr))
    assert restored == mr and stable_hash(restored) == stable_hash(mr)
    with pytest.raises(ValueError):
        from_jsonable(MrSnapshot, {"iid": "not a number"})


USERS = {
    "reviewer-one": UserInfo(0, "reviewer-one", "Reviewer One", False),
    "review-bot": UserInfo(0, "review-bot", "Review Bot", True),
    "service_account_group_1a2b": UserInfo(0, "service_account_group_1a2b", "Lint Service", True),
}
MR_URL = f"https://{HOST}/{PROJECT}/-/merge_requests/42"


def test_thread_files_are_rewritten_only_on_change(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path / "threads")
    discussion = rest_discussion("abc123def4567890", [(1, "reviewer-one", "Please add a test.")])
    name, content = render_thread(discussion, MR_URL, USERS)
    first = store.write("abc123def4567890", name, content, sig="s1")
    assert first.status == "new" and first.path.name == "2026-11-09T09-30-reviewer-one-abc123def456.txt"
    assert store.write("abc123def4567890", name, content).status == "unchanged"
    discussion["notes"].append(rest_discussion("x", [(2, "author-one", "Added.")])["notes"][0])
    name, content = render_thread(discussion, MR_URL, USERS)
    assert store.write("abc123def4567890", name, content, sig="s2").status == "updated"
    assert "@author-one (note:2):\nAdded." in first.path.read_text()
    store.save()
    reopened = ThreadStore(tmp_path / "threads")
    assert reopened.sig("abc123def4567890") == "s2"
    assert reopened.write("abc123def4567890", name, content).status == "unchanged"
    reopened.remove("abc123def4567890")
    assert not first.path.exists()


def test_bot_authors_and_service_accounts_are_named_as_glab_discussion_names_them(tmp_path: Path) -> None:
    bot = rest_discussion("bot0000000000001", [(3, "review-bot", "Nothing gating.\n\n<!-- plugin:command -->\n")])
    name, content = render_thread(bot, MR_URL, USERS)
    assert name == "2026-11-09T09-30-bot-review-bot-bot000000000.txt"
    assert "@review-bot [BOT] (note:3):\nNothing gating.\n\n<!-- plugin:command -->\n" in content
    service = rest_discussion("svc0000000000001", [(4, "service_account_group_1a2b", "Quality gate passed.")])
    name, _ = render_thread(service, MR_URL, USERS)
    assert name == "2026-11-09T09-30-bot-Lint-Service-svc000000000.txt"


def test_a_name_read_back_from_the_index_never_deletes_outside_the_dir(tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("keep")
    store = ThreadStore(tmp_path / "threads")
    store.meta["evil"] = {"filename": "../outside.txt", "hash": "x"}
    store.remove("evil")
    assert (tmp_path / "outside.txt").read_text() == "keep"


def test_render_matches_the_glab_discussion_layout() -> None:
    _, text = render_thread(rest_discussion("d1", [(5, "reviewer-one", "Nit.")], resolved=True, diff=True), MR_URL, {})
    assert text.splitlines()[:7] == [
        "Discussion: d1",
        "Type: DiffNote",
        "File: src/login.py",
        "Line: new:17",
        f"Commit: {'b' * 40}",
        "Resolved: yes",
        "URL: https://gitlab.example.com/group/project/-/merge_requests/42#note_5",
    ]


def test_paths_never_escape_their_root(tmp_path: Path) -> None:
    assert sanitize_part("..") == "unknown"
    assert sanitize_part("a/../b") == "a---b"
    path = project_dir(tmp_path, "127.0.0.1:8080", "group/../etc")
    assert path.is_relative_to(tmp_path) and ".." not in path.parts
