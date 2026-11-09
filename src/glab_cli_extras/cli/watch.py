"""`glab-watch`: report changes to a merge request, a pipeline, a project's merge requests, or a branch."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from glab_cli_extras import __version__
from glab_cli_extras.daemon.lifecycle import daemon_running, stop_daemon
from glab_cli_extras.daemon.transport import DaemonFiles, DaemonUnavailable, request
from glab_cli_extras.events import GROUPS, KINDS_BY_RESOURCE, LIFECYCLE_KINDS, FilterError, resolve_filter
from glab_cli_extras.gitlab.glab import GlabNotFoundError
from glab_cli_extras.gitlab.refs import RefError, ResourceRef, parse_id_ref, parse_repo_ref
from glab_cli_extras.sync.job_logs import LOGS_MODES, LogsMode
from glab_cli_extras.watch.changelog import Changelog
from glab_cli_extras.watch.client import Backend, BackendError, Watcher, WatchSettings, default_backend
from glab_cli_extras.watch.sinks import Sink, StatusOutput
from glab_cli_extras.watch.streams import MESSAGE_PRIORITIES, STREAM_MODES, StreamError, open_stream
from glab_cli_extras.watch.writer import ResourceWriter

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
LogsOpt = Annotated[str, typer.Option("--logs", help="Which finished jobs get their log fetched: failed, all or none.")]

_DURATION_RE = re.compile(r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$")


def parse_duration(value: str) -> float:
    text = value.strip().lower()
    if text.replace(".", "", 1).isdigit():
        return float(text)
    match = _DURATION_RE.match(text)
    if not text or not match or not any(match.groupdict().values()):
        raise ValueError(f"cannot read the duration {value!r}; use for example 90s, 30m, 2h or 1h30m")
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return float(parts["h"] * 3600 + parts["m"] * 60 + parts["s"])


def _fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    StatusOutput(sys.stderr).line(f"error: {message}")
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


def print_header(
    status: StatusOutput,
    ref: ResourceRef,
    sink: Sink,
    changelog: Changelog,
    events: frozenset[str],
    logs: LogsMode | None,
    backend: Backend,
    until: str | None,
    timeout: str | None,
    fallback: str | None = None,
) -> None:
    """The startup header: what is watched, how events are delivered, and how to restart with other options."""
    tracked = KINDS_BY_RESOURCE[ref.kind]
    subscribed = [k for k in tracked if k in events]
    ignored = [k for k in tracked if k not in events]
    status.line(f"resource: {ref.key}")
    status.line(f"stream: {sink.describe()}")
    if fallback:
        status.line(f"stream fallback: not messaging the Claude Code session: {fallback}")
    status.line(f"dir: {changelog.dir}")
    if ref.kind in ("mr", "pipeline"):
        status.line(f"state: {changelog.dir / 'state.json'}")
    status.line(f"changelog: {changelog.events_path}")
    status.line(f"events: {','.join(subscribed) or '(none)'} (always: {','.join(LIFECYCLE_KINDS)})")
    status.line(f"ignored: {','.join(ignored) or '(none)'}")
    if logs is not None:
        status.line(f"logs: {logs}")
    if until or timeout:
        status.line(
            f"ends: {'on ' + until if until else ''}{' or ' if until and timeout else ''}"
            f"{'after ' + timeout if timeout else ''}"
        )
    status.line(f"daemon: {backend.describe()}")
    groups = ", ".join(f"{name}" for name in GROUPS if name != "lifecycle" and set(GROUPS[name]) & set(tracked))
    options = "--events=<kinds|groups> --exclude-events=<kinds|groups>"
    if logs is not None:
        options += " --logs=failed|all|none"
    status.line(f"restart with: glab-watch {ref.kind} {restart_ref(ref)} {options} (groups: {groups})")


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
) -> None:
    if events_stream not in STREAM_MODES:
        _fail(f"--events-stream must be one of {', '.join(STREAM_MODES)}")
    if message_priority not in MESSAGE_PRIORITIES:
        _fail(f"--message-priority must be one of {', '.join(MESSAGE_PRIORITIES)}")
    if logs is not None and logs not in LOGS_MODES:
        _fail(f"--logs must be one of {', '.join(LOGS_MODES)}")
    try:
        selected = resolve_filter(kind, events, exclude_events)
    except FilterError as exc:
        _fail(str(exc))
    if until is not None and until not in KINDS_BY_RESOURCE[kind]:
        _fail(f"--until: a {kind} watch never produces {until!r}")
    if until is not None and until not in selected:
        _fail(f"--until={until} is filtered out by --events/--exclude-events")
    try:
        timeout_s = parse_duration(timeout) if timeout else None
    except ValueError as exc:
        _fail(str(exc))
    try:
        ref = resolve()
    except (RefError, GlabNotFoundError) as exc:
        _fail(str(exc))

    status = StatusOutput()
    try:
        stream = open_stream(events_stream, ref, message_priority, base_dir, status)
    except StreamError as exc:
        _fail(str(exc))
    changelog = stream.changelog
    logs_mode: LogsMode = logs if logs in LOGS_MODES else "failed"
    try:
        backend = default_backend(no_daemon)
    except (DaemonUnavailable, TimeoutError) as exc:
        _fail(f"cannot start the daemon: {exc}; retry with --no-daemon to poll without it", 1)
    print_header(
        status,
        ref,
        stream.sink,
        changelog,
        selected,
        logs if logs is not None else None,
        backend,
        until,
        timeout,
        stream.fallback,
    )
    writer = ResourceWriter(changelog.base, ref, backend, logs_mode, status)
    watcher = Watcher(
        WatchSettings(ref, selected, logs_mode, until, timeout_s),
        backend,
        stream.sink,
        changelog,
        status,
        writer=writer,
    )
    try:
        code = watcher.run()
    finally:
        backend.close()
    raise typer.Exit(code)


@app.command()
def mr(
    ref: Annotated[str, typer.Argument(help="group/project!123, !123, 123 (repo from the cwd), or the MR URL.")],
    events: EventsOpt = None,
    exclude_events: ExcludeOpt = None,
    logs: LogsOpt = "failed",
    until: UntilOpt = None,
    timeout: TimeoutOpt = None,
    events_stream: StreamOpt = "auto",
    base_dir: BaseDirOpt = None,
    message_priority: PriorityOpt = "next",
    no_daemon: NoDaemonOpt = False,
) -> None:
    """Watch one merge request: state, pushes, approvals, threads, and its head pipeline. Ends when merged or closed."""
    run_watch(
        lambda: parse_id_ref("mr", ref),
        "mr",
        events=events,
        exclude_events=exclude_events,
        events_stream=events_stream,
        base_dir=base_dir,
        until=until,
        timeout=timeout,
        no_daemon=no_daemon,
        message_priority=message_priority,
        logs=logs,
    )


@app.command()
def pipeline(
    ref: Annotated[str, typer.Argument(help="group/project!4567, !4567, 4567 (repo from the cwd), or the URL.")],
    events: EventsOpt = None,
    exclude_events: ExcludeOpt = None,
    logs: LogsOpt = "failed",
    until: UntilOpt = None,
    timeout: TimeoutOpt = None,
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
        no_daemon=no_daemon,
        message_priority=message_priority,
        logs=logs,
    )


@app.command()
def mrs(
    repo: Annotated[str | None, typer.Argument(help="group/project or a project URL; default: the cwd's repo.")] = None,
    events: EventsOpt = None,
    exclude_events: ExcludeOpt = None,
    until: UntilOpt = None,
    timeout: TimeoutOpt = None,
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
        no_daemon=no_daemon,
        message_priority=message_priority,
        logs=None,
    )


@daemon_app.command("status")
def daemon_status(
    as_json: Annotated[bool, typer.Option("--json", help="Print the raw status object.")] = False,
) -> None:
    """Show the daemon's subscriptions, resources, budgets and request counts."""
    files = DaemonFiles.default()
    try:
        response = request(files, {"op": "status"}, timeout=5)
    except DaemonUnavailable:
        running = daemon_running(files)
        typer.echo("daemon: not running" if not running else "daemon: running but not answering; see daemon logs")
        raise typer.Exit(0 if not running else 1) from None
    if as_json:
        typer.echo(json.dumps(response, indent=2))
        return
    _print_status(response, files)


