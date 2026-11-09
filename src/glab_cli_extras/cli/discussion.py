"""`glab-discussion`: the glab-discussion CLI, ported with its options and output.

The parser is argparse, as in glab-discussion, so option abbreviations, usage errors (exit 2) and help stay the same.
"""

from __future__ import annotations

import argparse
import sys

from glab_cli_extras.discussion.context import ContextError
from glab_cli_extras.gitlab.client import ApiError
from glab_cli_extras.gitlab.glab import GlabNotFoundError
from glab_cli_extras.gitlab.refs import RefError
from glab_cli_extras.gitlab.sync_api import GlabApiError
from glab_cli_extras.store.locks import LockTimeout


def _force_utf8_stdio() -> None:
    """Bodies piped in with --body - and the discussions printed out are UTF-8, whatever the console code page is.

    Without this, a cp1252 console (Windows default) makes Python mis-decode piped input and crash on emoji
    or diacritics in the output. Streams replaced by test harnesses may lack reconfigure(); skip those.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    # Shared parent parser for MR context flags
    mr_parent = argparse.ArgumentParser(add_help=False)
    mr_parent.add_argument("--mr-url", help="Full URL of the merge request")
    mr_parent.add_argument("--hostname", help="GitLab hostname (e.g. gitlab.com)")
    mr_parent.add_argument("--project", help="GitLab project path (e.g. group/project)")
    mr_parent.add_argument("--mr-iid", type=int, help="Merge request IID")

    parser = argparse.ArgumentParser(
        prog="glab-discussion",
        description="CLI wrapper around GitLab Discussions REST API for managing MR discussions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- read ---
    read_parser = subparsers.add_parser("read", parents=[mr_parent], help="Read and display MR discussions")
    dump_group = read_parser.add_mutually_exclusive_group()
    dump_group.add_argument("--dump", action="store_true", default=False, help="Dump discussions as structured data")
    dump_group.add_argument("--no-dump", action="store_true", default=False, help="Human-readable format")
    read_parser.add_argument("--full", action="store_true", default=False, help="Force full rewrite (only with --dump)")
    return parser


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "read":
        from glab_cli_extras.discussion.read import run

        run(args)
    else:
        parser.print_help()
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _dispatch(args, parser)
    except ContextError as exc:
        parser.error(str(exc))
    except (ApiError, GlabApiError, RefError, GlabNotFoundError, LockTimeout, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
