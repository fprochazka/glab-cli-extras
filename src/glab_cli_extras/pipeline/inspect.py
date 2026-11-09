"""glab-pipeline inspect: dump a pipeline's state into its dir and build the problem-driven summary.

The dir is the same one `glab-watch` writes for the pipeline, and each run reuses what is there:

- The pipeline tree is fetched through GitLab's GraphQL ETag cache with the ETag stored when the REST files were last
  written. A 304, or a tree that equals the one the files were written from, means `pipeline.json`, `jobs.json` and
  `bridges.json` are current, and no REST request is made.
- A finished job's log already on disk is never fetched again. A job whose log is not wanted, or not there yet, gets a
  placeholder file that says why and how to download it.
- The extras (lint, test report, downstream pipelines) are reused while what they depend on is unchanged.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glab_cli_extras.gitlab.client import ApiError, GitLabClient
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.gitlab.retry import standalone_client
from glab_cli_extras.pipeline import extras
from glab_cli_extras.pipeline.dump import (
    ExtrasResult,
    placeholders,
    record_dump,
    wants_log,
    write_downstream,
    write_lint,
    write_rest,
    write_summary,
    write_test_report,
)
from glab_cli_extras.pipeline.models import Bridge, Job, Pipeline, parse_bridges, parse_jobs, parse_pipeline
from glab_cli_extras.pipeline.summary import ExtrasPlan, decide_extras
from glab_cli_extras.serde import from_jsonable, stable_hash, to_jsonable
from glab_cli_extras.store.job_logs import has_log, write_log
from glab_cli_extras.store.layout import ensure_readme
from glab_cli_extras.store.state import load_state, locked, save_state
from glab_cli_extras.sync.dump import fetch_rest
from glab_cli_extras.sync.job_logs import FINAL_JOB_STATUSES, LogsMode, fetch_log_content
from glab_cli_extras.sync.pipeline import PipelineSnapshot, fetch_tree

MAX_PARALLEL = 10


@dataclass(frozen=True)
class InspectOptions:
    ref: ResourceRef
    directory: Path
    command: str
    """The command that downloads every log, for the placeholders."""
    logs: LogsMode = "failed"
    full: bool = False
    branch: str | None = None
    with_merged_ci_config: bool = False
    with_test_report: bool = False
    with_downstream_pipelines: bool = False
    with_artefacts: bool = False


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _snapshot(
    client: GitLabClient, opts: InspectOptions, state: dict[str, Any]
) -> tuple[PipelineSnapshot, str | None, bool]:
    """The pipeline tree, and whether it is unchanged since the REST files were written."""
    stored = state.get("dump")
    dump: dict[str, Any] = stored if isinstance(stored, dict) else {}
    etag = None if opts.full else dump.get("etag")
    tree = await fetch_tree(client, opts.ref, etag)
    if tree.not_modified:
        try:
            return from_jsonable(PipelineSnapshot, state.get("snapshot")), tree.etag, True
        except ValueError:
            tree = await fetch_tree(client, opts.ref, None)
    assert tree.snapshot is not None
    unchanged = not opts.full and dump.get("snapshot_hash") == stable_hash(tree.snapshot)
    return tree.snapshot, tree.etag, unchanged


async def _run_extras(
    client: GitLabClient,
    opts: InspectOptions,
    state: dict[str, Any],
    plan: ExtrasPlan,
    pipeline: Pipeline,
    bridges: list[Bridge],
) -> ExtrasResult:
    """The conditional fetches, in parallel, each reused while what it depends on is unchanged."""
    directory, project = opts.directory, opts.ref.project
    recorded = (state.get("dump") or {}).get("extras") or {}
    out = ExtrasResult()
    lint_key = {"sha": pipeline.sha, "two_step": opts.with_merged_ci_config}
    tasks: dict[str, Awaitable[Any]] = {}
    if plan.need_lint:
        if not opts.full and recorded.get("lint") == lint_key and (directory / "lint.json").is_file():
            out.has_lint, out.record["lint"] = True, lint_key
        elif opts.with_merged_ci_config:
            tasks["lint"] = extras.fetch_lint_two_step(client, project, pipeline.sha, pipeline.ref, opts.branch)
        else:
            tasks["lint"] = extras.fetch_lint_cheap(client, project, pipeline.sha, pipeline.ref)
    if plan.need_test_report:
        stored = _read_json(directory / "test-report.json")
        if not opts.full and recorded.get("test_report") == {"pipeline_status": pipeline.status} and stored is not None:
            out.has_report, out.report = True, stored if isinstance(stored, dict) else None
            out.record["test_report"] = recorded["test_report"]
        else:
            tasks["test_report"] = extras.fetch_test_report(client, project, pipeline.id)
    recorded_downstream = recorded.get("downstream") or {}
    by_id = {b.id: b for b in bridges}
    if plan.need_downstream:
        for bid in plan.downstream_bridges:
            bridge = by_id.get(bid)
            if bridge is None or not bridge.downstream_pipeline:
                continue
            dpid = bridge.downstream_pipeline.get("id")
            path = directory / "downstream" / extras.downstream_filename(bridge, int(dpid)) if dpid else None
            reusable = path is not None and path.is_file() and recorded_downstream.get(str(bid)) == bridge.status
            if reusable and not opts.full and path is not None:
                out.downstream[bid] = path
                out.record["downstream"][str(bid)] = bridge.status
            else:
                tasks[f"downstream:{bid}"] = extras.fetch_downstream(client, project, bridge)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for tag, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException):
            print(f"warning: failed to fetch {tag}: {result}", file=sys.stderr)
            continue
        if result is None:
            continue
        if tag == "lint":
            write_lint(directory, result, lint_key, out)
        elif tag == "test_report":
            write_test_report(directory, result, pipeline.status, out)
        else:
            bridge, dpid, data = result
            write_downstream(directory, bridge, dpid, data, out)
    return out


def _tree_only_jobs(snapshot: PipelineSnapshot, jobs: list[Job], opts: InspectOptions) -> list[Job]:
    """The jobs of this pipeline that the tree shows and GitLab's REST job list leaves out, such as a manual job of a
    later stage or `pages:deploy`. A watch writes their files from the tree, so inspect does too: both leave the same
    files. They are not in `jobs.json` or the summary, which stay GitLab's REST view."""
    rest_ids = {job.id for job in jobs}
    return [
        Job(
            id=j.id,
            name=j.name,
            stage=j.stage or "",
            status=j.status,
            failure_reason=None,
            allow_failure=j.allow_failure,
            web_url="",
            duration=None,
            started_at=j.started_at,
            finished_at=j.finished_at,
            created_at=None,
            has_archive_artifact=False,
            artifacts_expire_at=None,
        )
        for j in snapshot.jobs
        if j.pipeline_id == opts.ref.id and j.id not in rest_ids
    ]


