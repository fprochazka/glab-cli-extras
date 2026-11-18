"""`glab-watch`: report changes to a merge request, a pipeline, a project's merge requests, or a branch."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from cli_watch_kit.claude import MESSAGE_PRIORITIES, STREAM_MODES
from cli_watch_kit.cli import WatchOptions, format_status
from cli_watch_kit.cli import run_watch as kit_run_watch
from cli_watch_kit.client import BackendError, WriterContext
from cli_watch_kit.daemon.lifecycle import daemon_running, stop_daemon
from cli_watch_kit.daemon.transport import DaemonFiles, DaemonTimeout, DaemonUnavailable, request
from cli_watch_kit.durations import parse_duration
from cli_watch_kit.sinks import StatusOutput

from glab_cli_extras import __version__
from glab_cli_extras.cli.stdio import force_utf8_stdio
from glab_cli_extras.events import (
    CATALOG,
    KINDS_BY_RESOURCE,
    MR_PIPELINE_KINDS,
    PIPELINE_STUCK,
    QUIET,
    FilterError,
)
from glab_cli_extras.gitlab.glab import GlabNotFoundError
from glab_cli_extras.gitlab.refs import RefError, ResourceRef, parse_id_ref, parse_repo_ref
from glab_cli_extras.paths import base_dir as default_base_dir
from glab_cli_extras.store.layout import readme_text, resource_dir
from glab_cli_extras.sync.job_logs import LOGS_MODES, LogsMode
from glab_cli_extras.watch.app import APP, sender_name
from glab_cli_extras.watch.source import ref_of, target_of
from glab_cli_extras.watch.timers import Thresholds, WatchTimers
from glab_cli_extras.watch.writer import DaemonFetches, ResourceWriter

__all__ = ["also_subscribe", "app", "main", "parse_duration", "restart_ref"]

EXIT_USAGE = 2

app = typer.Typer(
    name="glab-watch",
    help=(
        "Watch a GitLab merge request, pipeline, project or branch and report each change as a JSON event. "
        "Uses the glab CLI for every request; a shared background daemon keeps many watchers cheap on the API."
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    context_settings={"help_option_names": ["-h", "--help"]},
)
daemon_app = typer.Typer(help="Inspect or stop the shared polling daemon.", no_args_is_help=True)
app.add_typer(daemon_app, name="daemon")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"glab-watch {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", "-V", callback=_version, is_eager=True, help="Print the version and exit.")
    ] = False,
) -> None:
    pass


EventsOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--events",
        help="Report only these event kinds or groups, comma-separated (groups: status, comments, push, pipeline).",
    ),
]
ExcludeOpt = Annotated[
    list[str] | None,
    typer.Option("--exclude-events", help="Do not report these event kinds or groups, comma-separated."),
]
StreamOpt = Annotated[
    str,
    typer.Option(
        "--events-stream",
        help="auto picks claude-message inside a Claude Code session that accepts messages, else stdout.",
    ),
]
BaseDirOpt = Annotated[
    Path | None,
    typer.Option(
        "--base-dir",
        help="Where the resource dirs go (state.json, logs, threads, events.jsonl). "
        "Default: <session scratchpad>/glab-cli-extras under Claude Code, else <temp dir>/glab-cli-extras-<uid>.",
    ),
]
UntilOpt = Annotated[str | None, typer.Option("--until", help="Stop after the first event of this kind.")]
TimeoutOpt = Annotated[
    str | None, typer.Option("--timeout", help="Stop after this long, for example 90s, 30m, 2h, 1h30m.")
]
IdleAfterOpt = Annotated[
    str | None,
    typer.Option(
        "--idle-after",
        help="Stop when nothing was reported for this long, for example 3h. Every reported event starts the count "
        "again; quiet and pipeline_stuck do not.",
    ),
]
NoDaemonOpt = Annotated[
    bool, typer.Option("--no-daemon", help="Poll inside this process with a private budget, without the daemon.")
]
PriorityOpt = Annotated[
    str,
    typer.Option(
        "--message-priority",
        help="claude-message only: now or next deliver into the running turn, later waits until the session is idle.",
    ),
]
QuietAfterOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--quiet-after",
        help="Report quiet once per head when the head pipeline succeeded on it and nothing else was reported for "
        "this long, for example 2m. Repeatable for several thresholds.",
    ),
]
StuckAfterOpt = Annotated[
    str | None,
    typer.Option(
        "--stuck-after",
        help="Report pipeline_stuck once per pipeline still running or pending this long after the watch first saw "
        "it so, for example 30m.",
    ),
]
LogsOpt = Annotated[str, typer.Option("--logs", help="Which finished jobs get their log fetched: failed, all or none.")]


def _status() -> StatusOutput:
    return StatusOutput(APP.status_prefix)


def _fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    _status().line(f"error: {message}")
    raise typer.Exit(code)


def restart_ref(ref: ResourceRef) -> str:
    """The resource as a command-line argument that resolves without the cwd."""
    base = f"https://{ref.host}/{ref.project}"
    if ref.kind == "mr":
        return f"{base}/-/merge_requests/{ref.id}"
    if ref.kind == "pipeline":
        return f"{base}/-/pipelines/{ref.id}"
    if ref.kind == "branch":
        return f"{base} {ref.branch}"
    return base


def also_subscribe(kind: str, enabled: list[str]) -> list[str]:
    """What the daemon must track for the threshold events without the watcher reporting it: `quiet` and
    `pipeline_stuck` of an MR read the head pipeline from the MR snapshot, which then has to carry it."""
    return ["head_pipeline_changed", *MR_PIPELINE_KINDS] if kind == "mr" and enabled else []


def _duration(option: str, value: str) -> float:
    try:
        seconds = parse_duration(value)
    except ValueError as exc:
        _fail(f"{option}: {exc}")
    if seconds <= 0:
        _fail(f"{option}: the duration must be longer than zero")
    return seconds


def _header(ref: ResourceRef, base: Path | None, logs: str | None) -> list[str]:
    """glab-watch's own header lines: the dir's `state.json` and the `--logs` mode."""
    lines = []
    if ref.kind in ("mr", "pipeline"):
        lines.append(f"state: {resource_dir(default_base_dir(base), ref) / 'state.json'}")
    if logs is not None:
        lines.append(f"logs: {logs}")
    return lines


def run_watch(
    resolve: Callable[[], ResourceRef],
    kind: str,
    *,
    events: list[str] | None,
    exclude_events: list[str] | None,
    events_stream: str,
    base_dir: Path | None,
    until: str | None,
    timeout: str | None,
    no_daemon: bool,
    message_priority: str,
    logs: str | None,
    quiet_after: list[str] | None = None,
    stuck_after: str | None = None,
    idle_after: str | None = None,
) -> None:
    """Check every option before anything starts (no glab call, no daemon), then run the watch on cli-watch-kit."""
    if events_stream not in STREAM_MODES:
        _fail(f"--events-stream must be one of {', '.join(STREAM_MODES)}")
    if message_priority not in MESSAGE_PRIORITIES:
        _fail(f"--message-priority must be one of {', '.join(MESSAGE_PRIORITIES)}")
    if logs is not None and logs not in LOGS_MODES:
        _fail(f"--logs must be one of {', '.join(LOGS_MODES)}")
    thresholds = Thresholds(
        quiet_after=tuple(sorted({_duration("--quiet-after", v) for v in quiet_after or ()})),
        stuck_after=_duration("--stuck-after", stuck_after) if stuck_after else None,
    )
    enabled = ([QUIET] if thresholds.quiet_after else []) + ([PIPELINE_STUCK] if thresholds.stuck_after else [])
    try:
        selected = CATALOG.resolve(kind, events, exclude_events, enabled)
    except FilterError as exc:
        _fail(str(exc))
    if until is not None and until not in KINDS_BY_RESOURCE[kind]:
        _fail(f"--until: a {kind} watch never produces {until!r}")
    if until is not None and until not in selected:
        _fail(f"--until={until} is filtered out by --events/--exclude-events")
    for option, value in (("--timeout", timeout), ("--idle-after", idle_after)):
        if value is not None:
            _duration(option, value)
    try:
        ref = resolve()
    except (RefError, GlabNotFoundError) as exc:
        _fail(str(exc))

    logs_mode: LogsMode = logs if logs in LOGS_MODES else "failed"
    options = WatchOptions(
        events=tuple(events or ()),
        exclude_events=tuple(exclude_events or ()),
        until=until,
        timeout=timeout,
        idle_after=idle_after,
        events_stream=events_stream,
        message_priority=message_priority,
        base_dir=base_dir,
        no_daemon=no_daemon,
    )
    timers = [WatchTimers(kind, thresholds, time.monotonic())] if enabled else []
    also = also_subscribe(kind, enabled)
    restart = f"glab-watch {kind} {restart_ref(ref)}" + (" --logs=failed|all|none" if logs is not None else "")

    def writer(context: WriterContext) -> ResourceWriter:
        return ResourceWriter(
            context.base_dir, ref_of(context.target), DaemonFetches(context.backend), logs_mode, context.status
        )

    code = kit_run_watch(
        APP,
        target_of(ref),
        options,
        source_options={"logs": logs_mode} if kind == "pipeline" else None,
        enabled_thresholds=enabled,
        also_subscribe=also,
        timers=timers,
        writer=writer,
        resource_dir=lambda base, target: resource_dir(base, ref_of(target)),
        readme=readme_text(kind),
        sender_name=sender_name(ref),
        restart=restart,
        header=_header(ref, base_dir, logs),
        status=_status(),
    )
    raise typer.Exit(code)


@app.command()
def mr(
    ref: Annotated[str, typer.Argument(help="group/project!123, !123, 123 (repo from the cwd), or the MR URL.")],
    events: EventsOpt = None,
    exclude_events: ExcludeOpt = None,
    until: UntilOpt = None,
    timeout: TimeoutOpt = None,
    idle_after: IdleAfterOpt = None,
    quiet_after: QuietAfterOpt = None,
    stuck_after: StuckAfterOpt = None,
    events_stream: StreamOpt = "auto",
    base_dir: BaseDirOpt = None,
    message_priority: PriorityOpt = "next",
    no_daemon: NoDaemonOpt = False,
) -> None:
    """Watch one merge request: state, pushes, approvals, threads, and its head pipeline's status. Ends when merged or
    closed. For the head pipeline's jobs, watch the pipeline: each pipeline event names the command."""
    run_watch(
        lambda: parse_id_ref("mr", ref),
        "mr",
        events=events,
        exclude_events=exclude_events,
        events_stream=events_stream,
        base_dir=base_dir,
        until=until,
        timeout=timeout,
        idle_after=idle_after,
        no_daemon=no_daemon,
        message_priority=message_priority,
        logs=None,
        quiet_after=quiet_after,
        stuck_after=stuck_after,
    )


