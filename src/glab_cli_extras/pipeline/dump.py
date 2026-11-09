"""Writing a pipeline dump into its dir: the REST files, the extras and the summary.

Both writers of a pipeline dir call these functions, so the files are the same whoever wrote them: glab-pipeline
inspect, which fetches in-process, and a glab-watch client, which gets the same data from the daemon when the watched
pipeline finishes.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from glab_cli_extras.events import utc_now_iso
from glab_cli_extras.paths import base_dir
from glab_cli_extras.pipeline import extras
from glab_cli_extras.pipeline.models import Bridge, Job, Pipeline, parse_bridges, parse_jobs, parse_pipeline
from glab_cli_extras.pipeline.summary import (
    ExtrasPlan,
    SummaryInputs,
    build_summary_dict,
    decide_extras,
    format_summary,
)
from glab_cli_extras.store.atomic import atomic_write_json, atomic_write_text
from glab_cli_extras.store.job_logs import log_entry, write_placeholder
from glab_cli_extras.store.layout import pipeline_dir
from glab_cli_extras.store.state import put_snapshot

LOGGED_STATUSES = frozenset({"success", "failed", "canceled", "running"})
"""A job in one of these has a log to download; the others never ran."""


@dataclass
class ExtrasResult:
    has_lint: bool = False
    has_report: bool = False
    report: dict[str, Any] | None = None
    downstream: dict[int, Path] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=lambda: {"downstream": {}})
    """What each extra was fetched for, kept in `state.json` to decide whether a rerun can reuse it."""


def wants_log(mode: str, status: str) -> bool:
    if mode == "all":
        return status in LOGGED_STATUSES
    if mode == "failed":
        return status == "failed"
    return False


def write_rest(directory: Path, pipeline_raw: Any, jobs_raw: list[Any], bridges_raw: list[Any]) -> None:
    atomic_write_json(directory / "pipeline.json", pipeline_raw)
    atomic_write_json(directory / "jobs.json", jobs_raw)
    if bridges_raw:
        atomic_write_json(directory / "bridges.json", bridges_raw)


def write_lint(directory: Path, result: Any, key: dict[str, Any], out: ExtrasResult) -> None:
    atomic_write_json(directory / "lint.json", result)
    out.has_lint = True
    out.record["lint"] = key
    merged_yaml = result.get("merged_yaml") if isinstance(result, dict) else None
    if merged_yaml:
        atomic_write_text(directory / "merged.yml", merged_yaml)


def write_test_report(directory: Path, result: Any, pipeline_status: str, out: ExtrasResult) -> None:
    atomic_write_json(directory / "test-report.json", result)
    out.has_report = True
    out.report = result if isinstance(result, dict) else None
    out.record["test_report"] = {"pipeline_status": pipeline_status}


def write_downstream(directory: Path, bridge: Bridge, dpid: int, data: Any, out: ExtrasResult) -> None:
    path = directory / "downstream" / extras.downstream_filename(bridge, dpid)
    atomic_write_json(path, data)
    out.downstream[bridge.id] = path
    out.record["downstream"][str(bridge.id)] = bridge.status


def placeholders(
    directory: Path, state: dict[str, Any], jobs: list[Job], logs_mode: str, command: str
) -> tuple[dict[int, Path], dict[int, bool]]:
    """Every job without a wanted log gets a placeholder; return each job's file and whether its log is a tail."""
    paths: dict[int, Path] = {}
    truncated: dict[int, bool] = {}
    for job in jobs:
        entry = log_entry(state, job.id)
        if entry is None or entry.get("log_state") == "placeholder":
            write_placeholder(
                directory,
                state,
                job_id=job.id,
                stage=job.stage,
                name=job.name,
                status=job.status,
                logs=logs_mode,
                command=command,
                coming=wants_log(logs_mode, job.status) and job.status != "running",
            )
            entry = log_entry(state, job.id)
        if entry is not None:
            paths[job.id] = directory / str(entry.get("file", ""))
            truncated[job.id] = bool(entry.get("truncated"))
    return paths, truncated


