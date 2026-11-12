"""`--attach`: which files pass, where the links go in the body, and the upload through the fake glab."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakeglab import FakeGlab, Route
from glab_cli_extras.cli.discussion import main
from glab_cli_extras.discussion.attach import add_links, check_files
from glab_cli_extras.discussion.checks import CheckError
from glab_cli_extras.gitlab import sync_api

HOST = "gitlab.example.com"
MR_URL = f"https://{HOST}/group/project/-/merge_requests/7"
PROJECT = "projects/group%2Fproject"
MR_PATH = f"{PROJECT}/merge_requests/7"
DISCUSSION = "abc" + "0" * 37

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _upload(name: str, image: bool = True) -> dict[str, str]:
    url = f"/uploads/{'f' * 32}/{name}"
    markdown = f"{'!' if image else ''}[{name}]({url})"
    return {"alt": name, "url": url, "full_path": f"/-/project/42{url}", "markdown": markdown}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGlab:
    fake = FakeGlab()
    monkeypatch.setattr(sync_api, "RUNNER", fake.sync)
    monkeypatch.setattr(sync_api, "SLEEP", lambda seconds: None)
    monkeypatch.delenv("GLAB_DISCUSSION_WRITE_AS_DRAFT", raising=False)
    return fake


@pytest.fixture
def screenshot(tmp_path: Path) -> Path:
    path = tmp_path / "screen.png"
    path.write_bytes(PNG)
    return path


class TestCheckFiles:
    @pytest.mark.parametrize("name", ["a.png", "a.svg", "build.log", "notes.txt", "no-extension"])
    def test_accepts_any_regular_file(self, tmp_path: Path, name: str) -> None:
        (tmp_path / name).write_bytes(b"anything")
        assert check_files([str(tmp_path / name)]) == [(tmp_path / name).resolve()]

    def test_refuses_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(CheckError, match="no such file; nothing was sent"):
            check_files([str(tmp_path / "gone.png")])

    def test_refuses_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(CheckError, match="not a regular file; nothing was sent"):
            check_files([str(tmp_path)])


class TestAddLinks:
    def test_after_the_text_with_the_trailing_newline_kept(self) -> None:
        assert add_links("Look.\n", ["![a](/u/a)", "![b](/u/b)"]) == "Look.\n\n![a](/u/a)\n![b](/u/b)\n"

    def test_without_a_trailing_newline(self) -> None:
        assert add_links("Look.", ["![a](/u/a)"]) == "Look.\n\n![a](/u/a)"

    def test_before_a_signature_line(self) -> None:
        body = "Look.\n\n<!-- sdlc:mr-babysit -->\n"
        assert add_links(body, ["![a](/u/a)"]) == "Look.\n\n![a](/u/a)\n\n<!-- sdlc:mr-babysit -->\n"

    def test_a_comment_inside_the_last_line_is_no_signature(self) -> None:
        assert add_links("Look <!-- x -->", ["![a](/u/a)"]) == "Look <!-- x -->\n\n![a](/u/a)"

    def test_no_links_leave_the_body_alone(self) -> None:
        assert add_links("Look.\n", []) == "Look.\n"


def run(*argv: str) -> None:
    main([*argv, "--mr-url", MR_URL])


def test_a_reply_uploads_the_image_as_a_form_part_and_shows_it_after_the_text(
    fake: FakeGlab, screenshot: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.routes = [
        Route(f"{PROJECT}/uploads", _upload("screen.png"), status=201),
        Route(f"{MR_PATH}/discussions/{DISCUSSION}/notes", {"id": 501}, status=201),
    ]
    run("write", "--reply-to", DISCUSSION, "--body", "See the screenshot.", "--attach", str(screenshot))
    upload, reply = fake.calls
    assert (upload.method, upload.endpoint, upload.stdin) == ("POST", f"{PROJECT}/uploads", None)
    assert upload.args[upload.args.index("--form") + 1] == f"file=@{screenshot.resolve()}"
    assert "--input" not in upload.args
    assert reply.body == {"body": f"See the screenshot.\n\n![screen.png](/uploads/{'f' * 32}/screen.png)"}
    assert "note:501" in capsys.readouterr().out


def test_a_note_and_a_draft_carry_files_too(fake: FakeGlab, screenshot: Path, tmp_path: Path) -> None:
    second = tmp_path / "b.png"
    second.write_bytes(PNG)
    fake.routes = [
        Route(f"{PROJECT}/uploads", _upload("x.png"), status=201),
        Route(f"{MR_PATH}/notes", {"id": 601}, status=201),
        Route(f"{MR_PATH}/draft_notes", {"id": 701}, status=201),
    ]
    run("write", "--note", "--body", "A.", "--attach", str(screenshot), "--attach", str(second))
    run("write", "--draft", "--body", "B.", "--attach", str(screenshot))
    image = f"![x.png](/uploads/{'f' * 32}/x.png)"
    assert [(c.endpoint, c.body) for c in fake.calls if c.endpoint != f"{PROJECT}/uploads"] == [
        (f"{MR_PATH}/notes", {"body": f"A.\n\n{image}\n{image}"}),
        (f"{MR_PATH}/draft_notes", {"note": f"B.\n\n{image}"}),
    ]
    assert [c.endpoint for c in fake.calls].count(f"{PROJECT}/uploads") == 3


def test_an_svg_and_a_log_are_uploaded_and_linked_as_gitlab_answers(fake: FakeGlab, tmp_path: Path) -> None:
    svg = tmp_path / "graph.svg"
    svg.write_text("<svg/>")
    log = tmp_path / "build.log"
    log.write_text("FAILED")
    fake.routes = [
        Route(f"{PROJECT}/uploads", _upload("graph.svg"), status=201, contains="graph.svg"),
        Route(f"{PROJECT}/uploads", _upload("build.log", image=False), status=201, contains="build.log"),
        Route(f"{MR_PATH}/notes", {"id": 601}, status=201),
    ]
    run("write", "--note", "--body", "A.", "--attach", str(svg), "--attach", str(log))
    url = f"/uploads/{'f' * 32}"
    assert fake.calls[-1].body == {"body": f"A.\n\n![graph.svg]({url}/graph.svg)\n[build.log]({url}/build.log)"}


def test_a_missing_file_stops_the_command_before_any_request(
    fake: FakeGlab, screenshot: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--body", "A.", "--attach", str(screenshot), "--attach", str(tmp_path / "gone.log"))
    assert exit_info.value.code == 1
    assert "gone.log: no such file; nothing was sent" in capsys.readouterr().err
    assert fake.calls == []


def test_a_failed_upload_posts_nothing(fake: FakeGlab, screenshot: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [Route(f"{PROJECT}/uploads", {"message": "413 Request Entity Too Large"}, status=413)]
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--body", "A.", "--attach", str(screenshot))
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "larger than this GitLab accepts as an attachment. The comment was not posted." in err
    assert [c.endpoint for c in fake.calls] == [f"{PROJECT}/uploads"]
