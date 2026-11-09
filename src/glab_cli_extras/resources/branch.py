"""Branch watching: pushes from the project's push-event feed, pipelines through the GraphQL ETag cache.

- Pushes come from `GET /projects/:id/events?action=pushed`, shared by every watched branch of the project and read
  every 30 s. Push events carry no "forced" flag, so a push costs one `merge_base` request to tell a force-push.
  A bulk push (more refs than GitLab's push-event limit) has no ref or SHAs; the branch endpoint fills the gap.
- Pipelines come from one GraphQL query keyed on `project_pipelines/<project id>`, probed every 10 s. GitLab
  touches that key on any pipeline change in the project, and answers an unchanged key with a free 304.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar
from urllib.parse import quote

from glab_cli_extras.events import RawEvent
from glab_cli_extras.gitlab import queries
from glab_cli_extras.gitlab.client import NotFound, encode_project
from glab_cli_extras.resources.base import Poller, gid_to_int, is_force_push, lower
from glab_cli_extras.resources.pipeline import FINAL_PIPELINE_STATUSES

PUSH_INTERVAL = 30.0
PIPELINE_INTERVAL = 10.0
FEED_PAGE_SIZE = 20
MAX_FEED_PAGES = 5
MERGE_TITLE_RE = re.compile(r"^Merge (branch|remote-tracking branch|pull request) ")


@dataclass(frozen=True, slots=True)
class PipelineEntry:
    id: int
    status: str
    sha: str | None


@dataclass(frozen=True, slots=True)
class BranchSnapshot:
    project: str
    branch: str
    exists: bool
    head_sha: str | None
    last_event_id: int
    project_id: int | None = None
    pipelines: tuple[PipelineEntry, ...] = field(default_factory=tuple)


def diff_pipelines(prev: tuple[PipelineEntry, ...], cur: tuple[PipelineEntry, ...], web: str) -> list[RawEvent]:
    known = {p.id: p for p in prev}
    events = []
    for pipeline in sorted(cur, key=lambda p: p.id):
        old = known.get(pipeline.id)
        data = {
            "pipeline_id": pipeline.id,
            "status": pipeline.status,
            "sha": pipeline.sha,
            "web_url": f"{web}/-/pipelines/{pipeline.id}",
        }
        if old is None:
            events.append(RawEvent("pipeline_created", data))
            if pipeline.status in FINAL_PIPELINE_STATUSES:
                events.append(RawEvent("pipeline_finished", data))
        elif pipeline.status in FINAL_PIPELINE_STATUSES and old.status not in FINAL_PIPELINE_STATUSES:
            events.append(RawEvent("pipeline_finished", data))
    return events


def new_branch_pushes(feed: list[Any], branch: str, after_id: int) -> tuple[list[dict[str, Any]], bool]:
    """Push events newer than `after_id` for the branch, oldest first, plus whether a bulk push is among them."""
    relevant = []
    bulk = False
    for event in feed:
        if not isinstance(event, dict) or int(event.get("id", 0)) <= after_id:
            continue
        push = event.get("push_data") or {}
        if push.get("ref") is None and push.get("ref_type") in (None, "branch") and push.get("ref_count"):
            bulk = True
        elif push.get("ref_type") == "branch" and push.get("ref") == branch:
            relevant.append(event)
    return sorted(relevant, key=lambda e: int(e["id"])), bulk


class BranchPoller(Poller[BranchSnapshot]):
    kind: ClassVar = "branch"
    snapshot_type: ClassVar = BranchSnapshot

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._etag: str | None = None
        self._last_feed_check = 0.0

    @property
    def _branch(self) -> str:
        assert self.ref.branch is not None
        return self.ref.branch

    @property
    def _encoded(self) -> str:
        return encode_project(self.ref.project)

    @property
    def _web(self) -> str:
        return f"https://{self.ref.host}/{self.ref.project}"

    async def _branch_head(self) -> str | None:
        try:
            data = await self.ctx.client.get_json(
                f"projects/{self._encoded}/repository/branches/{quote(self._branch, safe='')}", kind="branch"
            )
        except NotFound:
            return None
        return str((data.get("commit") or {}).get("id")) if isinstance(data, dict) else None

    async def _feed(self, after_id: int | None) -> list[Any]:
        """Push events, newest first, reaching back to `after_id`. None means the newest page is enough."""
        now = self.ctx.clock()
        cached = self.ctx.push_feeds.get(self.ref.host, self.ref.project, now)
        if cached is not None and (
            after_id is None or len(cached) < FEED_PAGE_SIZE or min(int(e.get("id", 0)) for e in cached) <= after_id
        ):
            return cached
        events: list[Any] = []
        for page in range(1, MAX_FEED_PAGES + 1):
            batch = await self.ctx.client.get_json(
                f"projects/{self._encoded}/events",
                {"action": "pushed", "sort": "desc", "per_page": FEED_PAGE_SIZE, "page": page},
                kind="push_events",
            )
            if not isinstance(batch, list):
                break
            events.extend(batch)
            if after_id is None or len(batch) < FEED_PAGE_SIZE or (batch and int(batch[-1].get("id", 0)) <= after_id):
                break
        self.ctx.push_feeds.put(self.ref.host, self.ref.project, now, events)
        return events

    async def _pipelines(self, snap: BranchSnapshot | None) -> tuple[int | None, tuple[PipelineEntry, ...] | None]:
        project_id = snap.project_id if snap else None
        result = await self.ctx.client.graphql(
            queries.load("branch_pipelines"),
            {"project": self.ref.project, "ref": self._branch},
            etag_resource=f"project_pipelines/{project_id}" if project_id else None,
            if_none_match=self._etag if project_id else None,
            kind="branch_pipelines",
        )
        if result.not_modified:
            return project_id, None
        project = (result.data or {}).get("project")
        if not project:
            raise NotFound(f"project {self.ref.project} not found", 404)
        self._etag = result.etag if project_id else None
        nodes = (project.get("pipelines") or {}).get("nodes") or []
        entries = tuple(
            sorted(
                (PipelineEntry(gid_to_int(n["id"]), lower(n.get("status")), n.get("sha")) for n in nodes),
                key=lambda p: p.id,
            )
        )
        return gid_to_int(project["id"]), entries

    async def _baseline(self) -> list[RawEvent]:
        head = await self._branch_head()
        feed = await self._feed(None)
        last_id = max((int(e.get("id", 0)) for e in feed if isinstance(e, dict)), default=0)
        project_id, pipelines = await self._pipelines(None)
        self._last_feed_check = self.ctx.clock()
        self.snapshot = BranchSnapshot(
            self.ref.project, self._branch, head is not None, head, last_id, project_id, pipelines or ()
        )
        return []

    async def poll(self) -> list[RawEvent]:
        if self.snapshot is None:
            return await self._baseline()
        snap = self.snapshot
        events: list[RawEvent] = []
        if self.options.wants("pipeline_created", "pipeline_finished"):
            project_id, pipelines = await self._pipelines(snap)
            if pipelines is not None:
                events.extend(diff_pipelines(snap.pipelines, pipelines, self._web))
                snap = replace(snap, project_id=project_id, pipelines=pipelines)
        now = self.ctx.clock()
        if now - self._last_feed_check >= PUSH_INTERVAL:
            push_events, snap = await self._push_events(snap)
            events.extend(push_events)
            self._last_feed_check = now
        self.snapshot = snap
        return events

    async def _push_events(self, snap: BranchSnapshot) -> tuple[list[RawEvent], BranchSnapshot]:
        feed = await self._feed(snap.last_event_id)
        pushes, bulk = new_branch_pushes(feed, self._branch, snap.last_event_id)
        newest = max((int(e.get("id", 0)) for e in feed if isinstance(e, dict)), default=snap.last_event_id)
        events: list[RawEvent] = []
        head, exists = snap.head_sha, snap.exists
        for push in pushes:
            data = push.get("push_data") or {}
            action = data.get("action")
            author = push.get("author_username")
            if action == "created":
                events.append(RawEvent("created", {"sha": data.get("commit_to"), "author": author}))
                head, exists = data.get("commit_to"), True
            elif action == "removed":
                events.append(RawEvent("deleted", {"sha": data.get("commit_from"), "author": author}))
                head, exists = None, False
            else:
                event = await self._push_event(data.get("commit_from"), data.get("commit_to"), author, data)
                events.append(event)
                head, exists = data.get("commit_to"), True
        if bulk and not pushes:
            current = await self._branch_head()
            if current != head:
                if current is None:
                    events.append(RawEvent("deleted", {"sha": head, "bulk_push": True}))
                elif head is None:
                    events.append(RawEvent("created", {"sha": current, "bulk_push": True}))
                else:
                    event = await self._push_event(head, current, None, {"bulk_push": True})
                    events.append(event)
                head, exists = current, current is not None
        return events, replace(snap, head_sha=head, exists=exists, last_event_id=max(newest, snap.last_event_id))

    async def _push_event(self, old: Any, new: Any, author: Any, push_data: dict[str, Any]) -> RawEvent:
        data: dict[str, Any] = {"from": old, "to": new, "author": author}
        for key in ("commit_count", "commit_title", "bulk_push"):
            if push_data.get(key) is not None:
                data[key] = push_data[key]
        forced = False
        if old and new and self.options.wants("force_pushed", "pushed"):
            forced, reason = await is_force_push(self.ctx.client, self.ref.project, str(old), str(new))
            if reason:
                data["forced_reason"] = reason
        title = str(push_data.get("commit_title") or "")
        if not forced and new and MERGE_TITLE_RE.match(title):
            data["merge_commit"] = True
            merged = await self.ctx.client.get_json(
                f"projects/{self._encoded}/repository/commits/{new}/merge_requests", kind="commit_mrs"
            )
            iids = [m.get("iid") for m in merged or [] if isinstance(m, dict) and m.get("state") == "merged"]
            if iids:
                data["merged_mr_iid"] = iids[0]
        return RawEvent("force_pushed" if forced else "pushed", data)

    def next_delay(self) -> float:
        if self.options.wants("pipeline_created", "pipeline_finished"):
            return PIPELINE_INTERVAL
        return PUSH_INTERVAL

    def expected_cost(self) -> float:
        if self.options.wants("pipeline_created", "pipeline_finished"):
            # A 304 probe every 10 s, plus one feed request every third poll.
            return 0.1 + 1.0 / 3
        return 1.0

    def summary(self) -> dict[str, Any]:
        snap = self.snapshot
        if snap is None:
            return {}
        latest = max(snap.pipelines, key=lambda p: p.id) if snap.pipelines else None
        return {
            "branch": snap.branch,
            "exists": snap.exists,
            "head_sha": snap.head_sha,
            "latest_pipeline": {"id": latest.id, "status": latest.status} if latest else None,
        }
