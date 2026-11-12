"""`glab-discussion`: the glab-discussion CLI, ported with its options and output.

The parser is argparse, as in glab-discussion, so option abbreviations, usage errors (exit 2) and help stay the same.
"""

from __future__ import annotations

import argparse
import sys

from glab_cli_extras import __version__
from glab_cli_extras.cli.deprecated import warn_if_installed
from glab_cli_extras.cli.stdio import force_utf8_stdio
from glab_cli_extras.config import ConfigError, Settings, load_settings
from glab_cli_extras.discussion.checks import CheckError, UsageError
from glab_cli_extras.discussion.context import ContextError
from glab_cli_extras.discussion.ids import parse_note_ref, parse_published_note_ref
from glab_cli_extras.gitlab.client import ApiError
from glab_cli_extras.gitlab.glab import GlabNotFoundError
from glab_cli_extras.gitlab.refs import RefError
from glab_cli_extras.gitlab.sync_api import GlabApiError
from glab_cli_extras.store.locks import LockTimeout

NOTE_REF_METAVAR = "note:ID|draft:ID"
NOTE_REF_HELP = "as 'read' prints it: note:123 for a published note, draft:123 for your pending draft"
PUBLISHED_REF_METAVAR = "note:ID"
PUBLISHED_REF_HELP = "as 'read' prints it: note:123"


