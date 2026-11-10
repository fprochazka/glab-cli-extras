"""`glab-mr-status`: what a merge request looks like now, and what changed since this reader last looked."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from glab_cli_extras import __version__
from glab_cli_extras.cli.stdio import force_utf8_stdio
from glab_cli_extras.gitlab.glab import GlabNotFoundError
from glab_cli_extras.gitlab.refs import RefError, ResourceRef, current_branch_mr, parse_id_ref
from glab_cli_extras.gitlab.retry import standalone_client
from glab_cli_extras.paths import base_dir
from glab_cli_extras.status.read import EXIT_FATAL, ReadOptions, run

DESCRIPTION = """\
Read one or more merge requests, print what changed since the last read, the current state and a scorecard, and
remember the state for the next read. The first read of an MR establishes a baseline.

Change ids: STATE_CHANGED, DRAFT_CHANGED, HEAD_CHANGED, TARGET_CHANGED, TITLE_CHANGED, PIPELINE_CHANGED,
BEHIND_CHANGED, MERGE_STATUS_CHANGED, NOTES_CHANGED, DISCUSSIONS_RESOLVED_CHANGED, REVIEWERS_CHANGED,
ASSIGNEES_CHANGED, LABELS_CHANGED, APPROVALS_CHANGED, READ_FAILED.

Exit codes: 0 a baseline or a read, 5 no MR could be read, 1 a fatal error, 2 a usage error.
To wait for a change instead of polling, run `glab-watch mr`."""

EPILOG = """\
Files, in the MR's dir <base>/<host>/<project>/-/merge_requests/<iid>/:
  mr.json                  the raw MR object, with the description
  external-statuses.json   the commit statuses posted from outside the pipeline
  threads/                 one file per thread, in the glab-discussion format
  status-baseline.json     this reader's last read
  status-events.log        every change it printed, in UTC
The pipeline dump goes into the pipeline's own dir.

Take every path from the output, never build one:
  state:                   the MR dir, or the base dir when the read covered several MRs
  discussions:             the threads dir, then the thread files this read rewrote, as updated:, new: and deleted:
  pipeline dump:           the pipeline's dir and its summary text, then each failed job with its stage and reason
  failed external status:  a failed commit status from outside the pipeline, with its URL

A thread file is rewritten only when its content changed: read only the thread files the discussions: line lists."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glab-mr-status",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "refs",
        nargs="*",
        metavar="MR",
        help="group/project!123, !123, 123 (repo from the cwd), or the MR URL. Default: the current branch's MR.",
    )
    parser.add_argument("--mr-url", action="append", default=[], help="An MR URL; repeat for a set.")
    parser.add_argument("--all", action="store_true", help="Fetch the threads and the pipeline dump (the default).")
    parser.add_argument("--comments", action="store_true", help="Fetch the threads only.")
    parser.add_argument("--pipeline", action="store_true", help="Fetch the head pipeline's dump only.")
    parser.add_argument("--reset", action="store_true", help="Forget this reader's baseline; the read starts anew.")
    parser.add_argument(
        "--base-dir",
        type=Path,
        help="Where the resource dirs go. Default: <session scratchpad>/glab-cli-extras under Claude Code, else "
        "<temp dir>/glab-cli-extras-<uid>.",
    )
    parser.add_argument("--version", action="version", version=f"glab-cli-extras {__version__}")
    return parser


def _error(message: str) -> int:
    print(f"glab-mr-status: error: {message}", file=sys.stderr)
    return EXIT_FATAL


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    # Line-buffered, so a caller reading the output as it comes sees each MR when it is printed.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    try:
        refs = [parse_id_ref("mr", arg) for arg in [*args.refs, *args.mr_url]]
        if not refs:
            mr = current_branch_mr()
            refs = [ResourceRef("mr", mr.host, mr.project, mr.iid)]
    except (RefError, GlabNotFoundError) as exc:
        if "no open merge request" in str(exc).lower():
            return _error("no open merge request for the current branch; push the branch and open one, or pass an MR")
        return _error(str(exc))
    explicit = args.comments or args.pipeline
    options = ReadOptions(
        base=args.base_dir or base_dir(),
        comments=args.all or args.comments or not explicit,
        pipeline=args.all or args.pipeline or not explicit,
        reset=args.reset,
    )
    return run(list(dict.fromkeys(refs)), options, standalone_client)


if __name__ == "__main__":
    sys.exit(main())
