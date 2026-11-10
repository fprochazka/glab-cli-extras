"""The glab-discussion write commands through the real transport, against the fake glab: what reaches `glab api`."""

from __future__ import annotations

import io
from typing import Any

import pytest

from fakeglab import FakeGlab, Route
from glab_cli_extras.cli.discussion import main
from glab_cli_extras.gitlab import sync_api

HOST = "gitlab.example.com"
MR_URL = f"https://{HOST}/group/project/-/merge_requests/7"
MR_PATH = "projects/group%2Fproject/merge_requests/7"
DISCUSSION = "abc" + "0" * 37
SIGNED_BODY = "Fixed in the last push.\n\n<!-- sdlc:mr-babysit -->\n"
"""A body as agents post it: the signature on the last line, then a newline. Both must reach GitLab byte for byte."""


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGlab:
    fake = FakeGlab()
    monkeypatch.setattr(sync_api, "RUNNER", fake.sync)
    monkeypatch.setattr(sync_api, "SLEEP", lambda seconds: None)
    monkeypatch.delenv("GLAB_DISCUSSION_WRITE_AS_DRAFT", raising=False)
    return fake


def run(*argv: str, stdin: str | None = None, monkeypatch: pytest.MonkeyPatch | None = None) -> None:
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    main([*argv, "--mr-url", MR_URL])


def writes(fake: FakeGlab) -> list[tuple[str, str, Any]]:
    return [(c.method, c.endpoint, c.body) for c in fake.calls if c.method != "GET"]


