"""Checks the write commands run before they call GitLab, for the mistakes agents kept making with glab-discussion.

Each failed check ends the command with exit 1 before any write request, with a message that says what to do instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from glab_cli_extras.discussion.diff import HUNK_RE
from glab_cli_extras.discussion.models import MrContext
from glab_cli_extras.gitlab.sync_api import GlabApiError, glab_api
from glab_cli_extras.paths import base_dir
from glab_cli_extras.store.layout import STATE_FILE, THREADS_DIR, mr_dir
from glab_cli_extras.store.threads import META_FILE

FULL_ID_LENGTH = 40
MIN_ID_PREFIX = 8


class CheckError(Exception):
    """A write command refuses its input. The message is shown as `Error: <message>`."""


class UsageError(ValueError):
    """The options contradict each other. Reported like an argparse usage error, with exit 2."""


def require_body(body: str) -> None:
    """Refuse an empty body, typically from `--body "$(cat <missing file>)"`. The body itself is never changed."""
    if not body.strip():
        raise CheckError("the body is empty; nothing was sent")


# ------------------------------------------------------------------------------------------------------------------
# Discussion ids


def _known_discussion_ids(ctx: MrContext) -> set[str]:
    """The discussion ids of the MR that `read` or a watch wrote down, from `state.json` and `threads/.meta.json`."""
    directory = mr_dir(base_dir(), ctx.hostname, ctx.project_path, ctx.mr_iid)
    ids: set[str] = set()
    try:
        state = json.loads((directory / STATE_FILE).read_text(encoding="utf-8"))
        for thread in (state.get("snapshot") or {}).get("threads") or []:
            if isinstance(thread, dict) and thread.get("id"):
                ids.add(str(thread["id"]))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        meta = json.loads((directory / THREADS_DIR / META_FILE).read_text(encoding="utf-8"))
        if isinstance(meta, dict):
            ids |= {str(key) for key in meta if not str(key).startswith("draft-")}
    except (OSError, ValueError):
        pass
    return ids


def full_discussion_id(ctx: MrContext, given: str) -> str:
    """The full discussion id for an id or an unambiguous prefix of it, such as the 12 characters in a thread file's
    name. A prefix is looked up in the MR's files, without a request."""
    if len(given) >= FULL_ID_LENGTH:
        return given
    matches = {i for i in _known_discussion_ids(ctx) if i.startswith(given)} if len(given) >= MIN_ID_PREFIX else set()
    if len(matches) == 1:
        return matches.pop()
    raise CheckError(
        f'no discussion {given} on {ctx.mr_url}; use the full id from the "Discussion:" line of the thread file'
    )


def _discussion_path(ctx: MrContext, discussion_id: str) -> str:
    return f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/discussions/{discussion_id}"


def fetch_discussion(ctx: MrContext, discussion_id: str) -> dict[str, Any] | None:
    """The discussion, or None when GitLab no longer has it."""
    try:
        discussion = glab_api(_discussion_path(ctx, discussion_id), hostname=ctx.hostname)
    except GlabApiError as exc:
        if exc.http_status == 404:
            return None
        raise
    return discussion if isinstance(discussion, dict) else None


def gone_message(ctx: MrContext, discussion_id: str) -> str:
    return (
        f"discussion {discussion_id} is gone from {ctx.mr_url}: it was deleted, or a force-push rewrote it."
        " Run 'glab-discussion read' for the current threads"
    )


def explain_404(ctx: MrContext, discussion_id: str, what: str) -> CheckError:
    """The error for a 404 on a write to a discussion: whether the discussion is gone, or the 404 was transient."""
    if fetch_discussion(ctx, discussion_id) is None:
        return CheckError(f"{gone_message(ctx, discussion_id)}. The {what} was not saved")
    return CheckError(
        f"GitLab answered 404 to the {what}, but discussion {discussion_id} still exists, so the error was transient."
        f" The {what} was not saved; send it again"
    )