def build_parser(settings: Settings | None = None) -> argparse.ArgumentParser:
    """The parser. When the `discussion.drafts` setting turns drafts off, it has no draft command or option at all,
    so that `--help` does not offer them and using one is a usage error."""
    drafts = settings is None or not settings.drafts_disabled
    ref_type, ref_metavar, ref_help = (
        (parse_note_ref, NOTE_REF_METAVAR, NOTE_REF_HELP)
        if drafts
        else (parse_published_note_ref, PUBLISHED_REF_METAVAR, PUBLISHED_REF_HELP)
    )
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
    parser.add_argument("--version", action="version", version=f"glab-cli-extras {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- read ---
    read_parser = subparsers.add_parser("read", parents=[mr_parent], help="Read and display MR discussions")
    dump_group = read_parser.add_mutually_exclusive_group()
    dump_group.add_argument("--dump", action="store_true", default=False, help="Dump discussions as structured data")
    dump_group.add_argument("--no-dump", action="store_true", default=False, help="Human-readable format")
    read_parser.add_argument("--full", action="store_true", default=False, help="Force full rewrite (only with --dump)")

    # --- write ---
    write_parser = subparsers.add_parser("write", parents=[mr_parent], help="Create a new discussion or reply")
    write_parser.add_argument("--body", required=True, help='Note body text (use "-" for stdin)')
    write_parser.add_argument("--reply-to", metavar="DISCUSSION_ID", help="Reply to an existing discussion")
    write_parser.add_argument(
        "--note",
        action="store_true",
        default=False,
        help="Post a plain comment, which cannot be resolved, instead of a new thread. Posted at once, never a draft",
    )
    write_parser.add_argument(
        "--attach",
        action="append",
        metavar="FILE",
        help="Upload a file to the project and link it after the body text; images, video and audio show inline."
        " Repeat for more files",
    )
    write_parser.add_argument("--file", help="File path for a diff note")
    write_parser.add_argument("--new-line", type=int, help="New-side line number for diff note")
    write_parser.add_argument("--old-line", type=int, help="Old-side line number for diff note")
    write_parser.add_argument("--commit", metavar="SHA", help="Commit SHA for diff note")
    if drafts:
        draft_group = write_parser.add_mutually_exclusive_group()
        draft_group.add_argument(
            "--draft",
            action="store_true",
            default=False,
            help="Write a pending draft that only you see until 'drafts publish'"
            " (default: the GLAB_DISCUSSION_WRITE_AS_DRAFT environment variable)",
        )
        draft_group.add_argument(
            "--no-draft",
            action="store_true",
            default=False,
            help="Publish the comment immediately, even if GLAB_DISCUSSION_WRITE_AS_DRAFT is true",
        )
        write_parser.add_argument(
            "--resolve",
            action="store_true",
            default=False,
            help="Resolve the thread when the draft is published (only with --reply-to and a draft)",
        )

    # --- diff ---
    diff_parser = subparsers.add_parser("diff", parents=[mr_parent], help="Show MR diff information")
    diff_parser.add_argument("--file", help="Filter to a single file path")
    diff_parser.add_argument("--version", type=int, help="Specific diff version ID")

    # --- resolve ---
    resolve_parser = subparsers.add_parser("resolve", parents=[mr_parent], help="Resolve or unresolve a discussion")
    resolve_parser.add_argument("discussion_id", help="Discussion ID to resolve")
    resolve_parser.add_argument("--unresolve", action="store_true", default=False, help="Unresolve instead of resolve")

    # --- delete ---
    delete_parser = subparsers.add_parser(
        "delete", parents=[mr_parent], help="Delete a note or a draft" if drafts else "Delete a note"
    )
    delete_parser.add_argument("note_ref", type=ref_type, metavar=ref_metavar, help=f"ID to delete, {ref_help}")

    # --- edit ---
    edit_parser = subparsers.add_parser(
        "edit", parents=[mr_parent], help="Edit a note or a draft" if drafts else "Edit a note"
    )
    edit_parser.add_argument("note_ref", type=ref_type, metavar=ref_metavar, help=f"ID to edit, {ref_help}")
    edit_parser.add_argument("--body", required=True, help='New note body text (use "-" for stdin)')

    if drafts:
        # --- drafts ---
        drafts_parser = subparsers.add_parser("drafts", help="Publish or delete your pending drafts")
        drafts_subparsers = drafts_parser.add_subparsers(dest="drafts_command", required=True)

        publish_parser = drafts_subparsers.add_parser(
            "publish",
            parents=[mr_parent],
            help="Publish all your drafts as one review",
            description="Publish all your drafts on the MR as one review, with one notification.",
        )
        publish_parser.add_argument(
            "--body", help='Summary comment, added as a draft and published with the review (use "-" for stdin)'
        )
        publish_parser.add_argument(
            "--verdict",
            choices=["reviewed", "requested-changes", "approve"],
            help="The verdict of the review. Adds you as a reviewer if you are not one. Needs GitLab 16.7 or newer."
            " Default: reviewed, if you have not given the MR a verdict yet; otherwise --verdict is required.",
        )

        drafts_delete_parser = drafts_subparsers.add_parser(
            "delete",
            parents=[mr_parent],
            help="Delete all your drafts",
            description="List all your drafts on the MR. With --force, delete them.",
        )
        drafts_delete_parser.add_argument(
            "--force", action="store_true", default=False, help="Delete the drafts instead of listing them"
        )
    return parser


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "read":
        from glab_cli_extras.discussion.read import run

        run(args)
    elif args.command == "write":
        from glab_cli_extras.discussion.write import run as run_write

        run_write(args)
    elif args.command == "diff":
        from glab_cli_extras.discussion.diff import run as run_diff

        run_diff(args)
    elif args.command == "resolve":
        from glab_cli_extras.discussion.resolve import run as run_resolve

        run_resolve(args)
    elif args.command == "delete":
        from glab_cli_extras.discussion.delete import run as run_delete

        run_delete(args)
    elif args.command == "edit":
        from glab_cli_extras.discussion.edit import run as run_edit

        run_edit(args)
    elif args.command == "drafts" and args.drafts_command == "publish":
        from glab_cli_extras.discussion.drafts import run_publish

        run_publish(args)
    elif args.command == "drafts" and args.drafts_command == "delete":
        from glab_cli_extras.discussion.drafts import run_delete as run_drafts_delete

        run_drafts_delete(args)
    else:
        parser.print_help()
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    force_utf8_stdio()
    warn_if_installed("glab-discussion")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    parser = build_parser(settings)
    args = parser.parse_args(argv)
    args.settings = settings
    try:
        _dispatch(args, parser)
    except (ContextError, UsageError) as exc:
        parser.error(str(exc))
    except CheckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except (ApiError, GlabApiError, RefError, GlabNotFoundError, LockTimeout, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
