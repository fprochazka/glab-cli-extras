"""Images attached to a comment with `write --attach`: checked before anything is sent, uploaded to the project, and
added to the body as Markdown images."""

from __future__ import annotations

import re
from pathlib import Path

from glab_cli_extras.discussion.checks import CheckError
from glab_cli_extras.discussion.models import MrContext
from glab_cli_extras.gitlab.sync_api import GlabApiError, glab_api_upload

# Extension -> the image type its content must start with.
IMAGE_EXTENSIONS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WebP"}

# A signature such as `<!-- plugin:command -->` on the body's last line, with the blank lines before it.
_SIGNATURE_RE = re.compile(r"(?:\A|\n)\n*(<!--[^\n]*-->\n*)\Z")


def sniff_image(head: bytes) -> str | None:
    """The image type the first bytes of a file show, or None for anything else."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WebP"
    return None


def check_images(files: list[str]) -> list[Path]:
    """Refuse a file that is missing, or that is not a PNG, JPEG, GIF or WebP image by both its name and its content."""
    paths: list[Path] = []
    for file in files:
        path = Path(file).expanduser()
        expected = IMAGE_EXTENSIONS.get(path.suffix.lower())
        if expected is None:
            raise CheckError(
                f"--attach {file}: only PNG, JPEG, GIF and WebP images can be attached"
                f" ({', '.join(IMAGE_EXTENSIONS)}); nothing was sent"
            )
        if not path.is_file():
            raise CheckError(f"--attach {file}: no such file; nothing was sent")
        with path.open("rb") as fh:
            found = sniff_image(fh.read(12))
        if found != expected:
            what = f"a {found} image" if found else f"not a {expected} image"
            raise CheckError(f"--attach {file}: the file is {what}, which its name does not match; nothing was sent")
        paths.append(path.resolve())
    return paths


def upload_images(ctx: MrContext, paths: list[Path]) -> list[str]:
    """Upload each image to the MR's project and return its Markdown, in the order given."""
    images: list[str] = []
    for path in paths:
        try:
            result = glab_api_upload(f"projects/{ctx.project_id}/uploads", str(path), hostname=ctx.hostname)
        except GlabApiError as exc:
            done = f" The {len(images)} images uploaded before it stay in the project." if images else ""
            raise CheckError(f"uploading {path} failed: {exc}. The comment was not posted.{done}") from exc
        markdown = (result or {}).get("markdown") if isinstance(result, dict) else None
        if not markdown:
            raise CheckError(f"GitLab answered the upload of {path} without its Markdown; the comment was not posted")
        images.append(str(markdown))
    return images


def add_images(body: str, images: list[str]) -> str:
    """The body with the images after its text, one per line after a blank line.

    A signature line at the end of the body stays the last line, so that the images go before it.
    """
    if not images:
        return body
    block = "\n".join(images)
    match = _SIGNATURE_RE.search(body)
    if match:
        text = body[: match.start()].rstrip("\n")
        return f"{text}\n\n{block}\n\n{match.group(1)}" if text else f"{block}\n\n{match.group(1)}"
    text = body.rstrip("\n")
    return f"{text}\n\n{block}{body[len(text) :]}"
