"""The pipeline tree: its snapshot model and the GraphQL fetch through GitLab's ETag cache.

The query sends `X-GITLAB-GRAPHQL-RESOURCE-ETAG: pipelines/id/<id>` and `If-None-Match`. GitLab answers an unchanged
pipeline with a 304 from its cache before running the query, and touches the key on every job transition of the
pipeline and its downstream pipelines. So one request is both the probe and the fetch.

GitLab's lookup by ID leaves out child pipelines (the ones a `trigger:` job starts in the same project), while the
lookup by IID finds them. So when the query by ID answers no pipeline, the IID comes from the REST endpoint, which sees
child pipelines too, and the tree is fetched by IID. The caller keeps that IID and passes it to later fetches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from glab_cli_extras.gitlab import queries
from glab_cli_extras.gitlab.client import GitLabClient, NotFound, encode_project
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
    lookup_iid: int | None = None
    """The IID the tree was fetched by, when the lookup by ID cannot see the pipeline. Pass it to the next fetch."""


async def fetch_tree(
    client: GitLabClient,
    ref: ResourceRef,
    if_none_match: str | None,
    *,
    use_etag: bool = True,
    lookup_iid: int | None = None,
) -> TreeResult:
    """Fetch the pipeline tree. With `use_etag`, an unchanged pipeline answers 304 and the result has no snapshot.

    Without `lookup_iid` the tree is fetched by ID, and a pipeline the lookup by ID cannot see costs one REST request
    for its IID and a second query by that IID. A pipeline REST does not find either raises NotFound.
    """
    assert ref.id is not None
    etag_resource = f"pipelines/id/{ref.id}" if use_etag else None
    if lookup_iid is None:
        result = await client.graphql(
            queries.load("pipeline", "pipeline_fragments"),
            {"project": ref.project, "id": f"gid://gitlab/Ci::Pipeline/{ref.id}"},
            etag_resource=etag_resource,
            if_none_match=if_none_match,
            kind="pipeline",
        )
        if result.not_modified:
            return TreeResult(True, None, result.etag)
        if ((result.data or {}).get("project") or {}).get("pipeline"):
            return TreeResult(False, parse_pipeline(result.data, ref.host, ref.project), result.etag)
        lookup_iid = await _pipeline_iid(client, ref)
        # The query by ID just ran, so a conditional request could only answer 304 with no tree to return.
        if_none_match = None
    result = await client.graphql(
        queries.load("pipeline_by_iid", "pipeline_fragments"),
        {"project": ref.project, "iid": str(lookup_iid)},
        etag_resource=etag_resource,
        if_none_match=if_none_match,
        kind="pipeline",
    )
    if result.not_modified:
        return TreeResult(True, None, result.etag, lookup_iid)
    return TreeResult(False, parse_pipeline(result.data, ref.host, ref.project), result.etag, lookup_iid)


async def _pipeline_iid(client: GitLabClient, ref: ResourceRef) -> int:
    """The IID of the pipeline from REST, which finds child pipelines. Raises NotFound when it does not exist."""
    try:
        data = await client.get_json(f"projects/{encode_project(ref.project)}/pipelines/{ref.id}", kind="pipeline_iid")
    except NotFound as exc:
        raise NotFound(f"pipeline not found in {ref.project}", 404) from exc
    iid = data.get("iid") if isinstance(data, dict) else None
    if not isinstance(iid, int):
        raise NotFound(f"pipeline not found in {ref.project}", 404)
    return iid
