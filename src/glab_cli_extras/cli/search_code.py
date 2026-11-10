"""`glab-search-code`: the glab-search-code CLI, ported with its options and its summary lines.

The parser is argparse, as in glab-search-code. Progress goes to stderr; stdout carries only the summary, which ends
with the `Output directory:` and `Metadata file:` lines that callers read the paths from.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from glab_cli_extras import __version__
from glab_cli_extras.cli.stdio import force_utf8_stdio
from glab_cli_extras.gitlab.client import ApiError, GitLabClient
from glab_cli_extras.gitlab.glab import GlabNotFoundError, GlabRunner, ProcessResult, run_glab
from glab_cli_extras.gitlab.refs import RefError, resolve_repo
from glab_cli_extras.gitlab.retry import DEFAULT_ATTEMPTS, RetryPolicy
from glab_cli_extras.paths import base_dir
from glab_cli_extras.search.run import MAX_HITS, SearchError, SearchOptions, SearchSummary, run_search
from glab_cli_extras.store.layout import search_dir

MAX_WORKERS = 50

DESCRIPTION = """\
Run a GitLab code search (scope blobs) and download every file that matched, whole, into a new dir per run:
files/<project path>/<file path>, metadata.json, hits.json with the matched lines, and download.log.

A search across a group or the whole instance needs Advanced search or Exact code search on the GitLab instance;
--project works without it.

Exit codes: 0 the search ran, also with no hits or with failed downloads (see download.log); 1 the search failed;
2 a usage error."""

EPILOG = """\
Examples:
  glab-search-code 'GeneratedValue' --hostname gitlab.example.com
  glab-search-code 'class MyService' --group group --workers 20
  glab-search-code 'GeneratedValue' --project group/project

Take the paths from the last two lines of stdout, `Output directory:` and `Metadata file:`; never build them."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glab-search-code",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("search_query", help="The text to search for, as GitLab's code search reads it.")
    parser.add_argument(
        "--hostname",
        help="GitLab hostname (e.g. gitlab.com). Default: the host of the current directory's repository.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--project", help="Search one project (e.g. group/project) instead of the whole instance.")
    scope.add_argument("--group", help="Search one group and its subgroups instead of the whole instance.")
    parser.add_argument("--workers", type=int, default=10, help=f"Requests at once, 1 to {MAX_WORKERS} (default: 10).")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_ATTEMPTS,
        help=(
            f"Attempts per request, the first one included (default: {DEFAULT_ATTEMPTS}). A rate limit waits for its"
            " Retry-After; a 5xx or a network error waits longer after each attempt. 401, 403 and 404 are not retried."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "The dir to write into (default: a new dir <base>/<host>/-/search/<UTC time>-<query>/, where <base> is"
            " <session scratchpad>/glab-cli-extras under Claude Code, else <temp dir>/glab-cli-extras-<uid>)."
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Print every glab call and its HTTP status to stderr."
    )
    parser.add_argument("--version", action="version", version=f"glab-cli-extras {__version__}")
    return parser


def verbose_runner(stream: TextIO, runner: GlabRunner = run_glab) -> GlabRunner:
    """A runner that prints each glab call and the status line of its answer."""

    async def run(args: Sequence[str], timeout: float, stdin: bytes | None = None) -> ProcessResult:
        print(f"[verbose] exec: glab {shlex.join(args)}", file=stream, flush=True)
        result = await runner(args, timeout, stdin)
        status = result.stdout.split(b"\n", 1)[0].decode("latin-1").strip() or "no response"
        print(f"[verbose] exit {result.returncode}: {status}: {args[-1]}", file=stream, flush=True)
        return result

    return run


def make_client(host: str, workers: int, attempts: int, verbose: bool) -> GitLabClient:
    runner = verbose_runner(sys.stderr) if verbose else run_glab
    return GitLabClient(host, runner=runner, max_concurrency=workers, retry_reads=RetryPolicy(attempts=attempts))


def utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def format_summary(query: str, summary: SearchSummary) -> str:
    lines = [f"Search query: {query}"]
    if summary.hits == 0:
        lines.append("No results found")
    else:
        lines.append(f"Found {summary.hits} hits in {summary.files} files of {summary.projects} projects")
    if summary.truncated:
        lines.append(f"Search stopped after {MAX_HITS} hits; there may be more. Narrow the query, or pass --group.")
    if summary.hits:
        lines.append(f"Total files downloaded: {summary.downloaded}")
    if summary.skipped:
        lines.append(f"Files skipped: {summary.skipped}")
    if summary.failed:
        lines.append(f"Failed downloads: {summary.failed} (see {summary.log_file})")
    lines.append(f"Hits file: {summary.hits_file}")
    lines.append(f"Output directory: {summary.directory}")
    lines.append(f"Metadata file: {summary.metadata_file}")
    return "\n".join(lines) + "\n"


def _error(message: str) -> int:
    print(f"glab-search-code: error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    if args.max_retries < 1:
        parser.error("--max-retries must be at least 1")
    if not args.search_query.strip():
        parser.error("the search query is empty")
    try:
        host = args.hostname
        if not host:
            try:
                host = resolve_repo(None).host
            except RefError as exc:
                parser.error(f"pass --hostname; the current directory names no GitLab host ({exc})")
        directory = args.output_dir or search_dir(base_dir(), host, utc_stamp(), args.search_query)
        options = SearchOptions(
            host=host,
            query=args.search_query,
            directory=Path(directory),
            project=args.project,
            group=args.group,
            workers=args.workers,
        )
        summary = asyncio.run(run_search(options, make_client(host, args.workers, args.max_retries, args.verbose)))
    except (SearchError, ApiError, GlabNotFoundError, RefError, OSError) as exc:
        return _error(str(exc))
    print(format_summary(args.search_query, summary), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
