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
    fake.routes = [Route(f"{MR_PATH}/discussions/abc/notes", {"id": 501}, status=201)]
    run("write", "--reply-to", "abc", "--body", "-", stdin=SIGNED_BODY, monkeypatch=monkeypatch)
    call = fake.calls[0]
    assert (call.method, call.endpoint) == ("POST", f"{MR_PATH}/discussions/abc/notes")
    assert call.stdin is not None and call.body == {"body": SIGNED_BODY}
    assert "--input" in call.args and "-f" not in call.args and "-F" not in call.args
    assert capsys.readouterr().out == f"Replied to discussion abc (note:501) - {MR_URL}#note_501\n"


def test_a_failed_write_is_never_retried(fake: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [Route(f"{MR_PATH}/discussions", {"message": "502 Bad Gateway"}, status=502)]
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--body", "A new point.")
    assert exit_info.value.code == 1
    assert len(fake.calls) == 1
    assert "HTTP 502" in capsys.readouterr().err


def test_resolve_sends_a_json_boolean_and_names_the_mr(fake: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [Route(f"{MR_PATH}/discussions/abc", {"id": "abc"})]
    run("resolve", "abc")
    assert writes(fake) == [("PUT", f"{MR_PATH}/discussions/abc", {"resolved": True})]
    assert capsys.readouterr().out == f"Resolved discussion abc - {MR_URL}\n"


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
