"""What to fetch beyond the jobs, and the problem-driven summary. Ported from glab-pipeline, behavior unchanged."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from glab_cli_extras.pipeline.models import Bridge, Job, Pipeline

YAML_HINT_REASONS = frozenset(
    {
        "unmet_prerequisites",
        "missing_dependency_failure",
        "config_error",
        "pipeline_loop_detected",
        "downstream_pipeline_creation_failed",
    }
)

TEST_STAGE_RE = re.compile(r"test|spec|qa", re.IGNORECASE)
TEST_NAME_RE = re.compile(r"\b(test|spec|rspec|pytest|jest|vitest|phpunit)\b", re.IGNORECASE)

# Bridge statuses that are NOT considered failures.
_BRIDGE_OK_STATUSES = frozenset({"success", "manual", "skipped"})


@dataclass(frozen=True)
class ExtrasPlan:
    """Decisions about which conditional fetches to perform."""

    need_lint: bool
    need_test_report: bool
    need_downstream: bool
    yaml_hint_jobs: tuple[int, ...]
    failed_test_jobs: tuple[int, ...]
    failed_bridges: tuple[int, ...]
    # IDs of bridges to fetch downstream details for. Normally equals
    # failed_bridges; under force_downstream this expands to every bridge
    # with a non-null downstream_pipeline.
    downstream_bridges: tuple[int, ...]
    need_artefacts: bool
    # IDs of jobs whose live (non-expired) archive artifact should be fetched.
    artefact_jobs: tuple[int, ...]


def is_test_job(job: Job) -> bool:
    """Heuristic: does this job look like a test job?"""
    return bool(TEST_STAGE_RE.search(job.stage) or TEST_NAME_RE.search(job.name))


def _parse_iso8601(value: str) -> dt.datetime:
    """Parse an ISO8601 timestamp, tolerating a trailing `Z` (UTC)."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def artifact_is_live(job: Job, now: dt.datetime) -> bool:
    """Whether a job has an archive artifact that hasn't expired as of `now`.

    A null `artifacts_expire_at` means the artifact never expires. A timestamp
    that can't be parsed is treated as live (better to attempt the download and
    let a 404 skip it than to silently drop a possibly-present archive).
    """
    if not job.has_archive_artifact:
        return False
    if job.artifacts_expire_at is None:
        return True
    try:
        expire_at = _parse_iso8601(job.artifacts_expire_at)
    except ValueError:
        return True
    return expire_at >= now


def decide_extras(
    pipeline: Pipeline,
    jobs: list[Job],
    bridges: list[Bridge],
    *,
    force_lint: bool = False,
    force_test_report: bool = False,
    force_downstream: bool = False,
    force_artefacts: bool = False,
    now: dt.datetime | None = None,
) -> ExtrasPlan:
    """Decide which conditional fetches to perform based on pipeline state."""
    yaml_hint_jobs = tuple(j.id for j in jobs if j.failure_reason in YAML_HINT_REASONS)
    failed_test_jobs = tuple(j.id for j in jobs if j.status == "failed" and is_test_job(j))
    failed_bridges = tuple(b.id for b in bridges if b.status not in _BRIDGE_OK_STATUSES)

    need_lint = bool(force_lint or pipeline.yaml_errors or len(jobs) == 0 or yaml_hint_jobs)
    need_test_report = bool(force_test_report or failed_test_jobs)
    need_downstream = bool(force_downstream or failed_bridges)

    downstream_bridges = tuple(b.id for b in bridges if b.downstream_pipeline) if force_downstream else failed_bridges

    selection_now = now or dt.datetime.now(dt.UTC)
    artefact_jobs = tuple(j.id for j in jobs if artifact_is_live(j, selection_now)) if force_artefacts else ()

    return ExtrasPlan(
        need_lint=need_lint,
        need_test_report=need_test_report,
        need_downstream=need_downstream,
        yaml_hint_jobs=yaml_hint_jobs,
        failed_test_jobs=failed_test_jobs,
        failed_bridges=failed_bridges,
        downstream_bridges=downstream_bridges,
        need_artefacts=force_artefacts,
        artefact_jobs=artefact_jobs,
    )


