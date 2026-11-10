"""`glab-pipeline`: the glab-pipeline CLI, ported with its options and output.

The parser is argparse, as in glab-pipeline, so option abbreviations, usage errors (exit 2) and help stay the same.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from pathlib import Path

from glab_cli_extras import __version__
from glab_cli_extras.cli.deprecated import warn_if_installed
from glab_cli_extras.cli.stdio import force_utf8_stdio
from glab_cli_extras.gitlab.client import ApiError
from glab_cli_extras.gitlab.glab import GlabNotFoundError
from glab_cli_extras.gitlab.refs import RefError, ResourceRef, current_branch_mr, parse_mr_url, parse_pipeline_url
from glab_cli_extras.gitlab.sync_api import GlabApiError
from glab_cli_extras.paths import base_dir
from glab_cli_extras.pipeline.inspect import InspectOptions, make_client, run_inspect
from glab_cli_extras.pipeline.summary import format_summary
from glab_cli_extras.store.layout import pipeline_dir
from glab_cli_extras.store.locks import LockTimeout
from glab_cli_extras.sync.job_logs import LOGS_MODES
from glab_cli_extras.sync.mr import fetch_snapshot


def _make_context_parent() -> argparse.ArgumentParser:
    """Argparse parent parser with shared pipeline-context flags."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--pipeline-url",
        help="Full GitLab pipeline URL (https://<host>/<project>/-/pipelines/<id>).",
    )
    parent.add_argument(
        "--pipeline-id",
        type=int,
        help="Numeric pipeline ID (requires --project/--hostname or git auto-detect).",
    )
    parent.add_argument(
        "--mr-url",
        help="Full GitLab merge request URL; uses head_pipeline.",
    )
    parent.add_argument(
        "--mr-iid",
        type=int,
        help="Merge request IID (requires --project and --hostname).",
    )
    parent.add_argument(
        "--project",
        help="Project path (e.g. group/subgroup/repo).",
    )
    parent.add_argument(
        "--hostname",
        help="GitLab hostname (e.g. gitlab.com).",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glab-pipeline",
        description="Agent-friendly CLI for inspecting GitLab CI pipelines.",
    )
    parser.add_argument("--version", action="version", version=f"glab-cli-extras {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    inspect_parser = subparsers.add_parser(
        "inspect",
        parents=[_make_context_parent()],
        help="Dump full pipeline state and print a problem-driven summary.",
        description=(
            "Dump the full state of a GitLab CI pipeline (pipeline.json, jobs.json, bridges.json, the job logs, plus "
            "conditional lint/test-report/downstream fetches) and print a problem-driven summary to stdout. The dump "
            "goes to the pipeline's dir, which glab-watch also writes, and a rerun reuses what is still current."
        ),
    )
    inspect_parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Directory to write dump files into (default: the pipeline's dir under <session scratchpad>/glab-cli-extras"
            " under Claude Code, else under <temp dir>/glab-cli-extras-<uid>)."
        ),
    )
    inspect_parser.add_argument(
        "--with-merged-ci-config",
        action="store_true",
        help=(
            "Force a two-step lint that resolves include: against the source "
            "branch (writes lint.json + merged.yml even when no state trigger fires)."
        ),
    )
    inspect_parser.add_argument(
        "--with-test-report",
        action="store_true",
        help="Force test-report fetch even when no failed test-stage job is present.",
    )
    inspect_parser.add_argument(
        "--with-downstream-pipelines",
        action="store_true",
        help="Force downstream pipeline detail fetch for every bridge, not just failed ones.",
    )
    inspect_parser.add_argument(
        "--with-artefacts",
        action="store_true",
        help="Download and unpack each job's artifacts archive (one zip per job) into artifacts/<stage>-<name>-<id>/.",
    )
    inspect_parser.add_argument(
        "--logs",
        choices=LOGS_MODES,
        default="failed",
        help=(
            "Which job logs to download: failed (default), all, or none. Every other job gets a placeholder file "
            "that says why its log is not there."
        ),
    )
    inspect_parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore what is on disk and fetch everything again.",
    )
    inspect_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the structured summary JSON to stdout instead of the "
            "human-readable text. summary.json is always written to the dump "
            "dir regardless of this flag."
        ),
    )
    return parser


