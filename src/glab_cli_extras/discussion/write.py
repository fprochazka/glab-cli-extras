from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from glab_cli_extras.config import DRAFTS
from glab_cli_extras.discussion.attach import add_images, check_images, upload_images
from glab_cli_extras.discussion.checks import (
    CheckError,
    UsageError,
    anchor_line,
    explain_404,
    full_discussion_id,
    refuse_quick_actions,
    require_body,
    require_drafts,
    settings_of,
)
from glab_cli_extras.discussion.context import resolve_mr_context
from glab_cli_extras.discussion.models import MrContext, is_inline_draft_position, parse_draft_note
from glab_cli_extras.gitlab.sync_api import GlabApiError, glab_api

DRAFT_ENV_VAR = "GLAB_DISCUSSION_WRITE_AS_DRAFT"
_TRUE_VALUES = {"true", "1", "yes"}
_FALSE_VALUES = {"false", "0", "no", ""}


def build_position(
    version: dict[str, Any], file: str, new_line: int | None, old_line: int | None, *, old_path: str | None = None
) -> dict[str, Any]:
    """Build the `position` of a diff note on one line of `file` in the given MR diff version. `old_path` is the
    file's path before a rename."""
    position: dict[str, Any] = {
        "position_type": "text",
        "base_sha": version["base_commit_sha"],
        "head_sha": version["head_commit_sha"],
        "start_sha": version["start_commit_sha"],
        "old_path": old_path or file,
        "new_path": file,
    }

    if new_line is not None:
        position["new_line"] = new_line
    if old_line is not None:
        position["old_line"] = old_line

    return position


def write_as_draft(args: argparse.Namespace) -> bool:
    """Decide whether to write a draft: --draft or --no-draft win, otherwise GLAB_DISCUSSION_WRITE_AS_DRAFT decides."""
    settings = settings_of(args)
    if settings.drafts_disabled:
        # The variable asks to hold every comment back as a draft; posting it at once instead is not what it meant.
        if os.environ.get(DRAFT_ENV_VAR, "").strip().lower() in _TRUE_VALUES:
            print(
                f"Error: {DRAFT_ENV_VAR} asks for a draft, and {settings.describe(DRAFTS)} turns drafts off."
                f" Unset {DRAFT_ENV_VAR} to post the comment at once. Nothing was sent.",
                file=sys.stderr,
            )
            sys.exit(1)
        return False
    if getattr(args, "draft", False):
        return True
    if getattr(args, "no_draft", False):
        return False

    value = os.environ.get(DRAFT_ENV_VAR, "")
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False

    print(
        f"Error: {DRAFT_ENV_VAR} is set to '{value}', which is not a known value."
        " Set it to true, 1, yes, false, 0 or no, or pass --draft or --no-draft.",
        file=sys.stderr,
    )
    sys.exit(1)


def _select_version(ctx: MrContext, commit: str | None) -> dict[str, Any]:
    versions = glab_api(
        f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/versions",
        hostname=ctx.hostname,
    )

    if not commit:
        latest: dict[str, Any] = versions[0]
        return latest

    # Find version matching the given commit SHA
    for v in versions:
        if v["head_commit_sha"] == commit:
            matching: dict[str, Any] = v
            return matching
    print(
        f"Error: no diff version found for commit {commit}",
        file=sys.stderr,
    )
    sys.exit(1)


def _diff_position(ctx: MrContext, args: argparse.Namespace) -> dict[str, Any]:
    """The position of a diff note, anchored on the version's diff: both line numbers on a context line, the old
    path of a renamed file, and an error for a line outside every hunk."""
    version = _select_version(ctx, args.commit)
    detail = glab_api(
        f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/versions/{version['id']}", hostname=ctx.hostname
    )
    anchor = anchor_line(detail if isinstance(detail, dict) else {}, args.file, args.new_line, args.old_line)
    return build_position(version, anchor.new_path, anchor.new_line, anchor.old_line, old_path=anchor.old_path)


_NOTE_CONFLICTS = (
    ("reply_to", "--reply-to"),
    ("file", "--file"),
    ("new_line", "--new-line"),
    ("old_line", "--old-line"),
    ("commit", "--commit"),
    ("draft", "--draft"),
    ("resolve", "--resolve"),
)


def _check_note_options(args: argparse.Namespace) -> None:
    """A plain note is a general comment posted at once: it has no thread to join, no diff position and no draft."""
    given = [flag for attr, flag in _NOTE_CONFLICTS if getattr(args, attr, None) not in (None, False)]
    if given:
        raise UsageError(
            f"--note posts a plain comment at once, so it does not take {', '.join(given)}."
            " Leave out --note for a thread, a reply, a diff note or a draft"
        )


