from __future__ import annotations

import argparse

from glab_cli_extras.discussion.checks import (
    CheckError,
    fetch_discussion,
    full_discussion_id,
    gone_message,
    not_resolvable_message,
)
from glab_cli_extras.discussion.context import resolve_mr_context
from glab_cli_extras.gitlab.sync_api import GlabApiError, glab_api


def run(args: argparse.Namespace) -> None:
    ctx = resolve_mr_context(args)
    resolved = not args.unresolve
    discussion_id = full_discussion_id(ctx, args.discussion_id)

    # GitLab answers 403 or 404 to a resolve of a general comment, which reads like a permission problem. Reading the
    # discussion first tells the two apart, and says when the discussion is gone.
    discussion = fetch_discussion(ctx, discussion_id)
    if discussion is None:
        raise CheckError(gone_message(ctx, discussion_id))
    if not any(isinstance(n, dict) and n.get("resolvable") for n in discussion.get("notes") or []):
        raise CheckError(not_resolvable_message(discussion_id))

    try:
        glab_api(
            f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/discussions/{discussion_id}",
            method="PUT",
            json_body={"resolved": resolved},
            hostname=ctx.hostname,
        )
    except GlabApiError as exc:
        if exc.http_status in (403, 404):
            raise CheckError(
                f"{not_resolvable_message(discussion_id)} (GitLab answered HTTP {exc.http_status})"
            ) from exc
        raise

    action = "Resolved" if resolved else "Unresolved"
    print(f"{action} discussion {discussion_id} - {ctx.mr_url}")
