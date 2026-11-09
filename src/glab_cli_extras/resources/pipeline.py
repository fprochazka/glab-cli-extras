"""Pipeline watching through GitLab's GraphQL ETag cache.

Each poll sends the whole pipeline-tree query with `X-GITLAB-GRAPHQL-RESOURCE-ETAG: pipelines/id/<id>` and
`If-None-Match`. GitLab answers an unchanged pipeline with a 304 from its cache before running the query, and touches
the key on every job transition of the pipeline and its downstream pipelines. So one request is both the probe and
the fetch. If the server sends no ETag, the poller falls back to plain polling at a slower interval.

A job that ends and needs its log (per `--logs`) holds back its `job_finished` event until the log is in the cache,
a few seconds later, so the event can carry the log path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from glab_cli_extras.artifacts.job_logs import fetch_job_log, wants_log
from glab_cli_extras.events import RawEvent
from glab_cli_extras.gitlab import queries
from glab_cli_extras.gitlab.client import ApiError, NotFound
from glab_cli_extras.resources.base import Poller, gid_to_int, lower

log = logging.getLogger(__name__)

FINAL_JOB_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})
FINAL_PIPELINE_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})
QUEUED_PIPELINE_STATUSES = frozenset({"created", "pending", "waiting_for_resource", "preparing", "scheduled"})
LOG_FETCH_DELAY = 5.0
"""Wait after a job ends, so GitLab can archive the trace first."""
LOG_FETCH_ATTEMPTS = 3

INTERVAL_RUNNING = 5.0
INTERVAL_QUEUED = 30.0
INTERVAL_RUNNING_NO_ETAG = 15.0
INTERVAL_QUEUED_NO_ETAG = 60.0


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


def _job_data(job: JobState) -> dict[str, Any]:
    data: dict[str, Any] = {
        "pipeline_id": job.pipeline_id,
        "job_id": job.id,
        "name": job.name,
        "stage": job.stage,
        "status": job.status,
    }
    if job.allow_failure:
        data["allow_failure"] = True
    return data


def diff_pipeline(prev: PipelineSnapshot, cur: PipelineSnapshot) -> list[RawEvent]:
    """Events between two snapshots of one pipeline tree: job transitions first, then the pipeline's own status."""
    events: list[RawEvent] = []
    known_downstream = {d.id for d in prev.downstream}
    for child in cur.downstream:
        if child.id not in known_downstream:
            events.append(
                RawEvent(
                    "downstream_created",
                    {
                        "pipeline_id": cur.id,
                        "downstream_pipeline_id": child.id,
                        "project": child.project,
                        "status": child.status,
                        "web_url": child.web_url,
                    },
                )
            )
    old_jobs = {j.id: j for j in prev.jobs}
    for job in cur.jobs:
        old = old_jobs.get(job.id)
        old_status = old.status if old else None
        if old_status == job.status:
            continue
        if job.status == "running" and old_status != "running":
            events.append(RawEvent("job_started", _job_data(job)))
        elif job.status in FINAL_JOB_STATUSES and job.status != "skipped" and old_status not in FINAL_JOB_STATUSES:
            events.append(RawEvent("job_finished", _job_data(job)))
    if prev.status != cur.status:
        events.append(RawEvent("status_changed", {"pipeline_id": cur.id, "from": prev.status, "to": cur.status}))
        if cur.status in FINAL_PIPELINE_STATUSES and prev.status not in FINAL_PIPELINE_STATUSES:
            events.append(RawEvent("finished", {"pipeline_id": cur.id, "status": cur.status, "web_url": cur.web_url}))
    return events


@dataclass
class _PendingLog:
    job: JobState
    event: RawEvent
    due: float
    attempts: int = 0


@dataclass
class _EtagState:
    etag: str | None = None
    supported: bool = True
    pending_logs: dict[int, _PendingLog] = field(default_factory=dict)