@app.command()
def pipeline(
    ref: Annotated[str, typer.Argument(help="group/project!4567, !4567, 4567 (repo from the cwd), or the URL.")],
    events: EventsOpt = None,
    exclude_events: ExcludeOpt = None,
    logs: LogsOpt = "failed",
    until: UntilOpt = None,
    timeout: TimeoutOpt = None,
    idle_after: IdleAfterOpt = None,
    stuck_after: StuckAfterOpt = None,
    events_stream: StreamOpt = "auto",
    base_dir: BaseDirOpt = None,
    message_priority: PriorityOpt = "next",
    no_daemon: NoDaemonOpt = False,
) -> None:
    """Watch one pipeline and its downstream pipelines. Ends when it finishes and the wanted logs are fetched."""
    run_watch(
        lambda: parse_id_ref("pipeline", ref),
        "pipeline",
        events=events,
        exclude_events=exclude_events,
        events_stream=events_stream,
        base_dir=base_dir,
        until=until,
        timeout=timeout,
        idle_after=idle_after,
        no_daemon=no_daemon,
        message_priority=message_priority,
        logs=logs,
        stuck_after=stuck_after,
    )


@app.command()
def mrs(
    repo: Annotated[str | None, typer.Argument(help="group/project or a project URL; default: the cwd's repo.")] = None,
    events: EventsOpt = None,
    exclude_events: ExcludeOpt = None,
    until: UntilOpt = None,
    timeout: TimeoutOpt = None,
    idle_after: IdleAfterOpt = None,
    events_stream: StreamOpt = "auto",
    base_dir: BaseDirOpt = None,
    message_priority: PriorityOpt = "next",
    no_daemon: NoDaemonOpt = False,
) -> None:
    """Watch every merge request of a project: opened, updated, merged, closed, reopened. Runs until stopped."""
    run_watch(
        lambda: parse_repo_ref("mrs", repo),
        "mrs",
        events=events,
        exclude_events=exclude_events,
        events_stream=events_stream,
        base_dir=base_dir,
        until=until,
        timeout=timeout,
        idle_after=idle_after,
        no_daemon=no_daemon,
        message_priority=message_priority,
        logs=None,
    )


