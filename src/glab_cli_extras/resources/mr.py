"""Merge request watching: one GraphQL snapshot per poll, follow-up requests only for what moved.

- A moved `diffHeadSha` is a push. One `merge_base` request tells a fast-forward from a force-push.
- A thread whose notes changed (`updatedAt` moves on edit, resolve and unresolve) is re-fetched alone and its file
  rewritten. Many changed threads at once are fetched with one list request instead.
- The head pipeline is a separate, shared `pipeline` resource; the daemon relays its events into the MR's stream.

The MR's own `updated_at` is not used: GitLab skips touching it within a minute of the last touch, and head pipeline
changes and single resolves never touch it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from glab_cli_extras.artifacts.threads import ThreadStore, is_system_discussion
from glab_cli_extras.events import GROUPS, PIPELINE_KINDS, RawEvent
from glab_cli_extras.gitlab import queries
from glab_cli_extras.gitlab.client import NotFound, encode_project
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.paths import project_dir
from glab_cli_extras.resources.base import Poller, PollerOptions, gid_to_int, is_force_push, lower
from glab_cli_extras.serde import stable_hash

log = logging.getLogger(__name__)

TRANSIENT_MERGE_STATUSES = frozenset({"checking", "unchecked", "preparing"})
"""GitLab reports these while it recomputes mergeability; the last settled value stays until a new one arrives."""
TERMINAL_STATES = frozenset({"merged", "closed"})
THREAD_LIST_THRESHOLD = 3
"""Above this many changed threads, one paginated list request is cheaper than one request per thread."""
INTERVAL = 30.0
INTERVAL_PREMIUM = 15.0
MAX_THREAD_PAGES = 10

COMMENT_KINDS = GROUPS["comments"]


@dataclass(frozen=True, slots=True)
class NoteState:
    id: int
    updated_at: str
    author: str | None


@dataclass(frozen=True, slots=True)
class ThreadState:
    id: str
    resolvable: bool
    resolved: bool
    notes: tuple[NoteState, ...]


@dataclass(frozen=True, slots=True)
class MrSnapshot:
    iid: int
    project: str
    state: str
    title: str
    description_hash: str
    draft: bool
    merge_status: str | None
    head_sha: str | None
    source_branch: str | None
    target_branch: str | None
    web_url: str | None
    approved: bool | None
    approvals_left: int | None
    approved_by: tuple[str, ...]
    reviewers: tuple[str, ...]
    head_pipeline_id: int | None = None
    head_pipeline_status: str | None = None
    head_pipeline_project: str | None = None
    threads: tuple[ThreadState, ...] | None = None
    """None when no subscriber wants comment events, so the query leaves discussions out."""


def _discussion_id(gid: Any) -> str:
    """GraphQL `gid://gitlab/Discussion/<sha>` → the REST discussion id `<sha>`."""
    return str(gid).rsplit("/", 1)[-1]


def parse_threads(nodes: list[Any]) -> tuple[ThreadState, ...]:
    threads = []
    for node in nodes:
        notes = (node.get("notes") or {}).get("nodes") or []
        if notes and all(n.get("system") for n in notes):
            continue
        threads.append(
            ThreadState(
                id=_discussion_id(node["id"]),
                resolvable=bool(node.get("resolvable")),
                resolved=bool(node.get("resolved")),
                notes=tuple(
                    NoteState(
                        id=gid_to_int(n["id"]),
                        updated_at=str(n.get("updatedAt") or ""),
                        author=(n.get("author") or {}).get("username"),
                    )
                    for n in notes
                    if not n.get("system")
                ),
            )
        )
    return tuple(sorted(threads, key=lambda t: t.id))


def parse_mr(node: Any, project: str, threads: tuple[ThreadState, ...] | None) -> MrSnapshot:
    if not node:
        raise NotFound(f"merge request not found in {project}", 404)
    pipeline = node.get("headPipeline") or None
    return MrSnapshot(
        iid=int(node["iid"]),
        project=project,
        state=lower(node.get("state")),
        title=str(node.get("title") or ""),
        description_hash=stable_hash(str(node.get("description") or "")),
        draft=bool(node.get("draft")),
        merge_status=lower(node.get("detailedMergeStatus")) or None,
        head_sha=node.get("diffHeadSha"),
        source_branch=node.get("sourceBranch"),
        target_branch=node.get("targetBranch"),
        web_url=node.get("webUrl"),
        approved=node.get("approved"),
        approvals_left=node.get("approvalsLeft"),
        approved_by=tuple(sorted(n["username"] for n in (node.get("approvedBy") or {}).get("nodes") or [])),
        reviewers=tuple(sorted(n["username"] for n in (node.get("reviewers") or {}).get("nodes") or [])),
        head_pipeline_id=gid_to_int(pipeline["id"]) if pipeline else None,
        head_pipeline_status=lower(pipeline.get("status")) if pipeline else None,
        head_pipeline_project=((pipeline.get("project") or {}).get("fullPath") or project) if pipeline else None,
        threads=threads,
    )


def settle(prev: MrSnapshot | None, cur: MrSnapshot) -> MrSnapshot:
    """Keep the last settled merge status while GitLab reports a transient one."""
    if prev is not None and cur.merge_status in TRANSIENT_MERGE_STATUSES:
        return replace(cur, merge_status=prev.merge_status)
    return cur


def _set_diff(old: tuple[str, ...], new: tuple[str, ...]) -> tuple[list[str], list[str]]:
    return sorted(set(new) - set(old)), sorted(set(old) - set(new))


def diff_threads(prev: tuple[ThreadState, ...], cur: tuple[ThreadState, ...]) -> list[RawEvent]:
    events: list[RawEvent] = []
    old_threads = {t.id: t for t in prev}
    for thread in cur:
        old = old_threads.get(thread.id)
        if old is None:
            first = thread.notes[0] if thread.notes else None
            events.append(
                RawEvent(
                    "thread_created",
                    {
                        "discussion_id": thread.id,
                        "author": first.author if first else None,
                        "resolvable": thread.resolvable,
                        "resolved": thread.resolved,
                        "notes": len(thread.notes),
                    },
                )
            )
            continue
        old_notes = {n.id: n for n in old.notes}
        new_notes = [n for n in thread.notes if n.id not in old_notes]
        resolution_changed = old.resolved != thread.resolved
        edited = [n for n in thread.notes if n.id in old_notes and old_notes[n.id].updated_at != n.updated_at]
        removed = len(set(old_notes) - {n.id for n in thread.notes})
        # Resolving bumps `updatedAt` on every note of the thread, so an edit only counts without a resolve.
        if new_notes or removed or (edited and not resolution_changed):
            last = thread.notes[-1] if thread.notes else None
            events.append(
                RawEvent(
                    "thread_updated",
                    {
                        "discussion_id": thread.id,
                        "new_notes": len(new_notes),
                        "edited_notes": 0 if resolution_changed else len(edited),
                        "removed_notes": removed,
                        "last_author": last.author if last else None,
                        "resolved": thread.resolved,
                    },
                )
            )
        if resolution_changed:
            kind = "thread_resolved" if thread.resolved else "thread_unresolved"
            events.append(RawEvent(kind, {"discussion_id": thread.id}))
    return events


def diff_mr(prev: MrSnapshot, cur: MrSnapshot) -> list[RawEvent]:
    """Events between two settled snapshots of one MR, in a stable order."""
    events: list[RawEvent] = []
    if prev.state != cur.state:
        data: dict[str, Any] = {"from": prev.state, "to": cur.state}
        if prev.state == "closed" and cur.state == "opened":
            data["reopened"] = True
        events.append(RawEvent("state_changed", data))
    if prev.draft != cur.draft:
        events.append(RawEvent("draft_changed", {"draft": cur.draft}))
    if prev.title != cur.title:
        events.append(RawEvent("title_changed", {"from": prev.title, "to": cur.title}))
    if prev.description_hash != cur.description_hash:
        events.append(RawEvent("description_changed", {}))
    if prev.head_sha != cur.head_sha and cur.head_sha:
        events.append(RawEvent("pushed", {"from": prev.head_sha, "to": cur.head_sha}))
    if prev.merge_status != cur.merge_status and cur.merge_status:
        events.append(RawEvent("merge_status_changed", {"from": prev.merge_status, "to": cur.merge_status}))
    if (prev.approved_by, prev.approvals_left, prev.approved) != (cur.approved_by, cur.approvals_left, cur.approved):
        added, removed = _set_diff(prev.approved_by, cur.approved_by)
        events.append(
            RawEvent(
                "approvals_changed",
                {
                    "approved": cur.approved,
                    "approvals_left": cur.approvals_left,
                    "approved_by": list(cur.approved_by),
                    "added": added,
                    "removed": removed,
                },
            )
        )
    if prev.reviewers != cur.reviewers:
        added, removed = _set_diff(prev.reviewers, cur.reviewers)
        events.append(
            RawEvent("reviewers_changed", {"reviewers": list(cur.reviewers), "added": added, "removed": removed})
        )
    if prev.head_pipeline_id != cur.head_pipeline_id and cur.head_pipeline_id is not None:
        events.append(
            RawEvent(
                "head_pipeline_changed",
                {
                    "pipeline_id": cur.head_pipeline_id,
                    "status": cur.head_pipeline_status,
                    "previous_pipeline_id": prev.head_pipeline_id,
                },
            )
        )
    if prev.threads is not None and cur.threads is not None:
        events.extend(diff_threads(prev.threads, cur.threads))
    return events


class MrPoller(Poller[MrSnapshot]):
    kind: ClassVar = "mr"
    snapshot_type: ClassVar = MrSnapshot

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._threads: ThreadStore | None = None

    @property
    def _wants_threads(self) -> bool:
        return self.options.wants(*COMMENT_KINDS)

    @property
    def _wants_pipeline(self) -> bool:
        return self.options.wants("head_pipeline_changed", *PIPELINE_KINDS)

    @property
    def _encoded(self) -> str:
        return encode_project(self.ref.project)

    def thread_store(self, web_url: str | None) -> ThreadStore:
        if self._threads is None:
            directory = (
                project_dir(self.ctx.cache_root, self.ref.host, self.ref.project) / f"mr-{self.ref.id}" / "threads"
            )
            fallback = f"https://{self.ref.host}/{self.ref.project}/-/merge_requests/{self.ref.id}"
            self._threads = ThreadStore(directory, web_url or fallback)
        return self._threads

    async def _fetch_snapshot(self) -> MrSnapshot:
        with_threads = self._wants_threads
        variables: dict[str, str | int | bool] = {
            "project": self.ref.project,
            "iid": str(self.ref.id),
            "withThreads": with_threads,
            "withPipeline": self._wants_pipeline,
        }
        result = await self.ctx.client.graphql(queries.load("mr"), variables, kind="mr")
        node = ((result.data or {}).get("project") or {}).get("mergeRequest")
        if not node:
            raise NotFound(f"merge request !{self.ref.id} not found in {self.ref.project}", 404)
        threads: tuple[ThreadState, ...] | None = None
        if with_threads:
            connection = node.get("discussions") or {}
            nodes = list(connection.get("nodes") or [])
            pages = 1
            while (connection.get("pageInfo") or {}).get("hasNextPage") and pages < MAX_THREAD_PAGES:
                variables["threadsAfter"] = str(connection["pageInfo"]["endCursor"])
                more = await self.ctx.client.graphql(queries.load("mr"), variables, kind="mr")
                connection = (
                    (((more.data or {}).get("project") or {}).get("mergeRequest") or {}).get("discussions")
                ) or {}
                nodes.extend(connection.get("nodes") or [])
                pages += 1
            threads = parse_threads(nodes)
        return parse_mr(node, self.ref.project, threads)

    async def _is_force_push(self, old_sha: str, new_sha: str) -> tuple[bool, str | None]:
        return await is_force_push(self.ctx.client, self.ref.project, old_sha, new_sha)

    async def _sync_all_threads(self, store: ThreadStore) -> dict[str, str]:
        paths: dict[str, str] = {}
        page = 1
        while page <= MAX_THREAD_PAGES:
            batch = await self.ctx.client.get_json(
                f"projects/{self._encoded}/merge_requests/{self.ref.id}/discussions",
                {"per_page": 100, "page": page},
                kind="discussions",
            )
            if not isinstance(batch, list) or not batch:
                break
            for discussion in batch:
                if isinstance(discussion, dict) and not is_system_discussion(discussion):
                    path, _ = store.write(discussion)
                    paths[str(discussion["id"])] = str(path)
            if len(batch) < 100:
                break
            page += 1
        return paths

    async def _sync_threads(self, store: ThreadStore, ids: set[str]) -> dict[str, str]:
        if len(ids) > THREAD_LIST_THRESHOLD:
            return await self._sync_all_threads(store)
        paths = {}
        for discussion_id in sorted(ids):
            discussion = await self.ctx.client.get_json(
                f"projects/{self._encoded}/merge_requests/{self.ref.id}/discussions/{discussion_id}",
                kind="discussion",
            )
            if isinstance(discussion, dict):
                path, _ = store.write(discussion)
                paths[discussion_id] = str(path)
        return paths

    async def poll(self) -> list[RawEvent]:
        prev = self.snapshot
        cur = settle(prev, await self._fetch_snapshot())
        store = self.thread_store(cur.web_url) if cur.threads is not None else None

        if prev is None or (prev.threads is None and cur.threads is not None):
            # Baseline, or comment tracking just started: write every thread file, report nothing about threads.
            if store is not None:
                await self._sync_all_threads(store)
            if prev is None:
                self.snapshot = cur
                self._check_terminal()
                return []
            prev = replace(prev, threads=cur.threads)

        events = diff_mr(prev, cur)
        resolved_events = []
        for event in events:
            if event.kind == "pushed" and self.options.wants("pushed") and event.data.get("from"):
                forced, reason = await self._is_force_push(str(event.data["from"]), str(event.data["to"]))
                data = {**event.data, "forced": forced}
                if reason:
                    data["forced_reason"] = reason
                event = replace(event, data=data)
            resolved_events.append(event)
        events = resolved_events

        if store is not None and prev.threads is not None and cur.threads is not None:
            events = await self._attach_thread_files(store, prev.threads, cur.threads, events)

        self.snapshot = cur
        self._check_terminal()
        return events

    async def _attach_thread_files(
        self,
        store: ThreadStore,
        prev: tuple[ThreadState, ...],
        cur: tuple[ThreadState, ...],
        events: list[RawEvent],
    ) -> list[RawEvent]:
        changed = {str(e.data["discussion_id"]) for e in events if e.kind in COMMENT_KINDS}
        for gone in {t.id for t in prev} - {t.id for t in cur}:
            store.remove(gone)
        if not changed:
            return events
        paths = await self._sync_threads(store, changed)
        attached = []
        for event in events:
            if event.kind in COMMENT_KINDS:
                path = paths.get(str(event.data["discussion_id"]))
                if path:
                    event = replace(event, artifacts=(path,))
            attached.append(event)
        return attached

    def _check_terminal(self) -> None:
        if self.snapshot is not None and self.snapshot.state in TERMINAL_STATES:
            self.terminal_reason = self.snapshot.state
            self.terminal_data = {"state": self.snapshot.state}

    def next_delay(self) -> float:
        return INTERVAL_PREMIUM if self.ctx.client.budget.premium_level else INTERVAL

    def summary(self) -> dict[str, Any]:
        snap = self.snapshot
        if snap is None:
            return {}
        summary: dict[str, Any] = {
            "iid": snap.iid,
            "title": snap.title,
            "state": snap.state,
            "draft": snap.draft,
            "merge_status": snap.merge_status,
            "head_sha": snap.head_sha,
            "source_branch": snap.source_branch,
            "target_branch": snap.target_branch,
            "web_url": snap.web_url,
            "approved": snap.approved,
            "approvals_left": snap.approvals_left,
            "approved_by": list(snap.approved_by),
            "reviewers": list(snap.reviewers),
        }
        if snap.head_pipeline_id is not None:
            summary["head_pipeline"] = {"id": snap.head_pipeline_id, "status": snap.head_pipeline_status}
        if snap.threads is not None:
            summary["threads"] = {
                "total": len(snap.threads),
                "unresolved": sum(1 for t in snap.threads if t.resolvable and not t.resolved),
                "dir": str(self.thread_store(snap.web_url).directory),
            }
        return summary

    def children(self) -> frozenset[ResourceRef]:
        snap = self.snapshot
        if snap is None or snap.head_pipeline_id is None or self.terminal:
            return frozenset()
        if not self.options.wants(*PIPELINE_KINDS):
            return frozenset()
        project = snap.head_pipeline_project or self.ref.project
        return frozenset({ResourceRef("pipeline", self.ref.host, project, snap.head_pipeline_id)})

    def child_options(self, child: ResourceRef) -> PollerOptions | None:
        return PollerOptions(self.options.events & frozenset(PIPELINE_KINDS), self.options.logs)