class PipelinePoller(Poller[PipelineSnapshot]):
    kind: ClassVar = "pipeline"
    snapshot_type: ClassVar = PipelineSnapshot

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._state = _EtagState()

    @property
    def pipeline_final(self) -> bool:
        return self.snapshot is not None and self.snapshot.status in FINAL_PIPELINE_STATUSES

    async def _fetch(self) -> PipelineSnapshot | None:
        assert self.ref.id is not None
        result = await self.ctx.client.graphql(
            queries.load("pipeline"),
            {"project": self.ref.project, "id": f"gid://gitlab/Ci::Pipeline/{self.ref.id}"},
            etag_resource=f"pipelines/id/{self.ref.id}" if self._state.supported else None,
            if_none_match=self._state.etag,
            kind="pipeline",
        )
        if result.not_modified:
            return None
        if self._state.supported and not result.etag:
            log.info("%s: no ETag from GitLab, falling back to plain polling", self.ref.key)
            self._state.supported = False
        self._state.etag = result.etag
        return parse_pipeline(result.data, self.ref.host, self.ref.project)

    async def poll(self) -> list[RawEvent]:
        events: list[RawEvent] = []
        if not self.pipeline_final or self.snapshot is None:
            cur = await self._fetch()
            if cur is not None:
                prev = self.snapshot
                self.snapshot = cur
                if prev is not None:
                    events.extend(self._hold_back_for_logs(diff_pipeline(prev, cur)))
        events.extend(await self._fetch_due_logs())
        if self.pipeline_final and not self._state.pending_logs and self.snapshot is not None:
            self.terminal_reason = "finished"
            self.terminal_data = {"status": self.snapshot.status}
        return events

    def _hold_back_for_logs(self, events: list[RawEvent]) -> list[RawEvent]:
        if self.options.logs == "none" or self.snapshot is None:
            return events
        jobs = {j.id: j for j in self.snapshot.jobs}
        released = []
        now = self.ctx.clock()
        for event in events:
            job = jobs.get(int(event.data.get("job_id", 0))) if event.kind == "job_finished" else None
            if job is not None and wants_log(self.options.logs, job.status):
                self._state.pending_logs[job.id] = _PendingLog(job, event, now + LOG_FETCH_DELAY)
            else:
                released.append(event)
        return released

    async def _fetch_due_logs(self) -> list[RawEvent]:
        now = self.ctx.clock()
        released = []
        for job_id, pending in sorted(self._state.pending_logs.items()):
            if pending.due > now:
                continue
            pending.attempts += 1
            try:
                fetched = await fetch_job_log(
                    self.ctx.client,
                    self.ctx.cache_root,
                    pending.job.project,
                    pending.job.id,
                    pending.job.name,
                    pending.job.trace_size,
                )
            except (ApiError, OSError) as exc:
                if pending.attempts < LOG_FETCH_ATTEMPTS:
                    pending.due = now + LOG_FETCH_DELAY * pending.attempts
                    continue
                data = {**pending.event.data, "log_error": str(exc)}
                released.append(replace(pending.event, data=data))
            else:
                data = {**pending.event.data, "log_truncated": fetched.truncated}
                released.append(replace(pending.event, data=data, artifacts=(str(fetched.path),)))
            del self._state.pending_logs[job_id]
        return released

    def next_delay(self) -> float:
        if self._state.pending_logs:
            now = self.ctx.clock()
            soonest = min(p.due for p in self._state.pending_logs.values())
            return max(0.5, min(soonest - now, INTERVAL_RUNNING))
        queued = self.snapshot is not None and self.snapshot.status in QUEUED_PIPELINE_STATUSES
        if self._state.supported:
            return INTERVAL_QUEUED if queued else INTERVAL_RUNNING
        return INTERVAL_QUEUED_NO_ETAG if queued else INTERVAL_RUNNING_NO_ETAG

    def expected_cost(self) -> float:
        # Most ETag probes are 304s, which the budget charges at 0.1.
        return 0.3 if self._state.supported else 1.0

    def summary(self) -> dict[str, Any]:
        snap = self.snapshot
        if snap is None:
            return {}
        counts: dict[str, int] = {}
        for job in snap.jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        return {
            "pipeline_id": snap.id,
            "status": snap.status,
            "ref": snap.ref,
            "sha": snap.sha,
            "web_url": snap.web_url,
            "jobs": dict(sorted(counts.items())),
            "downstream": [d.id for d in snap.downstream],
            "failed_jobs": [j.name for j in snap.jobs if j.status == "failed" and not j.allow_failure],
        }