@app.command()
def branch(
    args: Annotated[list[str], typer.Argument(metavar="[REPO] BRANCH", help="The branch, optionally after the repo.")],
    events: EventsOpt = None,
    exclude_events: ExcludeOpt = None,
    until: UntilOpt = None,
    timeout: TimeoutOpt = None,
    idle_after: IdleAfterOpt = None,
    events_stream: StreamOpt = "auto",
    base_dir: BaseDirOpt = None,
    message_priority: PriorityOpt = "next",
    no_daemon: NoDaemonOpt = False,
) -> None:
    """Watch a branch: pushes, force-pushes, creation, deletion, and its pipelines. Runs until stopped."""
    if len(args) not in (1, 2):
        _fail("expected [REPO] BRANCH")
    repo, name = (None, args[0]) if len(args) == 1 else (args[0], args[1])
    run_watch(
        lambda: parse_repo_ref("branch", repo, name),
        "branch",
        events=events,
        exclude_events=exclude_events,
        events_stream=events_stream,
        base_dir=base_dir,
        until=until,
        timeout=timeout,
        idle_after=idle_after,
        no_daemon=no_daemon,
        message_priority=message_priority,
        logs=None,
    )


@daemon_app.command("status")
def daemon_status(
    as_json: Annotated[bool, typer.Option("--json", help="Print the raw status object.")] = False,
) -> None:
    """Show the daemon's subscriptions, resources, budgets and request counts."""
    files = DaemonFiles.for_app(APP)
    try:
        response = request(files, {"op": "status"}, timeout=5)
    except (DaemonUnavailable, DaemonTimeout):
        if not daemon_running(files):
            typer.echo("daemon: not running")
            raise typer.Exit(0) from None
        pid = files.owner_pid()
        typer.echo(
            f"daemon: running (pid {pid if pid is not None else 'unknown'}) but not answering; see"
            " `glab-watch daemon logs`, and `glab-watch daemon stop` ends it"
        )
        raise typer.Exit(1) from None
    if as_json:
        typer.echo(json.dumps(response, indent=2))
        return
    for line in format_status(response, files):
        typer.echo(line)


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the daemon. Running watchers start a new one on their next poll."""
    files = DaemonFiles.for_app(APP)
    try:
        # A daemon that does not stop by itself (a hung one) is killed, once its pid is verified.
        stopped = stop_daemon(files, APP.env_float("STOP_TIMEOUT", 10.0), kill=APP)
    except DaemonUnavailable as exc:
        _fail(str(exc), 1)
    typer.echo("daemon: stopped" if stopped else "daemon: not running")


@daemon_app.command("logs")
def daemon_logs(
    lines: Annotated[int, typer.Option("--lines", "-n", help="How many lines from the end.")] = 50,
) -> None:
    """Print the end of the daemon's log file."""
    files = DaemonFiles.for_app(APP)
    try:
        content = files.log.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        typer.echo(f"no daemon log at {files.log}")
        return
    typer.echo(f"==> {files.log}")
    for line in content[-lines:]:
        typer.echo(line)


def main() -> None:
    force_utf8_stdio()
    try:
        app()
    except (BackendError, DaemonUnavailable) as exc:
        _status().line(f"error: {exc}")
        sys.exit(1)