def run(args: argparse.Namespace) -> None:
    # 1. Validate options before any API call
    plain_note = getattr(args, "note", False)
    if plain_note:
        _check_note_options(args)

    if args.reply_to and args.file:
        print("Error: --reply-to and --file are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if args.file and args.new_line is None and args.old_line is None:
        print(
            "Error: --file requires at least one of --new-line or --old-line",
            file=sys.stderr,
        )
        sys.exit(1)

    draft = not plain_note and write_as_draft(args)
    resolve = getattr(args, "resolve", False)

    if resolve and not (draft and args.reply_to):
        print(
            "Error: --resolve works only on a draft reply. Use it with --reply-to and --draft,"
            " or resolve a thread directly with 'glab-discussion resolve'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 2. Read the body, and check the images before any request
    body = sys.stdin.read() if args.body == "-" else args.body
    require_body(body)
    refuse_quick_actions(body, settings_of(args))
    images = check_images(getattr(args, "attach", None) or [])

    # 3. Resolve MR context, the full id of the discussion a reply goes to, and the position of a diff note
    ctx = resolve_mr_context(args)
    if args.reply_to:
        args.reply_to = full_discussion_id(ctx, args.reply_to)
    position = _diff_position(ctx, args) if args.file else None

    # 4. Upload the images: last, so that a refused option or line leaves no upload behind
    if images:
        body = add_images(body, upload_images(ctx, images))

    # 5. Determine mode and execute
    if plain_note:
        _write_note(ctx, body)
        return

    if draft:
        _write_draft(args, ctx, body, resolve, position)
        return

    if args.reply_to:
        # Reply to existing discussion
        try:
            result = glab_api(
                f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/discussions/{args.reply_to}/notes",
                method="POST",
                json_body={"body": body},
                hostname=ctx.hostname,
            )
        except GlabApiError as exc:
            if exc.http_status == 404:
                raise explain_404(ctx, args.reply_to, "reply") from exc
            raise
        print(f"Replied to discussion {args.reply_to} (note:{result['id']}) - {ctx.mr_url}#note_{result['id']}")

    elif position is not None:
        result = glab_api(
            f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/discussions",
            method="POST",
            json_body={"body": body, "position": position},
            hostname=ctx.hostname,
        )

        line = args.new_line if args.new_line is not None else args.old_line
        note = result["notes"][0]
        note_id = note["id"]
        if note.get("type") != "DiffNote":
            # The old silent failure: GitLab drops a position it cannot use and creates a general comment.
            raise CheckError(
                f"GitLab created note:{note_id} as a general comment, not as a diff note on {args.file}:{line}."
                f" It is left in place: {ctx.mr_url}#note_{note_id}"
            )
        print(
            f"Created diff note on {args.file}:{line} (discussion {result['id']}, note:{note_id})"
            f" - {ctx.mr_url}#note_{note_id}"
        )

    else:
        # New general discussion thread
        result = glab_api(
            f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/discussions",
            method="POST",
            json_body={"body": body},
            hostname=ctx.hostname,
        )
        note_id = result["notes"][0]["id"]
        print(f"Created discussion {result['id']} (note:{note_id}) - {ctx.mr_url}#note_{note_id}")


def _write_note(ctx: MrContext, body: str) -> None:
    result = glab_api(
        f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/notes",
        method="POST",
        json_body={"body": body},
        hostname=ctx.hostname,
    )
    note_id = (result or {}).get("id")
    if note_id is None:
        # A body of quick actions only: GitLab runs them, answers 202 with their summary, and keeps no note.
        for message in (result or {}).get("summary") or []:
            print(f"GitLab: {message}")
        print(f"GitLab ran the body as quick actions and created no note - {ctx.mr_url}")
        return
    print(f"Created note:{note_id} - {ctx.mr_url}#note_{note_id}")


def _write_draft(
    args: argparse.Namespace, ctx: MrContext, body: str, resolve: bool, position: dict[str, Any] | None
) -> None:
    require_drafts(settings_of(args))
    payload: dict[str, Any] = {"note": body}
    if args.reply_to:
        payload["in_reply_to_discussion_id"] = args.reply_to
        if resolve:
            payload["resolve_discussion"] = True
        target = f"reply to discussion {args.reply_to}" + (", resolves the thread" if resolve else "")
    elif position is not None:
        payload["position"] = position
        line = args.new_line if args.new_line is not None else args.old_line
        target = f"diff note on {args.file}:{line}"
    else:
        target = "new thread"

    try:
        result = glab_api(
            f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/draft_notes",
            method="POST",
            json_body=payload,
            hostname=ctx.hostname,
        )
    except GlabApiError as exc:
        if exc.http_status == 404 and args.reply_to:
            raise explain_404(ctx, args.reply_to, "draft reply") from exc
        if args.reply_to and exc.http_status == 400 and "has already been taken" in str(exc):
            raise _second_draft_reply(ctx, args.reply_to) from exc
        raise
    if args.file and not is_inline_draft_position(result.get("position") or {}):
        raise CheckError(
            f"GitLab saved draft:{result['id']} as a general draft, not as a diff note on {args.file}."
            f" It is left in place; delete it with 'glab-discussion delete draft:{result['id']}'"
        )

    print(f"Created draft:{result['id']} ({target}). Only you can see it until 'glab-discussion drafts publish'.")
    if not getattr(args, "draft", False):
        # The environment made this a draft, which the caller may not expect.
        print(f"Written as a draft because {DRAFT_ENV_VAR} is set. Pass --no-draft to post it right away.")


def _second_draft_reply(ctx: MrContext, discussion_id: str) -> CheckError:
    """The error for a second draft reply in one thread, which GitLab refuses with `400 author_id has already been
    taken`: it keeps one draft reply per user and thread. Names the existing draft when the drafts can be listed."""
    existing = None
    try:
        drafts = glab_api(
            f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/draft_notes", paginate=True, hostname=ctx.hostname
        )
        existing = next(
            (d.id for d in map(parse_draft_note, drafts or []) if d.discussion_id == discussion_id),
            None,
        )
    except (GlabApiError, KeyError, TypeError):
        pass
    if existing is None:
        what = "a draft reply"
        fix = "Find it with 'glab-discussion read', then edit it with 'glab-discussion edit draft:<id>' or delete it"
    else:
        what = f"a draft reply (draft:{existing})"
        fix = (
            f"Put this text into it with 'glab-discussion edit draft:{existing} --body - < file',"
            f" or delete it with 'glab-discussion delete draft:{existing}' and write the reply again"
        )
    return CheckError(
        f"you already have {what} in discussion {discussion_id}, and GitLab allows one draft reply per thread"
        f" for each user. The new reply was not saved. {fix}"
    )
