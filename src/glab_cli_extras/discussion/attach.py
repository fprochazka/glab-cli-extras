"""Files attached to a comment with `--attach`: checked before anything is sent, uploaded to the project, and added
to the body as the Markdown GitLab returns for them.

GitLab takes any file up to the instance's maximum attachment size. Its `markdown` field already decides how the
file shows: `![name](url)` for an image, a video, an audio file or an SVG, which render inline, and `[name](url)`, a
download link, for anything else. So the command checks only that the file can be read, and uses that Markdown as is.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from glab_cli_extras.discussion.checks import CheckError
from glab_cli_extras.discussion.models import MrContext
from glab_cli_extras.gitlab.sync_api import GlabApiError, glab_api_upload

# A signature such as `<!-- plugin:command -->` on the body's last line, with the blank lines before it.
_SIGNATURE_RE = re.compile(r"(?:\A|\n)\n*(<!--[^\n]*-->\n*)\Z")


def check_files(files: list[str]) -> list[Path]:
    """Refuse a file that is missing, not a regular file, or not readable."""
    paths: list[Path] = []
    for file in files:
        path = Path(file).expanduser()
        if not path.exists():
            raise CheckError(f"--attach {file}: no such file; nothing was sent")
        if not path.is_file():
            raise CheckError(f"--attach {file}: not a regular file; nothing was sent")
        if not os.access(path, os.R_OK):
            raise CheckError(f"--attach {file}: you cannot read the file; nothing was sent")
        paths.append(path.resolve())
    return paths


def upload_files(ctx: MrContext, paths: list[Path], outcome: str = "posted") -> list[str]:
    """Upload each file to the MR's project and return its Markdown, in the order given.

    `outcome` is what did not happen to the comment when an upload fails: "posted", or "edited" for `edit`.
    """
    links: list[str] = []
    for path in paths:
        try:
            result = glab_api_upload(f"projects/{ctx.project_id}/uploads", str(path), hostname=ctx.hostname)
        except GlabApiError as exc:
            done = f" The {len(links)} files uploaded before it stay in the project." if links else ""
            why = " The file is larger than this GitLab accepts as an attachment." if exc.http_status == 413 else ""
            raise CheckError(f"uploading {path} failed: {exc}.{why} The comment was not {outcome}.{done}") from exc
        markdown = (result or {}).get("markdown") if isinstance(result, dict) else None
        if not markdown:
            raise CheckError(
                f"GitLab answered the upload of {path} without its Markdown; the comment was not {outcome}"
            )
        links.append(str(markdown))
    return links


def add_links(body: str, links: list[str]) -> str:
    """The body with the links after its text, one per line after a blank line.

    A signature line at the end of the body stays the last line, so that the links go before it.
    """
    if not links:
        return body
    block = "\n".join(links)
    match = _SIGNATURE_RE.search(body)
    if match:
        text = body[: match.start()].rstrip("\n")
        return f"{text}\n\n{block}\n\n{match.group(1)}" if text else f"{block}\n\n{match.group(1)}"
    text = body.rstrip("\n")
    return f"{text}\n\n{block}{body[len(text) :]}"
