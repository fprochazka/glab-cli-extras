from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from glab_cli_extras.discussion.checks import (
    CheckError,
    anchor_line,
    explain_404,
    full_discussion_id,
    require_body,
)
from glab_cli_extras.discussion.context import resolve_mr_context
from glab_cli_extras.discussion.models import MrContext, is_inline_draft_position
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


def run(args: argparse.Namespace) -> None:
    # 1. Validate options before any API call
    if args.reply_to and args.file:
        print("Error: --reply-to and --file are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if args.file and args.new_line is None and args.old_line is None:
        print(
            "Error: --file requires at least one of --new-line or --old-line",
            file=sys.stderr,
        )
        sys.exit(1)

    draft = write_as_draft(args)
    resolve = getattr(args, "resolve", False)

    if resolve and not (draft and args.reply_to):
        print(
            "Error: --resolve works only on a draft reply. Use it with --reply-to and --draft,"
            " or resolve a thread directly with 'glab-discussion resolve'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 2. Read the body
    body = sys.stdin.read() if args.body == "-" else args.body
    require_body(body)

    # 3. Resolve MR context, and the full id of the discussion a reply goes to
    ctx = resolve_mr_context(args)
    if args.reply_to:
        args.reply_to = full_discussion_id(ctx, args.reply_to)

    # 4. Determine mode and execute
    if draft:
        _write_draft(args, ctx, body, resolve)
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

    elif args.file:
        # Diff note mode - fetch versions to get SHAs
        position = _diff_position(ctx, args)

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


def _write_draft(args: argparse.Namespace, ctx: MrContext, body: str, resolve: bool) -> None:
    payload: dict[str, Any] = {"note": body}
    if args.reply_to:
        payload["in_reply_to_discussion_id"] = args.reply_to
        if resolve:
            payload["resolve_discussion"] = True
        target = f"reply to discussion {args.reply_to}" + (", resolves the thread" if resolve else "")
    elif args.file:
        payload["position"] = _diff_position(ctx, args)
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
