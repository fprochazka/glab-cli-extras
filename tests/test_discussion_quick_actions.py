"""The `discussion.quick_actions = "reject"` setting: which lines count as quick actions, and which commands refuse
them before any request."""

from __future__ import annotations

import io

import pytest

from fakeglab import FakeGlab, Route
from glab_cli_extras.cli.discussion import main
from glab_cli_extras.discussion.checks import quick_action_lines
from glab_cli_extras.gitlab import sync_api

MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/7"
MR_PATH = "projects/group%2Fproject/merge_requests/7"


class TestQuickActionLines:
    def test_a_command_at_the_start_of_a_line(self) -> None:
        assert quick_action_lines("Done.\n/approve\n/label ~bug ~p1\n") == [(2, "/approve"), (3, "/label ~bug ~p1")]

    def test_any_word_counts_not_only_known_commands(self) -> None:
        assert quick_action_lines("/whatever it is") == [(1, "/whatever it is")]

    def test_an_indented_command_counts(self) -> None:
        assert quick_action_lines("Text\n   /merge\n\t/close") == [(2, "/merge"), (3, "/close")]

    def test_a_command_inside_a_closed_fence_does_not_count(self) -> None:
        body = "Run:\n```bash\n/merge\n```\n~~~\n/close\n~~~~\nAfter.\n"
        assert quick_action_lines(body) == []

    def test_a_command_after_the_fence_counts(self) -> None:
        assert quick_action_lines("```\n/merge\n```\n/close") == [(4, "/close")]

    def test_an_unclosed_fence_protects_nothing(self) -> None:
        assert quick_action_lines("```\n/merge\n") == [(2, "/merge")]

    def test_a_fence_closes_only_with_its_own_character(self) -> None:
        assert quick_action_lines("```\n/merge\n~~~\n") == [(2, "/merge")]

    @pytest.mark.parametrize(
        "body",
        [
            "A path: /usr/bin/env",
            "/usr/bin/env is a path",
            "`/merge` in backticks",
            "> /merge in a quote",
            "- /merge in a list",
            "Ends with /merge",
            "// a comment",
            "/ alone",
        ],
    )
    def test_text_that_is_no_quick_action(self, body: str) -> None:
        assert quick_action_lines(body) == []


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGlab:
    fake = FakeGlab()
    monkeypatch.setattr(sync_api, "RUNNER", fake.sync)
    monkeypatch.setattr(sync_api, "SLEEP", lambda seconds: None)
    monkeypatch.delenv("GLAB_DISCUSSION_WRITE_AS_DRAFT", raising=False)
    monkeypatch.setenv("GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS", "reject")
    return fake


@pytest.mark.parametrize(
    "argv",
    [
        ["write", "--body", "-"],
        ["write", "--note", "--body", "-"],
        ["write", "--draft", "--body", "-"],
        ["edit", "note:5", "--body", "-"],
        ["edit", "draft:5", "--body", "-"],
        ["drafts", "publish", "--verdict", "reviewed", "--body", "-"],
    ],
)
def test_every_command_that_posts_a_body_refuses_one_with_a_quick_action(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("Looks good.\n/approve\n"))
    with pytest.raises(SystemExit) as exit_info:
        main([*argv, "--mr-url", MR_URL])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "the body has lines that GitLab runs as quick actions (line 2: /approve)" in err
    assert 'discussion.quick_actions = "reject" (from GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS)' in err
    assert fake.calls == []


def test_a_quick_action_in_a_code_block_is_posted(fake: FakeGlab) -> None:
    fake.routes = [Route(f"{MR_PATH}/notes", {"id": 9}, status=201)]
    body = "Type this:\n\n```\n/approve\n```\n"
    main(["write", "--note", "--body", body, "--mr-url", MR_URL])
    assert fake.calls[0].body == {"body": body}


def test_the_review_is_still_submitted_with_its_own_quick_action(
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
    nothing = {"errors": ["User has not requested changes for this merge request"]}
    fake.routes = [
        Route("draft_notes", []),
        Route("currentUser", {"data": state}),
        Route("mergeRequestDestroyRequestedChanges", {"data": {"mergeRequestDestroyRequestedChanges": nothing}}),
        Route(f"{MR_PATH}/notes", {"summary": ["Submitted the current review."]}, status=202),
    ]
    main(["drafts", "publish", "--verdict", "reviewed", "--mr-url", MR_URL])
    assert ("POST", f"{MR_PATH}/notes") in [(c.method, c.endpoint) for c in fake.calls]
    assert "Set your reviewer state to reviewed." in capsys.readouterr().out


def test_an_invalid_config_stops_every_command(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS", "maybe")
    with pytest.raises(SystemExit) as exit_info:
        main(["read", "--mr-url", MR_URL])
    assert exit_info.value.code == 1
    assert "GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS: discussion.quick_actions is 'maybe'" in capsys.readouterr().err
    assert fake.calls == []
