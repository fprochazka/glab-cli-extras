from __future__ import annotations

import argparse

from glab_cli_extras.discussion.context import resolve_mr_context
from glab_cli_extras.discussion.ids import NoteRef
from glab_cli_extras.gitlab.sync_api import glab_api


def run(args: argparse.Namespace) -> None:
    ctx = resolve_mr_context(args)
    ref: NoteRef = args.note_ref

    resource = "draft_notes" if ref.is_draft else "notes"
    glab_api(
        f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/{resource}/{ref.id}",
        method="DELETE",
        hostname=ctx.hostname,
    )

    print(f"Deleted {ref} - {ctx.mr_url}")