async def _sync_logs(
    client: GitLabClient, opts: InspectOptions, state: dict[str, Any], jobs: list[Job]
) -> tuple[dict[int, Path], dict[int, bool]]:
    """Every job gets a file: its log when wanted, else a placeholder. Finished logs on disk are never fetched again."""
    directory = opts.directory
    to_fetch = []
    for job in jobs:
        if not wants_log(opts.logs, job.status):
            continue
        existing = None if opts.full else has_log(directory, state, job.id, full=True)
        if existing is None or job.status not in FINAL_JOB_STATUSES:
            to_fetch.append(job)

    async def fetch(job: Job) -> None:
        try:
            content = await fetch_log_content(client, opts.ref.project, job.id, None, tail=False)
        except ApiError as exc:
            print(f"warning: failed to fetch trace for job {job.id} ({job.name}): {exc}", file=sys.stderr)
            return
        write_log(directory, state, job_id=job.id, stage=job.stage, name=job.name, status=job.status, content=content)

    await asyncio.gather(*(fetch(job) for job in to_fetch))
    # A job whose fetch failed keeps (or gets) a placeholder, like every job whose log is not wanted.
    return placeholders(directory, state, jobs, opts.logs, opts.command)


async def run_inspect(opts: InspectOptions, client: GitLabClient) -> dict[str, Any]:
    directory = opts.directory
    with locked(directory):
        ensure_readme(directory, "pipeline")
        state = load_state(directory, opts.ref.key)
        snapshot, etag, unchanged = await _snapshot(client, opts, state)
        pipeline_raw = _read_json(directory / "pipeline.json")
        jobs_raw = _read_json(directory / "jobs.json")
        bridges_raw = _read_json(directory / "bridges.json") or []
        if not (unchanged and isinstance(pipeline_raw, dict) and isinstance(jobs_raw, list)):
            pipeline_raw, jobs_raw, bridges_raw = await fetch_rest(client, opts.ref)
            write_rest(directory, pipeline_raw, jobs_raw, bridges_raw)
        pipeline = parse_pipeline(pipeline_raw)
        jobs = parse_jobs(jobs_raw)
        bridges = parse_bridges(bridges_raw)
        plan = decide_extras(
            pipeline,
            jobs,
            bridges,
            force_lint=opts.with_merged_ci_config,
            force_test_report=opts.with_test_report,
            force_downstream=opts.with_downstream_pipelines,
            force_artefacts=opts.with_artefacts,
            now=dt.datetime.now(dt.UTC),
        )
        extras_result = await _run_extras(client, opts, state, plan, pipeline, bridges)
        log_paths, truncated = await _sync_logs(client, opts, state, jobs + _tree_only_jobs(snapshot, jobs, opts))
        artefacts: list[dict[str, Any]] = []
        if plan.need_artefacts:
            artefacts = await asyncio.to_thread(_download_artefacts, opts, jobs, plan.artefact_jobs)
        summary = write_summary(
            directory,
            host=opts.ref.host,
            project=opts.ref.project,
            pipeline=pipeline,
            jobs=jobs,
            bridges=bridges,
            plan=plan,
            extras_result=extras_result,
            log_paths=log_paths,
            truncated=truncated,
            logs_mode=opts.logs,
            artefacts=artefacts,
        )
        record_dump(state, to_jsonable(snapshot), stable_hash(snapshot), etag, extras_result.record)
        save_state(directory, state)
    return summary


def _download_artefacts(opts: InspectOptions, jobs: list[Job], job_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    by_id = {j.id: j for j in jobs}
    selected = [by_id[i] for i in job_ids if i in by_id]
    if not selected:
        return []
    artifacts_dir = opts.directory / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        return list(
            pool.map(
                lambda j: extras.download_one_artefact(j, opts.ref.host, opts.ref.project, artifacts_dir), selected
            )
        )


def make_client(host: str) -> GitLabClient:
    return standalone_client(host, MAX_PARALLEL)