def _error(message: str) -> int:
    print(f"glab-pipeline: error: {message}", file=sys.stderr)
    return 1


def _command(args: argparse.Namespace, ref: ResourceRef) -> str:
    """The `glab-pipeline inspect ... --logs=all` command for this pipeline, in the form the user passed."""
    parts = ["glab-pipeline", "inspect"]
    if args.pipeline_id and args.project and args.hostname:
        parts += ["--pipeline-id", str(ref.id), "--project", ref.project, "--hostname", ref.host]
    else:
        parts += ["--pipeline-url", f"https://{ref.host}/{ref.project}/-/pipelines/{ref.id}"]
    if args.output_dir:
        parts += ["--output-dir", str(args.output_dir)]
    parts.append("--logs=all")
    return shlex.join(parts)


async def _resolve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[ResourceRef, str | None]:
    """The pipeline the flags name, and its source branch when the flags name an MR. Precedence as in glab-pipeline:
    --pipeline-url, --pipeline-id, --mr-url/--mr-iid, then the current branch's MR. A pipeline named by its id gets
    its branch from its own ref, in the lint, since it need not be the current MR's."""
    if args.pipeline_url:
        host, project, pipeline_id = parse_pipeline_url(args.pipeline_url)
        return ResourceRef("pipeline", host, project, pipeline_id), None
    if args.pipeline_id:
        if args.project and args.hostname:
            return ResourceRef("pipeline", args.hostname, args.project, args.pipeline_id), None
        if args.project:
            parser.error("--pipeline-id with --project needs --hostname too, or pass --pipeline-url")
        mr = current_branch_mr()
        return ResourceRef("pipeline", mr.host, mr.project, args.pipeline_id), None
    if args.mr_url or args.mr_iid:
        if args.mr_url:
            host, project, iid = parse_mr_url(args.mr_url)
        else:
            if not (args.hostname and args.project):
                raise ValueError("--mr-iid requires --hostname and --project (or use --mr-url)")
            host, project, iid = args.hostname, args.project, args.mr_iid
        client = make_client(host)
        snap = await fetch_snapshot(
            client, ResourceRef("mr", host, project, iid), with_threads=False, with_pipeline=True
        )
        if snap.head_pipeline_id is None:
            raise ValueError(f"No pipeline found for MR !{iid} in {project}")
        return ResourceRef("pipeline", host, snap.head_pipeline_project or project, snap.head_pipeline_id), (
            snap.source_branch
        )
    if args.project:
        parser.error("--project needs --pipeline-id or --mr-iid, and --hostname")
    mr = current_branch_mr()
    if mr.head_pipeline_id is None:
        raise ValueError("No pipeline found for current MR")
    return ResourceRef("pipeline", mr.host, mr.project, mr.head_pipeline_id), mr.source_branch


def _run_inspect(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    async def main() -> dict[str, object]:
        ref, branch = await _resolve(args, parser)
        assert ref.id is not None
        directory = args.output_dir or pipeline_dir(base_dir(), ref.host, ref.project, ref.id)
        options = InspectOptions(
            ref=ref,
            directory=Path(directory),
            command=_command(args, ref),
            logs=args.logs,
            full=args.full,
            branch=branch,
            with_merged_ci_config=args.with_merged_ci_config,
            with_test_report=args.with_test_report,
            with_downstream_pipelines=args.with_downstream_pipelines,
            with_artefacts=args.with_artefacts,
        )
        return await run_inspect(options, make_client(ref.host))

    try:
        summary = asyncio.run(main())
    except (ApiError, GlabApiError, RefError, GlabNotFoundError, LockTimeout, ValueError, OSError) as exc:
        return _error(str(exc))
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(format_summary(summary), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    warn_if_installed("glab-pipeline")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "inspect":
        parser.print_help()
        return 0
    return _run_inspect(args, parser)


if __name__ == "__main__":
    sys.exit(main())
