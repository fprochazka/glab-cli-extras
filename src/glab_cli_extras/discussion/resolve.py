from __future__ import annotations

import argparse

from glab_cli_extras.discussion.context import resolve_mr_context
from glab_cli_extras.gitlab.sync_api import glab_api


def run(args: argparse.Namespace) -> None:
    ctx = resolve_mr_context(args)
    resolved = not args.unresolve

    glab_api(
        f"projects/{ctx.project_id}/merge_requests/{ctx.mr_iid}/discussions/{args.discussion_id}",
        method="PUT",
        json_body={"resolved": resolved},
        hostname=ctx.hostname,
    )

    action = "Resolved" if resolved else "Unresolved"
    print(f"{action} discussion {args.discussion_id} - {ctx.mr_url}")
