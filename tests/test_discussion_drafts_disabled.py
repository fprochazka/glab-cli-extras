"""The `discussion.drafts = "disabled"` setting: no draft command or option in the parser, and no draft written."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from fakeglab import FakeGlab, Route
from glab_cli_extras.cli.discussion import build_parser, main
from glab_cli_extras.config import load_settings
from glab_cli_extras.discussion.checks import CheckError
from glab_cli_extras.discussion.drafts import run_publish
from glab_cli_extras.gitlab import sync_api

MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/7"
MR_PATH = "projects/group%2Fproject/merge_requests/7"
DISCUSSION = "abc" + "0" * 37


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGlab:
    fake = FakeGlab()
    monkeypatch.setattr(sync_api, "RUNNER", fake.sync)
    monkeypatch.setattr(sync_api, "SLEEP", lambda seconds: None)
    monkeypatch.delenv("GLAB_DISCUSSION_WRITE_AS_DRAFT", raising=False)
    monkeypatch.setenv("GLAB_CLI_EXTRAS_DISCUSSION_DRAFTS", "disabled")
    return fake


def _help(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit) as exit_info:
        main([*argv, "--help"])
    assert exit_info.value.code == 0
    return capsys.readouterr().out


def test_help_offers_no_draft_command_or_option(fake: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    assert "drafts" not in _help(capsys)
    write_help = _help(capsys, "write")
    assert "--draft" not in write_help and "--no-draft" not in write_help and "--resolve" not in write_help
    assert "--note" in write_help and "--attach" in write_help
    for command in ("edit", "delete"):
        text = _help(capsys, command)
        assert "note:ID" in text and "draft" not in text


def test_the_parser_keeps_them_when_drafts_are_enabled() -> None:
    text = build_parser(load_settings()).format_help()
    assert "drafts" in text


@pytest.mark.parametrize(
    "argv",
    [
        ["drafts", "publish", "--verdict", "reviewed"],
        ["drafts", "delete", "--force"],
        ["write", "--draft", "--body", "A."],
        ["write", "--no-draft", "--body", "A."],
        ["write", "--reply-to", DISCUSSION, "--resolve", "--body", "A."],
        ["edit", "draft:5", "--body", "A."],
        ["edit", "draft:5", "--attach", "screen.png"],
        ["delete", "draft:5"],
    ],
)
def test_every_draft_command_and_option_is_a_usage_error(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([*argv, "--mr-url", MR_URL])
    assert exit_info.value.code == 2
    assert fake.calls == []
    err = capsys.readouterr().err
    assert "invalid choice" in err or "unrecognized arguments" in err or "drafts off" in err


def test_a_write_is_posted_at_once(fake: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [Route(f"{MR_PATH}/discussions", {"id": "d" * 40, "notes": [{"id": 9}]}, status=201)]
    main(["write", "--body", "A.", "--mr-url", MR_URL])
    assert [(c.method, c.endpoint) for c in fake.calls] == [("POST", f"{MR_PATH}/discussions")]
    assert "Created discussion" in capsys.readouterr().out


def test_edit_and_delete_still_take_a_note(fake: FakeGlab) -> None:
    fake.routes = [Route(f"{MR_PATH}/notes/5", {"id": 5})]
    main(["edit", "note:5", "--body", "B.", "--mr-url", MR_URL])
    main(["delete", "note:5", "--mr-url", MR_URL])
    assert [c.method for c in fake.calls] == ["PUT", "DELETE"]


def test_the_draft_variable_stops_the_write_instead_of_posting_it(
    fake: FakeGlab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLAB_DISCUSSION_WRITE_AS_DRAFT", "true")
    with pytest.raises(SystemExit) as exit_info:
        main(["write", "--body", "A.", "--mr-url", MR_URL])
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert 'GLAB_DISCUSSION_WRITE_AS_DRAFT asks for a draft, and discussion.drafts = "disabled"' in err
    assert fake.calls == []


def test_a_draft_or_a_review_is_refused_also_past_the_parser(fake: FakeGlab, tmp_path: Path) -> None:
    with pytest.raises(CheckError, match="drafts are turned off"):
        run_publish(Namespace(mr_url=MR_URL, body=None, verdict="reviewed", settings=load_settings()))
    assert fake.calls == []


def test_the_setting_from_a_file_removes_the_commands_too(
    fake: FakeGlab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GLAB_CLI_EXTRAS_DISCUSSION_DRAFTS")
    path = tmp_path / "agent.toml"
    path.write_text('[discussion]\ndrafts = "disabled"\n')
    monkeypatch.setenv("GLAB_CLI_EXTRAS_CONFIG", str(path))
    with pytest.raises(SystemExit) as exit_info:
        main(["drafts", "publish", "--mr-url", MR_URL])
    assert exit_info.value.code == 2
    assert "invalid choice: 'drafts'" in capsys.readouterr().err
