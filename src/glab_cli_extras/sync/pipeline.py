"""The pipeline tree: its snapshot model and the GraphQL fetch through GitLab's ETag cache.

The query sends `X-GITLAB-GRAPHQL-RESOURCE-ETAG: pipelines/id/<id>` and `If-None-Match`. GitLab answers an unchanged
pipeline with a 304 from its cache before running the query, and touches the key on every job transition of the
pipeline and its downstream pipelines. So one request is both the probe and the fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from glab_cli_extras.gitlab import queries
from glab_cli_extras.gitlab.client import GitLabClient, NotFound
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.gitlab.values import gid_to_int, lower

FINAL_JOB_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})
FINAL_PIPELINE_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})
QUEUED_PIPELINE_STATUSES = frozenset({"created", "pending", "waiting_for_resource", "preparing", "scheduled"})


@dataclass(frozen=True, slots=True)
class JobState:
    id: int
    name: str
    stage: str | None
    status: str
    allow_failure: bool
    pipeline_id: int
    project: str
    started_at: str | None = None
    finished_at: str | None = None
    trace_size: int | None = None


@dataclass(frozen=True, slots=True)
class DownstreamState:
    id: int
    iid: int | None
    status: str
    project: str
    web_url: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineSnapshot:
    id: int
    iid: int | None
    project: str
    status: str
    ref: str | None
    sha: str | None
    web_url: str | None
    jobs: tuple[JobState, ...] = ()
    downstream: tuple[DownstreamState, ...] = ()


def _web_url(host: str, path: Any) -> str | None:
    return f"https://{host}{path}" if path else None


def _trace_size(job: dict[str, Any]) -> int | None:
    for artifact in (job.get("artifacts") or {}).get("nodes") or []:
        if lower(artifact.get("fileType")) == "trace" and isinstance(artifact.get("size"), int):
            return int(artifact["size"])
    return None


def _parse_jobs(nodes: list[Any], pipeline_id: int, project: str) -> list[JobState]:
    jobs = []
    for node in nodes:
        jobs.append(
            JobState(
                id=gid_to_int(node["id"]),
                name=str(node.get("name") or ""),
                stage=(node.get("stage") or {}).get("name"),
                status=lower(node.get("status")),
                allow_failure=bool(node.get("allowFailure")),
                pipeline_id=pipeline_id,
                project=project,
                started_at=node.get("startedAt"),
                finished_at=node.get("finishedAt"),
                trace_size=_trace_size(node),
            )
        )
    return jobs


def parse_pipeline(data: Any, host: str, project: str) -> PipelineSnapshot:
    """Build a snapshot from the `PipelineTree` query result. Raises NotFound when the pipeline is missing."""
    node = ((data or {}).get("project") or {}).get("pipeline")
    if not node:
        raise NotFound(f"pipeline not found in {project}", 404)
    pipeline_id = gid_to_int(node["id"])
    jobs = _parse_jobs((node.get("jobs") or {}).get("nodes") or [], pipeline_id, project)
    downstream = []
    for child in (node.get("downstream") or {}).get("nodes") or []:
        child_id = gid_to_int(child["id"])
        child_project = str((child.get("project") or {}).get("fullPath") or project)
        downstream.append(
            DownstreamState(
                id=child_id,
                iid=int(child["iid"]) if child.get("iid") else None,
                status=lower(child.get("status")),
                project=child_project,
                web_url=_web_url(host, child.get("path")),
            )
        )
        jobs.extend(_parse_jobs((child.get("jobs") or {}).get("nodes") or [], child_id, child_project))
    return PipelineSnapshot(
        id=pipeline_id,
        iid=int(node["iid"]) if node.get("iid") else None,
        project=project,
        status=lower(node.get("status")),
        ref=node.get("ref"),
        sha=node.get("sha"),
        web_url=_web_url(host, node.get("path")),
        jobs=tuple(sorted(jobs, key=lambda j: j.id)),
        downstream=tuple(sorted(downstream, key=lambda d: d.id)),
    )


@dataclass(frozen=True, slots=True)
class TreeResult:
    not_modified: bool
    snapshot: PipelineSnapshot | None
    etag: str | None


async def fetch_tree(
    client: GitLabClient, ref: ResourceRef, if_none_match: str | None, *, use_etag: bool = True
) -> TreeResult:
    """Fetch the pipeline tree. With `use_etag`, an unchanged pipeline answers 304 and the result has no snapshot."""
    assert ref.id is not None
    result = await client.graphql(
        queries.load("pipeline"),
        {"project": ref.project, "id": f"gid://gitlab/Ci::Pipeline/{ref.id}"},
        etag_resource=f"pipelines/id/{ref.id}" if use_etag else None,
        if_none_match=if_none_match,
        kind="pipeline",
    )
    if result.not_modified:
        return TreeResult(True, None, result.etag)
    return TreeResult(False, parse_pipeline(result.data, ref.host, ref.project), result.etag)