def _print_status(status: dict[str, Any], files: DaemonFiles) -> None:
    typer.echo(
        f"daemon: pid {status['pid']}, version {status['version']}, protocol {status['protocol']}, "
        f"up {status['uptime_s']} s, endpoint {files.describe_endpoint()}"
    )
    for sub in status.get("subscriptions", []):
        typer.echo(
            f"subscription {sub['id'][:8]} client {sub['client_id'][:8]} {sub['resource']} "
            f"(lease idle {sub['idle_s']} s, logs {sub['logs']})"
        )
    for res in status.get("resources", []):
        state = "ended" if res["ended"] else (f"degraded: {res['degraded']}" if res["degraded"] else "active")
        typer.echo(
            f"resource {res['resource']}: {state}, {res['polls']} polls, seq {res['seq']}, "
            f"next poll in {res['next_poll_in_s']} s"
        )
    for host, info in status.get("hosts", {}).items():
        requests, budget = info["requests"], info["budget"]
        typer.echo(
            f"host {host}: {requests['requests']} requests ({requests['not_modified']} not modified), "
            f"budget {budget['rate_per_minute']}/min, remaining {budget['remaining']}"
        )


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the daemon. Running watchers start a new one on their next poll."""
    files = DaemonFiles.default()
    try:
        stopped = stop_daemon(files)
    except DaemonUnavailable as exc:
        _fail(str(exc), 1)
    typer.echo("daemon: stopped" if stopped else "daemon: not running")


@daemon_app.command("logs")
def daemon_logs(
    lines: Annotated[int, typer.Option("--lines", "-n", help="How many lines from the end.")] = 50,
) -> None:
    """Print the end of the daemon's log file."""
    files = DaemonFiles.default()
    try:
        content = files.log.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        typer.echo(f"no daemon log at {files.log}")
        return
    typer.echo(f"==> {files.log}")
    for line in content[-lines:]:
        typer.echo(line)


def main() -> None:
    try:
        app()
    except (BackendError, DaemonUnavailable) as exc:
        StatusOutput(sys.stderr).line(f"error: {exc}")
        sys.exit(1)
