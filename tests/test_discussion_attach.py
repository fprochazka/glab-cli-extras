"""`write --attach`: which files pass, where the images go in the body, and the upload through the fake glab."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakeglab import FakeGlab, Route
from glab_cli_extras.cli.discussion import main
from glab_cli_extras.discussion.attach import add_images, check_images, sniff_image
from glab_cli_extras.discussion.checks import CheckError
from glab_cli_extras.gitlab import sync_api

HOST = "gitlab.example.com"
MR_URL = f"https://{HOST}/group/project/-/merge_requests/7"
PROJECT = "projects/group%2Fproject"
MR_PATH = f"{PROJECT}/merge_requests/7"
DISCUSSION = "abc" + "0" * 37

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 16


def _upload(name: str) -> dict[str, str]:
    url = f"/uploads/{'f' * 32}/{name}"
    return {"alt": name, "url": url, "full_path": f"/-/project/42{url}", "markdown": f"![{name}]({url})"}


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


class TestCheckImages:
    @pytest.mark.parametrize(
        ("name", "content"),
        [("a.png", PNG), ("a.PNG", PNG), ("a.jpg", JPEG), ("a.jpeg", JPEG), ("a.gif", GIF), ("a.webp", WEBP)],
    )
    def test_accepts_the_common_image_types(self, tmp_path: Path, name: str, content: bytes) -> None:
        (tmp_path / name).write_bytes(content)
        assert check_images([str(tmp_path / name)]) == [(tmp_path / name).resolve()]

    def test_refuses_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(CheckError, match="no such file; nothing was sent"):
            check_images([str(tmp_path / "gone.png")])

    def test_refuses_another_extension(self, tmp_path: Path) -> None:
        (tmp_path / "a.svg").write_text("<svg/>")
        with pytest.raises(CheckError, match="only PNG, JPEG, GIF and WebP images can be attached"):
            check_images([str(tmp_path / "a.svg")])

    def test_refuses_a_file_whose_content_is_no_image(self, tmp_path: Path) -> None:
        (tmp_path / "a.png").write_text("not an image")
        with pytest.raises(CheckError, match="the file is not a PNG image"):
            check_images([str(tmp_path / "a.png")])

    def test_refuses_an_image_under_another_type_s_name(self, tmp_path: Path) -> None:
        (tmp_path / "a.png").write_bytes(JPEG)
        with pytest.raises(CheckError, match="the file is a JPEG image, which its name does not match"):
            check_images([str(tmp_path / "a.png")])

    def test_sniff_knows_nothing_else(self) -> None:
        assert sniff_image(b"%PDF-1.7") is None
        assert sniff_image(b"RIFF\x00\x00\x00\x00WAVE") is None


class TestAddImages:
    def test_after_the_text_with_the_trailing_newline_kept(self) -> None:
        assert add_images("Look.\n", ["![a](/u/a)", "![b](/u/b)"]) == "Look.\n\n![a](/u/a)\n![b](/u/b)\n"

    def test_without_a_trailing_newline(self) -> None:
        assert add_images("Look.", ["![a](/u/a)"]) == "Look.\n\n![a](/u/a)"

    def test_before_a_signature_line(self) -> None:
        body = "Look.\n\n<!-- sdlc:mr-babysit -->\n"
        assert add_images(body, ["![a](/u/a)"]) == "Look.\n\n![a](/u/a)\n\n<!-- sdlc:mr-babysit -->\n"

    def test_a_comment_inside_the_last_line_is_no_signature(self) -> None:
        assert add_images("Look <!-- x -->", ["![a](/u/a)"]) == "Look <!-- x -->\n\n![a](/u/a)"

    def test_no_images_leaves_the_body_alone(self) -> None:
        assert add_images("Look.\n", []) == "Look.\n"


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


def test_a_note_and_a_draft_carry_images_too(fake: FakeGlab, screenshot: Path, tmp_path: Path) -> None:
    second = tmp_path / "b.gif"
    second.write_bytes(GIF)
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


def test_a_file_that_is_no_image_stops_the_command_before_any_request(
    fake: FakeGlab, screenshot: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = tmp_path / "notes.png"
    text.write_text("plain text")
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--body", "A.", "--attach", str(screenshot), "--attach", str(text))
    assert exit_info.value.code == 1
    assert "the file is not a PNG image" in capsys.readouterr().err
    assert fake.calls == []


def test_a_failed_upload_posts_nothing(fake: FakeGlab, screenshot: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake.routes = [Route(f"{PROJECT}/uploads", {"message": "413 Request Entity Too Large"}, status=413)]
    with pytest.raises(SystemExit) as exit_info:
        run("write", "--body", "A.", "--attach", str(screenshot))
    assert exit_info.value.code == 1
    assert "The comment was not posted." in capsys.readouterr().err
    assert [c.endpoint for c in fake.calls] == [f"{PROJECT}/uploads"]
