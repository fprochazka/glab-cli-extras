"""Merge request watching: one GraphQL snapshot per poll, follow-up requests only for what moved.

- A moved `diffHeadSha` is a push. One `merge_base` request tells a fast-forward from a force-push.
- A thread whose notes changed (`updatedAt` moves on edit, resolve and unresolve) is re-fetched alone into the engine's
  cache, for the clients to write. Many changed threads at once are fetched with one list request instead.
- The head pipeline is reported at the level of the pipeline only, from the same snapshot: its id and status cost no
  extra request. Job-level detail is what `glab-watch pipeline` is for, and every pipeline event names that command.
- External commit statuses are not in the snapshot query; they cost one request per 100 statuses of the head commit.
  They are fetched again only when the head, the head pipeline or an unfinished external status moves, and otherwise
  every `STATUS_REFRESH_POLLS` polls: a gate outside the CI can report on a pipeline that is not the head pipeline,
  and then nothing in the snapshot moves.

The MR's own `updated_at` is not used: GitLab skips touching it within a minute of the last touch, and head pipeline
changes and single resolves never touch it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, ClassVar

from glab_cli_extras.events import GROUPS, MR_PIPELINE_KINDS, RawEvent
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources.base import PollContext, Poller, is_force_push
from glab_cli_extras.sync.mr import (
    ExternalStatus,
    MrSnapshot,
    NoteState,
    ThreadState,
    fetch_discussions,
    fetch_external_statuses,
    fetch_snapshot,
    parse_mr,
    parse_threads,
    thread_sig,
)
from glab_cli_extras.sync.pipeline import FINAL_PIPELINE_STATUSES

log = logging.getLogger(__name__)

TRANSIENT_MERGE_STATUSES = frozenset({"checking", "unchecked", "preparing"})
"""GitLab reports these while it recomputes mergeability; the last settled value stays until a new one arrives."""
TERMINAL_STATES = frozenset({"merged", "closed"})
INTERVAL = 30.0
AFTER_PUSH_POLLS = 2
INTERVAL_PREMIUM = 15.0

STATUS_REFRESH_POLLS = 10
"""About every five minutes, the external statuses are fetched even though nothing in the snapshot moved."""
UNFINISHED_STATUSES = frozenset({"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"})
"""External statuses that will change on their own. While one is unfinished, every poll fetches the statuses."""

COMMENT_KINDS = GROUPS["comments"]
EXTERNAL_STATUS_CHANGED = "external_status_changed"


def settle(prev: MrSnapshot | None, cur: MrSnapshot) -> MrSnapshot:
    """Keep the last settled merge status while GitLab reports a transient one, and the last known approvals while
    GitLab does not return them, so a poll without approvals never reads as "every approval was removed"."""
    if prev is None:
        return cur
    if cur.merge_status in TRANSIENT_MERGE_STATUSES:
        cur = replace(cur, merge_status=prev.merge_status)
    if cur.approved_by is None and prev.approved_by is not None:
        cur = replace(cur, approved_by=prev.approved_by, approvals_left=prev.approvals_left, approved=prev.approved)
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


def diff_external_statuses(prev: tuple[ExternalStatus, ...], cur: tuple[ExternalStatus, ...]) -> list[RawEvent]:
    """One event per external status that appeared or changed its status, by name. A status that disappears is not
    reported: that happens on a push, which `pushed` reports."""
    old = {s.name: s for s in prev}
    events = []
    for status in cur:
        before = old.get(status.name)
        if before is not None and before.status == status.status:
            continue
        data: dict[str, Any] = {
            "name": status.name,
            "from": before.status if before else None,
            "to": status.status,
            "allow_failure": status.allow_failure,
        }
        if status.description:
            data["description"] = status.description
        if status.target_url:
            data["target_url"] = status.target_url
        events.append(RawEvent(EXTERNAL_STATUS_CHANGED, data))
    return events


def pipeline_watch_command(snap: MrSnapshot, host: str) -> str | None:
    """The command that watches the head pipeline job by job, for an agent that wants more than its status."""
    if snap.head_pipeline_id is None:
        return None
    url = (
        snap.head_pipeline_web_url or f"https://{host}/{snap.head_pipeline_project}/-/pipelines/{snap.head_pipeline_id}"
    )
    return f"glab-watch pipeline {url}"


def _pipeline_data(snap: MrSnapshot) -> dict[str, Any]:
    data: dict[str, Any] = {"pipeline_id": snap.head_pipeline_id, "web_url": snap.head_pipeline_web_url}
    host = (snap.web_url or "").split("://", 1)[-1].split("/", 1)[0]
    data["watch_command"] = pipeline_watch_command(snap, host)
    return data


def diff_head_pipeline(prev: MrSnapshot, cur: MrSnapshot) -> list[RawEvent]:
    """The head pipeline at the level of the pipeline, never of its jobs: a new head pipeline, its status changes,
    and its end. Each event names the `glab-watch pipeline` command for the job-level detail."""
    if cur.head_pipeline_id is None:
        return []
    events: list[RawEvent] = []
    status = cur.head_pipeline_status
    changed = prev.head_pipeline_id != cur.head_pipeline_id
    if changed:
        data = {**_pipeline_data(cur), "status": status, "previous_pipeline_id": prev.head_pipeline_id}
        events.append(RawEvent("head_pipeline_changed", data))
    elif prev.head_pipeline_status != status:
        data = {**_pipeline_data(cur), "from": prev.head_pipeline_status, "to": status}
        events.append(RawEvent("pipeline_status_changed", data))
    was_final = not changed and prev.head_pipeline_status in FINAL_PIPELINE_STATUSES
    if status in FINAL_PIPELINE_STATUSES and not was_final:
        data = {**_pipeline_data(cur), "status": status}
        if status == "failed" and cur.head_pipeline_failed_jobs:
            data["failed_jobs"] = list(cur.head_pipeline_failed_jobs)
        events.append(RawEvent("pipeline_finished", data))
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
    if prev.target_branch != cur.target_branch and prev.target_branch and cur.target_branch:
        events.append(RawEvent("target_branch_changed", {"from": prev.target_branch, "to": cur.target_branch}))
    if prev.head_sha != cur.head_sha and cur.head_sha:
        events.append(RawEvent("pushed", {"from": prev.head_sha, "to": cur.head_sha}))
    # A merged or closed MR is not mergeable by definition; GitLab still moves its merge status (to not_open),
    # which says nothing the state_changed event does not.
    if prev.merge_status != cur.merge_status and cur.merge_status and cur.state not in TERMINAL_STATES:
        events.append(RawEvent("merge_status_changed", {"from": prev.merge_status, "to": cur.merge_status}))
    approvals_known = prev.approved_by is not None and cur.approved_by is not None
    if approvals_known and (prev.approved_by, prev.approvals_left, prev.approved) != (
        cur.approved_by,
        cur.approvals_left,
        cur.approved,
    ):
        assert prev.approved_by is not None and cur.approved_by is not None
        added, removed = _set_diff(prev.approved_by, cur.approved_by)
        events.append(
            RawEvent(
                "approvals_changed",
                {
                    "approved": cur.approved,
                    "approvals_left": cur.approvals_left,
                    "approved_by": list(cur.approved_by or ()),
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
    for field, kind in (("assignees", "assignees_changed"), ("labels", "labels_changed")):
        old_names, new_names = getattr(prev, field), getattr(cur, field)
        if old_names is not None and new_names is not None and old_names != new_names:
            added, removed = _set_diff(old_names, new_names)
            events.append(RawEvent(kind, {field: list(new_names), "added": added, "removed": removed}))
    if (
        prev.blocking_threads_resolved is not None
        and cur.blocking_threads_resolved is not None
        and prev.blocking_threads_resolved != cur.blocking_threads_resolved
    ):
        events.append(RawEvent("blocking_threads_resolved_changed", {"resolved": cur.blocking_threads_resolved}))
    events.extend(diff_head_pipeline(prev, cur))
    if prev.external_statuses is not None and cur.external_statuses is not None:
        events.extend(diff_external_statuses(prev.external_statuses, cur.external_statuses))
    if prev.threads is not None and cur.threads is not None:
        events.extend(diff_threads(prev.threads, cur.threads))
    return events


def thread_cache_key(ref: ResourceRef, discussion_id: str, sig: str) -> tuple[str, str, str, str]:
    return ("thread", ref.key, discussion_id, sig)


async def prefetch_threads(
    ctx: PollContext, ref: ResourceRef, snapshot: MrSnapshot, ids: set[str]
) -> dict[str, dict[str, Any]]:
    """These threads in their current version, from the engine's cache or fetched into it.

    A thread GitLab no longer returns (deleted, or rewritten for a moment after a force-push) is left out, so a
    client keeps its old file until the snapshot drops the thread.
    """
    sigs = {t.id: thread_sig(t) for t in snapshot.threads or ()}
    found: dict[str, dict[str, Any]] = {}
    missing: set[str] = set()
    for discussion_id in ids:
        sig = sigs.get(discussion_id)
        cached = ctx.threads.get(thread_cache_key(ref, discussion_id, sig)) if sig else None
        if cached is not None:
            found[discussion_id] = cached
        else:
            missing.add(discussion_id)
    if missing:
        fetched = await fetch_discussions(ctx.client, ref, missing)
        for discussion_id, discussion in fetched.items():
            found[discussion_id] = discussion
            sig = sigs.get(discussion_id)
            if sig:
                size = len(json.dumps(discussion, ensure_ascii=False))
                ctx.threads.put(thread_cache_key(ref, discussion_id, sig), discussion, size)
    return found


class MrPoller(Poller[MrSnapshot]):
    kind: ClassVar = "mr"
    snapshot_type: ClassVar = MrSnapshot

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._polls_since_push: int | None = None
        self._polls_since_statuses = 0

    @property
    def _wants_threads(self) -> bool:
        return self.options.wants(*COMMENT_KINDS)

    @property
    def _wants_external_statuses(self) -> bool:
        return self.options.wants(EXTERNAL_STATUS_CHANGED)

    @property
    def _wants_pipeline(self) -> bool:
        # The external statuses need the head pipeline's id, to tell its own jobs' statuses apart.
        return self.options.wants("head_pipeline_changed", EXTERNAL_STATUS_CHANGED, *MR_PIPELINE_KINDS)

    async def _fetch_snapshot(self) -> MrSnapshot:
        return await fetch_snapshot(
            self.ctx.client, self.ref, with_threads=self._wants_threads, with_pipeline=self._wants_pipeline
        )

    def _statuses_due(self, prev: MrSnapshot | None, cur: MrSnapshot) -> bool:
        if prev is None or prev.external_statuses is None:
            return True
        if (prev.head_sha, prev.head_pipeline_id) != (cur.head_sha, cur.head_pipeline_id):
            return True
        # A gate outside the CI often reports when the pipeline ends.
        ended = cur.head_pipeline_status in FINAL_PIPELINE_STATUSES
        if ended and prev.head_pipeline_status != cur.head_pipeline_status:
            return True
        if any(s.status in UNFINISHED_STATUSES for s in prev.external_statuses):
            return True
        return self._polls_since_statuses + 1 >= STATUS_REFRESH_POLLS

    async def _with_external_statuses(self, prev: MrSnapshot | None, cur: MrSnapshot) -> MrSnapshot:
        if not self._wants_external_statuses:
            return cur
        if not self._statuses_due(prev, cur):
            assert prev is not None
            self._polls_since_statuses += 1
            return replace(cur, external_statuses=prev.external_statuses)
        self._polls_since_statuses = 0
        if not cur.head_sha:
            return replace(cur, external_statuses=())
        statuses = await fetch_external_statuses(self.ctx.client, self.ref, cur.head_sha, cur.head_pipeline_id)
        return replace(cur, external_statuses=statuses)

    async def _is_force_push(self, old_sha: str, new_sha: str) -> tuple[bool, str | None]:
        return await is_force_push(self.ctx.client, self.ref.project, old_sha, new_sha)

    async def poll(self) -> list[RawEvent]:
        prev = self.snapshot
        cur = settle(prev, await self._fetch_snapshot())
        cur = await self._with_external_statuses(prev, cur)

        if prev is None:
            self.snapshot = cur
            self._check_terminal()
            return []
        if prev.threads is None and cur.threads is not None:
            # Comment tracking just started: report nothing about threads. Each client fetches the threads its own
            # files lack, through the engine. The baseline above does the same.
            prev = replace(prev, threads=cur.threads)
        if prev.external_statuses is None and cur.external_statuses is not None:
            prev = replace(prev, external_statuses=cur.external_statuses)

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
        events = self._mark_unresolves_after_push(resolved_events)

        if cur.threads is not None:
            changed = {str(e.data["discussion_id"]) for e in events if e.kind in COMMENT_KINDS}
            if changed:
                await prefetch_threads(self.ctx, self.ref, cur, changed)

        self.snapshot = cur
        self._check_terminal()
        return events

    def _mark_unresolves_after_push(self, events: list[RawEvent]) -> list[RawEvent]:
        """GitLab can show a thread unresolved for a poll or two after a force-push rewrote its diff note. Such an
        unresolve is marked `after_push`, not dropped, so a real one is never lost; the reader decides."""
        if any(e.kind == "pushed" for e in events):
            self._polls_since_push = 0
        elif self._polls_since_push is not None:
            self._polls_since_push += 1
            if self._polls_since_push > AFTER_PUSH_POLLS:
                self._polls_since_push = None
        if self._polls_since_push is None:
            return events
        return [replace(e, data={**e.data, "after_push": True}) if e.kind == "thread_unresolved" else e for e in events]

    def _check_terminal(self) -> None:
        if self.snapshot is not None and self.snapshot.state in TERMINAL_STATES:
            self.terminal_reason = self.snapshot.state
            self.terminal_data = {"state": self.snapshot.state}

    def expected_cost(self) -> float:
        """The snapshot query, and the external statuses at least every `STATUS_REFRESH_POLLS` polls."""
        return 1.0 + (1.0 / STATUS_REFRESH_POLLS if self._wants_external_statuses else 0.0)

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
            "approved_by": list(snap.approved_by) if snap.approved_by is not None else None,
            "reviewers": list(snap.reviewers),
        }
        if snap.assignees is not None:
            summary["assignees"] = list(snap.assignees)
        if snap.labels is not None:
            summary["labels"] = list(snap.labels)
        if snap.blocking_threads_resolved is not None:
            summary["blocking_threads_resolved"] = snap.blocking_threads_resolved
        if snap.head_pipeline_id is not None:
            summary["head_pipeline"] = {
                "id": snap.head_pipeline_id,
                "status": snap.head_pipeline_status,
                "web_url": snap.head_pipeline_web_url,
                "watch_command": pipeline_watch_command(snap, self.ref.host),
            }
        if snap.external_statuses is not None:
            summary["external_statuses"] = [
                {"name": s.name, "status": s.status, "target_url": s.target_url} for s in snap.external_statuses
            ]
        if snap.threads is not None:
            summary["threads"] = {
                "total": len(snap.threads),
                "unresolved": sum(1 for t in snap.threads if t.resolvable and not t.resolved),
            }
        return summary


__all__ = [
    "ExternalStatus",
    "MrPoller",
    "MrSnapshot",
    "NoteState",
    "ThreadState",
    "diff_mr",
    "parse_mr",
    "parse_threads",
    "settle",
]