def not_resolvable_message(discussion_id: str) -> str:
    return f"discussion {discussion_id} is not resolvable (a general comment or a system note); reply to it instead"


# ------------------------------------------------------------------------------------------------------------------
# Diff note anchoring


@dataclass
class DiffLines:
    """The lines a diff note can anchor on in one file's diff, by side."""

    added: set[int] = field(default_factory=set)
    removed: set[int] = field(default_factory=set)
    old_of_new: dict[int, int] = field(default_factory=dict)
    """Context lines: new line → old line."""
    new_of_old: dict[int, int] = field(default_factory=dict)


def parse_diff_lines(diff_text: str) -> DiffLines:
    lines = DiffLines()
    old_line = new_line = 0
    in_hunk = False
    for line in diff_text.split("\n"):
        match = HUNK_RE.match(line)
        if match:
            old_line, new_line, in_hunk = int(match.group(1)), int(match.group(2)), True
            continue
        if not in_hunk:
            continue
        if line.startswith("-"):
            lines.removed.add(old_line)
            old_line += 1
        elif line.startswith("+"):
            lines.added.add(new_line)
            new_line += 1
        elif line.startswith(" "):
            lines.old_of_new[new_line] = old_line
            lines.new_of_old[old_line] = new_line
            old_line += 1
            new_line += 1
    return lines


@dataclass(frozen=True)
class Anchor:
    old_path: str
    new_path: str
    new_line: int | None
    old_line: int | None


def anchor_line(version_detail: dict[str, Any], file: str, new_line: int | None, old_line: int | None) -> Anchor:
    """The position GitLab accepts for a note on this line of the version's diff.

    GitLab rejects a note on a context line without both line numbers (`400 line_code can't be blank`), and a note
    on a line outside every hunk. A renamed file needs its old path. All three come from the diff.
    """
    head = version_detail.get("head_commit_sha") or "the newest version"
    entries = [d for d in version_detail.get("diffs") or [] if isinstance(d, dict)]
    entry = next((d for d in entries if file in (d.get("new_path"), d.get("old_path"))), None)
    if entry is None:
        raise CheckError(
            f"{file} is not in the diff of {head}. Pass the path as 'glab-discussion diff' prints it,"
            " or put the finding in a general comment or the review summary"
        )
    old_path, new_path = str(entry.get("old_path") or file), str(entry.get("new_path") or file)
    text = str(entry.get("diff") or "")
    if not text:
        # GitLab left the diff out (too large, or binary): nothing to check against, so send the lines as given.
        return Anchor(old_path, new_path, new_line, old_line)
    lines = parse_diff_lines(text)

    def outside(number: int) -> CheckError:
        return CheckError(
            f"line {number} of {file} is not in the diff of {head}; GitLab accepts diff notes only on changed lines"
            " and their context. Put the finding in a general comment or the review summary"
        )

    if new_line is not None and old_line is None:
        if new_line in lines.added:
            return Anchor(old_path, new_path, new_line, None)
        if new_line in lines.old_of_new:
            return Anchor(old_path, new_path, new_line, lines.old_of_new[new_line])
        raise outside(new_line)
    if old_line is not None and new_line is None:
        if old_line in lines.removed:
            return Anchor(old_path, new_path, None, old_line)
        if old_line in lines.new_of_old:
            return Anchor(old_path, new_path, lines.new_of_old[old_line], old_line)
        raise outside(old_line)
    assert new_line is not None and old_line is not None
    if lines.old_of_new.get(new_line) == old_line:
        return Anchor(old_path, new_path, new_line, old_line)
    if new_line in lines.old_of_new:
        raise CheckError(
            f"new line {new_line} of {file} is old line {lines.old_of_new[new_line]}, not {old_line};"
            f" pass --old-line {lines.old_of_new[new_line]}, or leave --old-line out"
        )
    if new_line in lines.added:
        raise CheckError(f"new line {new_line} of {file} is an added line, which has no old line; leave --old-line out")
    raise outside(new_line)