def test_a_reply_goes_as_json_on_stdin_byte_for_byte(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.routes = [Route(f"{MR_PATH}/discussions/{DISCUSSION}/notes", {"id": 501}, status=201)]
    run("write", "--reply-to", DISCUSSION, "--body", "-", stdin=SIGNED_BODY, monkeypatch=monkeypatch)
    call = fake.calls[0]
    assert (call.method, call.endpoint) == ("POST", f"{MR_PATH}/discussions/{DISCUSSION}/notes")
    assert call.stdin is not None and call.body == {"body": SIGNED_BODY}
    assert "--input" in call.args and "-f" not in call.args and "-F" not in call.args
    assert capsys.readouterr().out == f"Replied to discussion {DISCUSSION} (note:501) - {MR_URL}#note_501\n"


def test_a_failed_write_is_never_retried(fake: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [Route(f"{MR_PATH}/discussions", {"message": "502 Bad Gateway"}, status=502)]
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--body", "A new point.")
    assert exit_info.value.code == 1
    assert len(fake.calls) == 1
    assert "HTTP 502" in capsys.readouterr().err


def test_resolve_sends_a_json_boolean_and_names_the_mr(fake: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [Route(f"{MR_PATH}/discussions/{DISCUSSION}", {"id": DISCUSSION, "notes": [{"resolvable": True}]})]
    run("resolve", DISCUSSION)
    assert writes(fake) == [("PUT", f"{MR_PATH}/discussions/{DISCUSSION}", {"resolved": True})]
    assert capsys.readouterr().out == f"Resolved discussion {DISCUSSION} - {MR_URL}\n"


def test_editing_an_inline_draft_sends_its_position_back(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    position = {
        "position_type": "text",
        "base_sha": "a" * 40,
        "start_sha": "a" * 40,
        "head_sha": "b" * 40,
        "old_path": "app.py",
        "new_path": "app.py",
        "old_line": None,
        "new_line": 12,
        "line_range": None,
        "width": None,
    }
    fake.routes = [Route(f"{MR_PATH}/draft_notes/15", {"id": 15, "note": "Old.", "position": position})]
    run("edit", "draft:15", "--body", "-", stdin=SIGNED_BODY, monkeypatch=monkeypatch)
    kept = {k: v for k, v in position.items() if k not in ("line_range", "width")}
    assert writes(fake) == [("PUT", f"{MR_PATH}/draft_notes/15", {"note": SIGNED_BODY, "position": kept})]
    assert capsys.readouterr().out == f"Edited draft:15 - {MR_URL}\n"


def test_submit_review_is_a_command_only_note_and_its_202_is_read(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str]
) -> None:
    state = {
        "currentUser": {"username": "me"},
        "project": {
            "mergeRequest": {
                "allowsMultipleReviewers": True,
                "reviewers": {"nodes": [{"username": "me", "mergeRequestInteraction": {"reviewState": "REVIEWED"}}]},
                "approvedBy": {"nodes": []},
                "mergeabilityChecks": [],
            }
        },
    }
    nothing_to_remove = {"errors": ["User has not requested changes for this merge request"]}
    fake.routes = [
        Route("draft_notes", []),
        Route("currentUser", {"data": state}),
        Route(
            "mergeRequestDestroyRequestedChanges", {"data": {"mergeRequestDestroyRequestedChanges": nothing_to_remove}}
        ),
        Route(f"{MR_PATH}/notes", {"commands_changes": {}, "summary": ["Submitted the current review."]}, status=202),
    ]
    run("drafts", "publish", "--verdict", "reviewed")
    assert [w for w in writes(fake) if w[1] != "graphql"] == [
        ("POST", f"{MR_PATH}/notes", {"body": "/submit_review reviewed"})
    ]
    out = capsys.readouterr().out
    assert "GitLab: Submitted the current review." in out and "Set your reviewer state to reviewed." in out


TAKEN = {"message": {"author_id": ["has already been taken"]}}
"""GitLab's 400 for a second draft reply of one user in one thread."""


def test_a_second_draft_reply_in_a_thread_names_the_draft_to_edit_or_delete(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str]
) -> None:
    drafts = [
        {"id": 77, "note": "Other.", "discussion_id": None},
        {"id": 78, "note": "1st", "discussion_id": DISCUSSION},
    ]
    fake.routes = [
        Route(f"{MR_PATH}/draft_notes", TAKEN, status=400, contains="-X POST"),
        Route(f"{MR_PATH}/draft_notes", drafts),
    ]
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--draft", "--reply-to", DISCUSSION, "--body", "Second.")
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert f"you already have a draft reply (draft:78) in discussion {DISCUSSION}" in err
    assert "'glab-discussion edit draft:78 --body - < file'" in err and "'glab-discussion delete draft:78'" in err
    assert len(writes(fake)) == 1


def test_a_second_draft_reply_still_explains_itself_when_the_drafts_cannot_be_listed(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.routes = [
        Route(f"{MR_PATH}/draft_notes", TAKEN, status=400, contains="-X POST"),
        Route(f"{MR_PATH}/draft_notes", {"message": "403 Forbidden"}, status=403),
    ]
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--draft", "--reply-to", DISCUSSION, "--body", "Second.")
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "you already have a draft reply in discussion" in err and "Find it with 'glab-discussion read'" in err


def test_a_note_is_a_plain_comment_on_the_notes_endpoint(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # GLAB_DISCUSSION_WRITE_AS_DRAFT does not apply: a plain note is never a draft.
    monkeypatch.setenv("GLAB_DISCUSSION_WRITE_AS_DRAFT", "true")
    fake.routes = [Route(f"{MR_PATH}/notes", {"id": 601, "body": SIGNED_BODY}, status=201)]
    run("write", "--note", "--body", "-", stdin=SIGNED_BODY, monkeypatch=monkeypatch)
    assert writes(fake) == [("POST", f"{MR_PATH}/notes", {"body": SIGNED_BODY})]
    assert capsys.readouterr().out == f"Created note:601 - {MR_URL}#note_601\n"


def test_a_note_of_quick_actions_only_reports_what_gitlab_did(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.routes = [Route(f"{MR_PATH}/notes", {"commands_changes": {}, "summary": ["Added ~bug label."]}, status=202)]
    run("write", "--note", "--body", "/label ~bug")
    out = capsys.readouterr().out
    assert "GitLab: Added ~bug label." in out and "created no note" in out


@pytest.mark.parametrize(
    ("options", "named"),
    [
        (["--reply-to", DISCUSSION], "--reply-to"),
        (["--file", "app.py", "--new-line", "3"], "--file, --new-line"),
        (["--old-line", "3"], "--old-line"),
        (["--commit", "abc"], "--commit"),
        (["--draft"], "--draft"),
        (["--draft", "--reply-to", DISCUSSION, "--resolve"], "--reply-to, --draft, --resolve"),
    ],
)
def test_a_note_refuses_the_options_of_threads_diff_notes_and_drafts(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], options: list[str], named: str
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--note", "--body", "Text.", *options)
    assert exit_info.value.code == 2
    assert f"--note posts a plain comment at once, so it does not take {named}." in capsys.readouterr().err
    assert fake.calls == []


def test_a_note_takes_no_draft(fake: FakeGlab) -> None:
    fake.routes = [Route(f"{MR_PATH}/notes", {"id": 602}, status=201)]
    run("write", "--note", "--no-draft", "--body", "Text.")
    assert writes(fake) == [("POST", f"{MR_PATH}/notes", {"body": "Text."})]