# ---------------------------------------------------------------------------
# Summary formatting (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummaryInputs:
    pipeline: Pipeline
    jobs: list[Job]
    bridges: list[Bridge]
    plan: ExtrasPlan
    output_dir: Path
    job_log_paths: dict[int, Path]
    downstream_paths: dict[int, Path]
    has_lint_file: bool
    has_test_report_file: bool
    test_report: dict[str, Any] | None
    log_truncated: dict[int, bool] = field(default_factory=dict)
    logs_mode: str = "all"
    downstream_dirs: dict[int, Path] = field(default_factory=dict)
    artefact_results: list[dict[str, Any]] = field(default_factory=list)


def _format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    total = int(seconds)
    m, s = divmod(total, 60)
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def build_summary_dict(s: SummaryInputs) -> dict[str, Any]:
    """Build a canonical JSON-serializable summary dict from SummaryInputs.

    This is the single source of truth for the summary; format_summary renders
    a human-readable string from this dict.
    """
    p = s.pipeline
    output_dir = s.output_dir.resolve()

    # Files section: omit keys for files that weren't written.
    files: dict[str, Any] = {
        "pipeline": str(output_dir / "pipeline.json"),
        "jobs": str(output_dir / "jobs.json"),
        "jobs_count": len(s.jobs),
        "logs_mode": s.logs_mode,
    }
    if s.bridges:
        files["bridges"] = str(output_dir / "bridges.json")
        files["bridges_count"] = len(s.bridges)
    if s.has_lint_file:
        files["lint"] = str(output_dir / "lint.json")
        merged_yml = output_dir / "merged.yml"
        if merged_yml.exists():
            files["merged_yaml"] = str(merged_yml)
    if s.has_test_report_file:
        files["test_report"] = str(output_dir / "test-report.json")

    # Failed jobs
    hint_set = set(s.plan.yaml_hint_jobs)
    failed_jobs_list: list[dict[str, Any]] = []
    for j in s.jobs:
        if j.status != "failed":
            continue
        log_path = s.job_log_paths.get(j.id)
        failed_jobs_list.append(
            {
                "id": j.id,
                "name": j.name,
                "stage": j.stage,
                "failure_reason": j.failure_reason,
                "log": str(log_path.resolve()) if log_path is not None else None,
                "log_truncated": s.log_truncated.get(j.id, False),
                "yaml_hint": j.id in hint_set,
            }
        )

    # Failed downstream
    failed_bridge_ids = set(s.plan.failed_bridges)
    failed_downstream_list: list[dict[str, Any]] = []
    for b in s.bridges:
        if b.id not in failed_bridge_ids:
            continue
        dp = b.downstream_pipeline or {}
        detail = s.downstream_paths.get(b.id)
        failed_downstream_list.append(
            {
                "bridge_id": b.id,
                "bridge_name": b.name,
                "downstream_pipeline_id": dp.get("id"),
                "downstream_status": dp.get("status"),
                "detail": str(detail.resolve()) if detail is not None else None,
                "dir": str(s.downstream_dirs[b.id]) if b.id in s.downstream_dirs else None,
            }
        )

    # Test failures
    test_failures: dict[str, Any] | None = None
    if s.has_test_report_file:
        tr = s.test_report or {}
        total = tr.get("total") if isinstance(tr, dict) else None
        if isinstance(total, dict) and "failed" in total and "count" in total:
            test_failures = {
                "failed": total["failed"],
                "total": total["count"],
            }

    result: dict[str, Any] = {
        "pipeline": {
            "id": p.id,
            "status": p.status,
            "url": p.web_url,
            "ref": p.ref,
            "sha": p.sha,
            "sha_short": (p.sha or "")[:7],
            "source": p.source,
            "created_at": p.created_at,
            "duration_seconds": p.duration,
            "duration_human": _format_duration(p.duration),
        },
        "output_dir": str(output_dir),
        "files": files,
        "yaml_errors": p.yaml_errors,
        "failed_jobs": failed_jobs_list,
        "failed_downstream": failed_downstream_list,
        "test_failures": test_failures,
    }

    if s.plan.need_artefacts:
        downloaded = sum(1 for r in s.artefact_results if r["status"] in ("ok", "empty"))
        result["artifacts"] = {
            "dir": "artifacts",
            "jobs": s.artefact_results,
            "downloaded": downloaded,
            "skipped": len(s.artefact_results) - downloaded,
        }

    return result


