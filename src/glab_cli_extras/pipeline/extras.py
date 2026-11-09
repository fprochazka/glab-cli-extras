"""The conditional fetches of glab-pipeline inspect: lint, test report, downstream pipelines, artefacts.

Ported from glab-pipeline with its behavior. They take the same client as everything else, so the daemon fetches them
within its budget for a finished watched pipeline, and inspect fetches them in-process. The artefact archives stream
straight to disk through `sync_api` and are fetched by inspect only.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from glab_cli_extras.gitlab.client import GitLabClient, encode_project
from glab_cli_extras.gitlab.sync_api import GlabApiError, glab_api_download
from glab_cli_extras.pipeline.models import Bridge, Job
from glab_cli_extras.store.layout import filename_part


async def fetch_lint_cheap(client: GitLabClient, project: str, sha: str, ref: str | None) -> Any:
    # dry_run_ref sets the branch/tag context used when simulating rules. It is skipped when the ref is not a plain
    # branch name (`refs/merge-requests/N/head` for MR pipelines): lint expects a branch or tag, and falling back to
    # the project default is better than passing something it rejects.
    query = {"content_ref": sha, "dry_run": "true", "include_jobs": "true"}
    if ref and not ref.startswith("refs/"):
        query["dry_run_ref"] = ref
    return await client.get_json(f"projects/{encode_project(project)}/ci/lint", query, kind="lint")


async def fetch_lint_two_step(client: GitLabClient, project: str, sha: str, ref: str | None, branch: str | None) -> Any:
    """Two-step lint: fetch the raw CI file from the source branch and POST it to `ci/lint` with that ref.

    This resolves `include:` against the source branch, so masked CI variables in include paths work. Falls back to
    the cheap GET on any failure. The POST is a read on GitLab's side, but like every POST it is not retried.
    """
    target = branch or (ref if ref and not ref.startswith("refs/") else None)
    if not target:
        print(
            "warning: --with-merged-ci-config: no source branch resolvable, falling back to cheap GET /ci/lint",
            file=sys.stderr,
        )
        return await fetch_lint_cheap(client, project, sha, ref)
    encoded = encode_project(project)
    try:
        project_data = await client.get_json(f"projects/{encoded}", kind="project")
        ci_config_path = (project_data or {}).get("ci_config_path") or ".gitlab-ci.yml"
        raw = await client.get(
            f"projects/{encoded}/repository/files/{quote(ci_config_path, safe='')}/raw?ref={quote(target, safe='')}",
            kind="file",
        )
        return await client.request(
            "POST",
            f"projects/{encoded}/ci/lint",
            json_body={"content": raw.text(), "dry_run": True, "ref": target, "include_jobs": True},
            kind="lint",
        )
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the cheap lint, as glab-pipeline did
        print(
            f"warning: --with-merged-ci-config two-step lint failed ({exc}); falling back to cheap GET /ci/lint",
            file=sys.stderr,
        )
        return await fetch_lint_cheap(client, project, sha, ref)


async def fetch_test_report(client: GitLabClient, project: str, pipeline_id: int) -> Any:
    path = f"projects/{encode_project(project)}/pipelines/{pipeline_id}/test_report_summary"
    return await client.get_json(path, kind="test_report")


async def fetch_downstream(client: GitLabClient, project: str, bridge: Bridge) -> tuple[Bridge, int, Any] | None:
    dp = bridge.downstream_pipeline or {}
    dpid = dp.get("id")
    if not dpid:
        return None
    target = dp.get("project_id") or encode_project(project)
    return bridge, int(dpid), await client.get_json(f"projects/{target}/pipelines/{dpid}", kind="downstream")


def downstream_filename(bridge: Bridge, dpid: int) -> str:
    return f"{filename_part(bridge.name)}-{dpid}.json"


def artifact_dir_name(job: Job) -> str:
    return f"{filename_part(job.stage)}-{filename_part(job.name)}-{job.id}"


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> tuple[int, int]:
    """Extract `zip_path` into `dest_dir`, rejecting members that escape it.

    Returns (file_count, total_uncompressed_bytes). Raises zipfile.BadZipFile on
    a corrupt archive and ValueError on a zip-slip member (caller catches both).
    """
    dest_root = dest_dir.resolve()
    file_count = 0
    total_bytes = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest_dir / member.filename).resolve()
            if target != dest_root and dest_root not in target.parents:
                raise ValueError(f"zip member escapes destination: {member.filename!r}")
            if member.is_dir():
                continue
            zf.extract(member, dest_dir)
            file_count += 1
            total_bytes += member.file_size
    return file_count, total_bytes


def download_one_artefact(job: Job, host: str, project: str, artifacts_dir: Path) -> dict[str, Any]:
    """Download and unpack one job's archive; never raises (a failure becomes the record's status)."""
    rel_dir = f"artifacts/{artifact_dir_name(job)}"
    record: dict[str, Any] = {
        "job_id": job.id,
        "name": job.name,
        "stage": job.stage,
        "dir": rel_dir,
        "file_count": 0,
        "total_bytes": 0,
        "status": "ok",
        "reason": None,
    }
    job_dir = artifacts_dir / artifact_dir_name(job)
    job_dir.mkdir(parents=True, exist_ok=True)
    zip_path = job_dir / "_artifacts.zip"
    try:
        glab_api_download(f"projects/{encode_project(project)}/jobs/{job.id}/artifacts", str(zip_path), hostname=host)
        file_count, total_bytes = _safe_extract_zip(zip_path, job_dir)
        record["file_count"] = file_count
        record["total_bytes"] = total_bytes
        if file_count == 0:
            record["status"] = "empty"
    except GlabApiError as exc:
        # A 404 means the archive expired or was removed since the jobs were listed; not worth surfacing loudly.
        record["status"] = "missing/expired" if exc.http_status == 404 else "error"
        record["reason"] = str(exc)
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        record["status"] = "error"
        record["reason"] = str(exc)
    finally:
        zip_path.unlink(missing_ok=True)
    return record
