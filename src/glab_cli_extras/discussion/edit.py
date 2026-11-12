from __future__ import annotations

import argparse
import sys
from typing import Any

from glab_cli_extras.discussion.attach import add_links, check_files, upload_files
from glab_cli_extras.discussion.checks import UsageError, refuse_quick_actions, require_body, settings_of
from glab_cli_extras.discussion.context import resolve_mr_context
from glab_cli_extras.discussion.ids import NoteRef
from glab_cli_extras.discussion.models import is_inline_draft_position
from glab_cli_extras.gitlab.sync_api import glab_api

# The position fields the draft notes API accepts back on update.
_POSITION_KEYS = ("position_type", "base_sha", "start_sha", "head_sha", "old_path", "new_path", "old_line", "new_line")


def run(args: argparse.Namespace) -> None:
    ref: NoteRef = args.note_ref
    attach: list[str] = getattr(args, "attach", None) or []
    if args.body is None and not attach:
        raise UsageError("edit needs --body, --attach, or both")
    settings = settings_of(args)

    # 1. Check the given body and the attached files before any request
    body: str | None = None
    if args.body is not None:
        body = sys.stdin.read() if args.body == "-" else args.body
        require_body(body)
        refuse_quick_actions(body, settings)
    attached = check_files(attach)

    ctx = resolve_mr_context(args)
    mr_path = f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}"
    path = f"{mr_path}/draft_notes/{ref.id}" if ref.is_draft else f"{mr_path}/notes/{ref.id}"

    # 2. Without --body, the links go into the current text. The draft is read anyway, for its position.
    draft: dict[str, Any] | None = (glab_api(path, hostname=ctx.hostname) or {}) if ref.is_draft else None
    if body is None:
        current = draft if draft is not None else glab_api(path, hostname=ctx.hostname) or {}
        body = str(current.get("note" if ref.is_draft else "body") or "")
        refuse_quick_actions(body, settings)

    # 3. Upload the files: last, so that a refused body leaves no upload behind
    if attached:
        body = add_links(body, upload_files(ctx, attached, outcome="edited"))

    if draft is not None:
        # Works around a GitLab API bug present since 16.3 (commit f17012fd): `PUT .../draft_notes/:id` sets
        # `position` to nil when the request does not send one, which turns an inline draft into a general
        # one. The UI's own route does not do this. Send the existing position back with the new text.
        position = draft.get("position") or {}
        if is_inline_draft_position(position):
            kept = {key: position.get(key) for key in _POSITION_KEYS}
            if position.get("line_range"):
                kept["line_range"] = position["line_range"]
            glab_api(path, method="PUT", json_body={"note": body, "position": kept}, hostname=ctx.hostname)
        else:
            glab_api(path, method="PUT", json_body={"note": body}, hostname=ctx.hostname)
    else:
        glab_api(path, method="PUT", json_body={"body": body}, hostname=ctx.hostname)

    print(f"Edited {ref} - {ctx.mr_url}")
