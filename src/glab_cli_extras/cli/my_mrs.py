"""`glab-my-mrs`: your GitLab merge request dashboard as it is now, all of its lists or only some."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from glab_cli_extras import __version__
from glab_cli_extras.cli.stdio import force_utf8_stdio
from glab_cli_extras.dashboard.lists import (
    DUO_BOT,
    LAYOUT_OPTIONS,
    MERGED_LISTS,
    SIDES,
    ListFilter,
    ListFilterError,
    effective_layout,
    parse_list_filter,
)
from glab_cli_extras.dashboard.model import DashboardMr
from glab_cli_extras.dashboard.query import fetch_merged, fetch_open
from glab_cli_extras.dashboard.view import Dashboard, build, to_json, to_text
from glab_cli_extras.gitlab.client import ApiError, GitLabClient
from glab_cli_extras.gitlab.glab import GlabNotFoundError
from glab_cli_extras.gitlab.refs import RefError, parse_dashboard_ref
from glab_cli_extras.gitlab.retry import standalone_client

EXIT_FATAL = 1
EXIT_USAGE = 2

DESCRIPTION = f"""\
Print your GitLab merge request dashboard (https://<host>/dashboard/merge_requests) as it is now: each list with its
merge requests, their reviewers' states, head pipeline and when they last changed. The lists and the rules that put
an MR into them are the dashboard's own, in the layout your dashboard preference picks.

Lists, action-based: returned_to_you, reviews_requested, assigned_to_you, waiting_for_assignee,
waiting_for_approvals, approved_by_you, approved_by_others. Role-based: reviews, assigned, reviews_inactive.
The Merged tab, in both: merged_recently_reviews, merged_recently_assigned (shown only when --list names one).
Each list is on one side: author (you are the author or an assignee) or reviewer. The author-side lists ignore the
{DUO_BOT} bot as a reviewer, as the dashboard does.

Exit codes: 0 read, 1 an error, 2 a usage error.
To be told when the lists change, run `glab-watch my-mrs`."""

EPILOG = """\
Cost: one GraphQL request per 50 open MRs per side, and one more for the Merged tab when a merged list is asked for.
Nothing is written to disk."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glab-my-mrs",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "host",
        nargs="?",
        help="gitlab.example.com or any URL on the host, such as the dashboard's. Default: the host of the current "
        "directory's repository, else glab's default host.",
    )
    parser.add_argument(
        "--list",
        action="append",
        default=[],
        dest="lists",
        metavar="LIST",
        help="Show only these lists; comma-separated or repeated.",
    )
    parser.add_argument("--side", choices=SIDES, help="Show only the lists of one side.")
    parser.add_argument(
        "--layout",
        choices=LAYOUT_OPTIONS,
        default="auto",
        help="auto (the default) follows your dashboard preference; action or role picks the lists.",
    )
    parser.add_argument("--json", action="store_true", help="Print the lists as JSON instead of text.")
    parser.add_argument("--version", action="version", version=f"glab-cli-extras {__version__}")
    return parser


def _error(message: str, code: int = EXIT_FATAL) -> int:
    print(f"glab-my-mrs: error: {message}", file=sys.stderr)
    return code


def wants_merged(lists: ListFilter) -> bool:
    """Whether the Merged tab must be read: only when `--list` names a merged list."""
    return any(d.id in lists.lists and lists.matches(d.id) for d in MERGED_LISTS)


async def read_dashboard(client: GitLabClient, host: str, layout: str, lists: ListFilter) -> Dashboard:
    pinned = None if layout == "auto" else layout
    read = await fetch_open(client)
    merged: list[DashboardMr] = []
    if wants_merged(lists):
        merged, _, cut = await fetch_merged(client, datetime.now(UTC))
        read.truncated = read.truncated or cut
    return build(host, read, pinned, merged)


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        lists = parse_list_filter(args.lists, args.side)
        if args.layout != "auto":
            # A pinned layout is checked before any request.
            lists.selected(effective_layout(args.layout, None), include_merged=False)
    except ListFilterError as exc:
        return _error(str(exc), EXIT_USAGE)
    try:
        ref = parse_dashboard_ref(args.host)
        dashboard = asyncio.run(read_dashboard(standalone_client(ref.host), ref.host, args.layout, lists))
    except (RefError, GlabNotFoundError, ApiError) as exc:
        return _error(str(exc))
    try:
        selected = lists.selected(dashboard.layout, include_merged=False)
    except ListFilterError as exc:
        return _error(f"{exc}; pass --layout to pick the other layout", EXIT_USAGE)
    if not dashboard.duo_bot:
        warning = f"no user {DUO_BOT} on {ref.host}, so no reviewer is ignored as the Duo bot"
        print(f"glab-my-mrs: warning: {warning}", file=sys.stderr)
    if dashboard.truncated:
        print("glab-my-mrs: warning: more MRs than the command reads; some lists are incomplete", file=sys.stderr)
    if args.json:
        print(json.dumps(to_json(dashboard, selected), indent=2, ensure_ascii=False))
    else:
        print(to_text(dashboard, selected), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