def _logs_note(files: dict[str, Any]) -> str:
    mode = files.get("logs_mode", "all")
    if mode == "failed":
        return "failed jobs' logs in job-logs/, the others are placeholders; --logs=all downloads them"
    if mode == "none":
        return "no logs downloaded, placeholders in job-logs/; --logs=all downloads them"
    return "all logs in job-logs/"


def format_summary(summary: dict[str, Any]) -> str:
    """Render the canonical summary dict as human-readable text."""
    p = summary["pipeline"]
    files = summary["files"]
    output_dir = summary["output_dir"]
    lines: list[str] = []

    lines.append(f"PIPELINE {p['id']} — status: {p['status']}")
    lines.append(f"URL:     {p['url']}")
    short_sha = p["sha_short"]
    source = p["source"] or "unknown"
    lines.append(f"Ref:     {p['ref']}   SHA: {short_sha}   Source: {source}")
    lines.append(f"Created: {p['created_at']}   Duration: {p['duration_human']}")
    lines.append("")
    lines.append("Files Created:")
    lines.append(f"  Pipeline:   {files['pipeline']}")
    lines.append(f"  Jobs:       {files['jobs']}   ({files['jobs_count']} jobs, {_logs_note(files)})")
    if "bridges" in files:
        lines.append(f"  Bridges:    {files['bridges']}   ({files['bridges_count']} bridges)")
    lines.append("")
    lines.append(f"All files in: {output_dir}/")

    # YAML errors / lint section
    yaml_errors = summary["yaml_errors"]
    if yaml_errors:
        lines.append("")
        lines.append("YAML Errors:")
        for ln in yaml_errors.splitlines() or [yaml_errors]:
            lines.append(f"  {ln}")
        lines.append("  → lint.json, merged.yml")
    elif "lint" in files:
        lines.append("")
        lines.append("Lint result: see lint.json, merged.yml")

    # Failed jobs section
    failed_jobs = summary["failed_jobs"]
    if failed_jobs:
        lines.append("")
        lines.append(f"Failed Jobs ({len(failed_jobs)}):")
        for j in failed_jobs:
            reason = j["failure_reason"] or "—"
            lines.append(f"  {j['name']}  (stage={j['stage']}, reason={reason})")
            if j["log"] is not None:
                lines.append(f"    Log: {j['log']}")
            if j["yaml_hint"]:
                lines.append("    → likely YAML/needs issue, see lint.json")

    # Failed downstream pipelines
    failed_downstream = summary["failed_downstream"]
    if failed_downstream:
        lines.append("")
        lines.append(f"Failed Downstream Pipelines ({len(failed_downstream)}):")
        for d in failed_downstream:
            dpid = d["downstream_pipeline_id"] if d["downstream_pipeline_id"] is not None else "—"
            dpstatus = d["downstream_status"] if d["downstream_status"] is not None else "—"
            lines.append(f"  {d['bridge_name']} → pipeline {dpid} ({dpstatus})")
            if d["detail"] is not None:
                lines.append(f"    Detail: {d['detail']}")

    # Test failures
    if "test_report" in files:
        tf = summary["test_failures"]
        if tf is not None:
            lines.append("")
            lines.append(f"Test Failures ({tf['failed']} failed / {tf['total']} total):")
            lines.append("  See test-report.json")
        else:
            lines.append("")
            lines.append("Test failures present — see test-report.json")

    # Artifacts section (only when --with-artefacts was requested)
    artifacts = summary.get("artifacts")
    if artifacts is not None:
        lines.append("")
        lines.append(f"Artifacts (downloaded {artifacts['downloaded']} jobs, skipped {artifacts['skipped']}):")
        for r in artifacts["jobs"]:
            head = f"  {r['stage']}/{r['name']} (#{r['job_id']}):"
            if r["status"] in ("ok", "empty"):
                lines.append(f"{head} {r['file_count']} files, {_format_size(r['total_bytes'])} -> {r['dir']}/")
            else:
                reason = r["reason"] or r["status"]
                lines.append(f"{head} skipped ({r['status']}: {reason})")

    return "\n".join(lines) + "\n"


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GiB"