def downstream_dirs(host: str, project: str, bridges: list[Bridge], found: dict[int, Path]) -> dict[int, Path]:
    """The dir each failed downstream pipeline has, where `inspect --pipeline-id <id>` would write it."""
    dirs = {}
    for bridge in bridges:
        dp = bridge.downstream_pipeline or {}
        if bridge.id in found and dp.get("id"):
            downstream_project = _project_from_url(dp.get("web_url")) or project
            dirs[bridge.id] = pipeline_dir(base_dir(), host, downstream_project, int(dp["id"]))
    return dirs


def _project_from_url(url: Any) -> str | None:
    if not isinstance(url, str) or "/-/pipelines/" not in url:
        return None
    path = url.split("://", 1)[-1].split("/", 1)[-1]
    return path.split("/-/pipelines/")[0] or None


def write_summary(
    directory: Path,
    *,
    host: str,
    project: str,
    pipeline: Pipeline,
    jobs: list[Job],
    bridges: list[Bridge],
    plan: ExtrasPlan,
    extras_result: ExtrasResult,
    log_paths: dict[int, Path],
    truncated: dict[int, bool],
    logs_mode: str,
    artefacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = build_summary_dict(
        SummaryInputs(
            pipeline=pipeline,
            jobs=jobs,
            bridges=bridges,
            plan=plan,
            output_dir=directory,
            job_log_paths=log_paths,
            downstream_paths=extras_result.downstream,
            has_lint_file=extras_result.has_lint,
            has_test_report_file=extras_result.has_report,
            test_report=extras_result.report,
            artefact_results=artefacts or [],
            log_truncated=truncated,
            logs_mode=logs_mode,
            downstream_dirs=downstream_dirs(host, project, bridges, extras_result.downstream),
        )
    )
    summary["state"] = str(directory.resolve() / "state.json")
    atomic_write_text(directory / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    atomic_write_text(directory / "summary.txt", format_summary(summary))
    return summary


def record_dump(
    state: dict[str, Any], snapshot: Any, snapshot_hash: str, etag: str | None, record: dict[str, Any]
) -> None:
    now = utc_now_iso()
    put_snapshot(state, snapshot, now, etag=etag)
    state["dump"] = {"written_at": now, "etag": etag, "snapshot_hash": snapshot_hash, "extras": record}


def write_dump(
    directory: Path, state: dict[str, Any], ref: Any, dump: dict[str, Any], logs_mode: str, command: str
) -> dict[str, Any]:
    """Write a dump fetched by the daemon (`sync.dump.fetch_dump`): the same files inspect writes with its defaults.
    The caller holds the dir's lock and saves the state."""
    pipeline_raw, jobs_raw, bridges_raw = dump["pipeline"], list(dump["jobs"]), list(dump.get("bridges") or [])
    write_rest(directory, pipeline_raw, jobs_raw, bridges_raw)
    pipeline = parse_pipeline(pipeline_raw)
    jobs = parse_jobs(jobs_raw)
    bridges = parse_bridges(bridges_raw)
    plan = decide_extras(pipeline, jobs, bridges, now=dt.datetime.now(dt.UTC))
    out = ExtrasResult()
    if dump.get("lint") is not None:
        write_lint(directory, dump["lint"], {"sha": pipeline.sha, "two_step": False}, out)
    if dump.get("test_report") is not None:
        write_test_report(directory, dump["test_report"], pipeline.status, out)
    by_id = {b.id: b for b in bridges}
    for item in dump.get("downstream") or []:
        bridge = by_id.get(int(item["bridge_id"]))
        if bridge is not None:
            write_downstream(directory, bridge, int(item["pipeline_id"]), item["data"], out)
    log_paths, truncated = placeholders(directory, state, jobs, logs_mode, command)
    summary = write_summary(
        directory,
        host=ref.host,
        project=ref.project,
        pipeline=pipeline,
        jobs=jobs,
        bridges=bridges,
        plan=plan,
        extras_result=out,
        log_paths=log_paths,
        truncated=truncated,
        logs_mode=logs_mode,
    )
    snapshot = dump.get("snapshot")
    if snapshot is not None:
        record_dump(state, snapshot, str(dump.get("snapshot_hash")), dump.get("etag"), out.record)
    return summary
