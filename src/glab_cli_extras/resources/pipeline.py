"""Pipeline watching through GitLab's GraphQL ETag cache.

Each poll sends the whole pipeline-tree query with `X-GITLAB-GRAPHQL-RESOURCE-ETAG: pipelines/id/<id>` and
`If-None-Match`. GitLab answers an unchanged pipeline with a 304 from its cache before running the query, and touches
the key on every job transition of the pipeline and its downstream pipelines. So one request is both the probe and
the fetch. If the server sends no ETag, the poller falls back to plain polling at a slower interval.

A job that ends and needs its log (per `--logs`) holds back its `job_finished` event until the daemon has the log in
memory, a few seconds later, so the client's request for it right after the event costs no GitLab request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from glab_cli_extras.events import RawEvent
from glab_cli_extras.gitlab.client import ApiError
from glab_cli_extras.resources.base import PollContext, Poller
from glab_cli_extras.sync.job_logs import LogContent, fetch_log_content, wants_log
from glab_cli_extras.sync.pipeline import (
    FINAL_JOB_STATUSES,
    FINAL_PIPELINE_STATUSES,
    QUEUED_PIPELINE_STATUSES,
    DownstreamState,
    JobState,
    PipelineSnapshot,
    fetch_tree,
    parse_pipeline,
)

log = logging.getLogger(__name__)

LOG_FETCH_DELAY = 5.0
"""Wait after a job ends, so GitLab can archive the trace first."""
LOG_FETCH_ATTEMPTS = 3

INTERVAL_RUNNING = 5.0
INTERVAL_QUEUED = 30.0
INTERVAL_RUNNING_NO_ETAG = 15.0
INTERVAL_QUEUED_NO_ETAG = 60.0


def _job_data(job: JobState) -> dict[str, Any]:
    data: dict[str, Any] = {
        "pipeline_id": job.pipeline_id,
        "project": job.project,
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
            data = {"pipeline_id": cur.id, "project": cur.project, "status": cur.status, "web_url": cur.web_url}
            events.append(RawEvent("finished", data))
    return events


def log_cache_key(host: str, job_id: int) -> tuple[str, str, int]:
    return ("log", host, job_id)


async def prefetch_log(ctx: PollContext, job: JobState) -> LogContent:
    """The log of an ended job, from the engine's cache or fetched into it."""
    key = log_cache_key(ctx.client.host, job.id)
    cached = ctx.logs.get(key)
    if cached is not None:
        return cached
    content = await fetch_log_content(ctx.client, job.project, job.id, job.trace_size)
    ctx.logs.put(key, content, len(content.body))
    return content


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
    def etag(self) -> str | None:
        return self._state.etag if self._state.supported else None

    @property
    def pipeline_final(self) -> bool:
        return self.snapshot is not None and self.snapshot.status in FINAL_PIPELINE_STATUSES

    async def _fetch(self) -> PipelineSnapshot | None:
        assert self.ref.id is not None
        etag = self._state.etag if self._state.supported else None
        result = await fetch_tree(self.ctx.client, self.ref, etag, use_etag=self._state.supported)
        if result.not_modified:
            return None
        if self._state.supported and not result.etag:
            log.info("%s: no ETag from GitLab, falling back to plain polling", self.ref.key)
            self._state.supported = False
        self._state.etag = result.etag
        return result.snapshot

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
                content = await prefetch_log(self.ctx, pending.job)
            except ApiError as exc:
                if pending.attempts < LOG_FETCH_ATTEMPTS:
                    pending.due = now + LOG_FETCH_DELAY * pending.attempts
                    continue
                data = {**pending.event.data, "log_error": str(exc)}
                released.append(replace(pending.event, data=data))
            else:
                data = {**pending.event.data, "log_truncated": content.truncated}
                released.append(replace(pending.event, data=data))
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


__all__ = [
    "FINAL_JOB_STATUSES",
    "FINAL_PIPELINE_STATUSES",
    "QUEUED_PIPELINE_STATUSES",
    "DownstreamState",
    "JobState",
    "PipelinePoller",
    "PipelineSnapshot",
    "diff_pipeline",
    "parse_pipeline",
]
