"""The checks the glab-discussion write commands run before any write request, against the fake glab."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fakeglab import FakeGlab, Route
from glab_cli_extras.cli.discussion import main
from glab_cli_extras.gitlab import sync_api

HOST = "gitlab.example.com"
MR_URL = f"https://{HOST}/group/project/-/merge_requests/7"
MR_PATH = "projects/group%2Fproject/merge_requests/7"
DISCUSSION = "6a1f2b3c4d5e" + "0" * 28
VERSION = {"id": 3, "base_commit_sha": "b" * 40, "head_commit_sha": "h" * 40, "start_commit_sha": "s" * 40}
# old 10 removed; old 11 / new 10 context; new 42 added between context lines old 40 / new 41 and old 41 / new 43.
DIFF = "@@ -10,2 +10,1 @@\n-gone\n keep\n@@ -40,2 +41,3 @@\n a\n+b\n c\n"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGlab:
    fake = FakeGlab()
    monkeypatch.setattr(sync_api, "RUNNER", fake.sync)
    monkeypatch.setattr(sync_api, "SLEEP", lambda seconds: None)
    monkeypatch.delenv("GLAB_DISCUSSION_WRITE_AS_DRAFT", raising=False)
    return fake


def run(*argv: str) -> None:
    main([*argv, "--mr-url", MR_URL])


def fails(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit) as exit_info:
        run(*argv)
    assert exit_info.value.code == 1
    return capsys.readouterr().err


def writes(fake: FakeGlab) -> list[tuple[str, str, Any]]:
    return [(c.method, c.endpoint, c.body) for c in fake.calls if c.method != "GET"]


def diff_routes(old_path: str = "src/a.py", new_path: str = "src/a.py", note_type: str = "DiffNote") -> list[Route]:
    detail = {**VERSION, "diffs": [{"old_path": old_path, "new_path": new_path, "diff": DIFF}]}
    return [
        Route(f"{MR_PATH}/versions/3", detail),
        Route(f"{MR_PATH}/versions", [VERSION]),
        Route(f"{MR_PATH}/discussions", {"id": DISCUSSION, "notes": [{"id": 77, "type": note_type}]}, status=201),
    ]


@pytest.mark.parametrize("body", ["", "  \n\t"])
def test_an_empty_body_is_refused_before_any_request(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    for argv in (("write", "--body", body), ("edit", "note:5", "--body", body)):
        assert "the body is empty; nothing was sent" in fails(capsys, *argv)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        (("--new-line", "42"), {"new_line": 42}),
        (("--new-line", "10"), {"new_line": 10, "old_line": 11}),
        (("--old-line", "10"), {"old_line": 10}),
        (("--old-line", "40"), {"new_line": 41, "old_line": 40}),
        (("--new-line", "43", "--old-line", "41"), {"new_line": 43, "old_line": 41}),
    ],
)
def test_a_diff_note_is_anchored_on_the_diff(fake: FakeGlab, given: tuple[str, ...], sent: dict[str, int]) -> None:
    fake.routes = diff_routes()
    run("write", "--body", "Rename this.", "--file", "src/a.py", *given)
    ((_, endpoint, body),) = writes(fake)
    position = body["position"]
    assert {k: position[k] for k in ("new_line", "old_line") if k in position} == sent


def test_a_renamed_file_anchors_on_its_old_path(fake: FakeGlab) -> None:
    fake.routes = diff_routes(old_path="src/old.py", new_path="src/a.py")
    run("write", "--body", "Rename this.", "--file", "src/a.py", "--new-line", "42")
    position = writes(fake)[0][2]["position"]
    assert (position["old_path"], position["new_path"]) == ("src/old.py", "src/a.py")


@pytest.mark.parametrize(
    ("given", "message"),
    [
        (("--new-line", "99"), "line 99 of src/a.py is not in the diff of " + "h" * 40),
        (("--new-line", "10", "--old-line", "12"), "new line 10 of src/a.py is old line 11, not 12"),
        (("--new-line", "42", "--old-line", "41"), "new line 42 of src/a.py is an added line"),
    ],
)
def test_a_line_the_diff_cannot_take_is_refused_before_the_write(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], given: tuple[str, ...], message: str
) -> None:
    fake.routes = diff_routes()
    assert message in fails(capsys, "write", "--body", "x", "--file", "src/a.py", *given)
    assert writes(fake) == []


def test_a_diff_note_gitlab_made_a_general_comment_is_an_error(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.routes = diff_routes(note_type="DiscussionNote")
    err = fails(capsys, "write", "--body", "x", "--file", "src/a.py", "--new-line", "42")
    assert "GitLab created note:77 as a general comment, not as a diff note on src/a.py:42" in err


def test_a_discussion_id_prefix_is_resolved_from_the_mrs_files(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    state_dir = tmp_path / "base" / HOST / "group" / "project" / "-" / "merge_requests" / "7"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(json.dumps({"snapshot": {"threads": [{"id": DISCUSSION}]}}))
    fake.routes = [Route(f"{MR_PATH}/discussions/{DISCUSSION}/notes", {"id": 9}, status=201)]
    run("write", "--body", "Done.", "--reply-to", DISCUSSION[:12])
    assert f"Replied to discussion {DISCUSSION} (note:9)" in capsys.readouterr().out

    unknown = fails(capsys, "write", "--body", "Done.", "--reply-to", "ffffffffffff")
    assert f'no discussion ffffffffffff on {MR_URL}; use the full id from the "Discussion:" line' in unknown
    assert len(fake.calls) == 1


def test_resolve_refuses_a_thread_that_cannot_be_resolved(fake: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [Route(f"{MR_PATH}/discussions/{DISCUSSION}", {"id": DISCUSSION, "notes": [{"resolvable": False}]})]
    assert "is not resolvable (a general comment or a system note); reply to it instead" in fails(
        capsys, "resolve", DISCUSSION
    )
    assert writes(fake) == []


def test_resolve_explains_a_403_and_never_retries_it(fake: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [
        Route(f"{MR_PATH}/discussions/{DISCUSSION}", {"message": "403 Forbidden"}, status=403, contains="PUT"),
        Route(f"{MR_PATH}/discussions/{DISCUSSION}", {"id": DISCUSSION, "notes": [{"resolvable": True}]}),
    ]
    err = fails(capsys, "resolve", DISCUSSION)
    assert "is not resolvable" in err and "(GitLab answered HTTP 403)" in err
    assert [c.method for c in fake.calls] == ["GET", "PUT"]


@pytest.mark.parametrize(
    ("still_there", "message"),
    [(False, "is gone from"), (True, "still exists, so the error was transient")],
)
def test_a_reply_404_says_whether_the_discussion_is_gone(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], still_there: bool, message: str
) -> None:
    lookup = Route(f"{MR_PATH}/discussions/{DISCUSSION}", {"id": DISCUSSION}, status=200 if still_there else 404)
    fake.routes = [Route(f"{MR_PATH}/discussions/{DISCUSSION}/notes", {"message": "404 Not found"}, status=404), lookup]
    err = fails(capsys, "write", "--body", "Done.", "--reply-to", DISCUSSION)
    assert message in err and "The reply was not saved" in err
    assert [c.method for c in fake.calls] == ["POST", "GET"]
