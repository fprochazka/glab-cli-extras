"""Merge request watching: one GraphQL snapshot per poll, follow-up requests only for what moved.

- A moved `diffHeadSha` is a push. One `merge_base` request tells a fast-forward from a force-push.
- A thread whose notes changed (`updatedAt` moves on edit, resolve and unresolve) is re-fetched alone into the engine's
  cache, for the clients to write. Many changed threads at once are fetched with one list request instead.
- The head pipeline is reported at the level of the pipeline only, from the same snapshot: its id and status cost no
  extra request. Job-level detail is what `glab-watch pipeline` is for, and every pipeline event names that command.

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
    MrSnapshot,
    NoteState,
    ThreadState,
    fetch_discussions,
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

COMMENT_KINDS = GROUPS["comments"]


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
    if prev.head_sha != cur.head_sha and cur.head_sha:
        events.append(RawEvent("pushed", {"from": prev.head_sha, "to": cur.head_sha}))
    if prev.merge_status != cur.merge_status and cur.merge_status:
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
    events.extend(diff_head_pipeline(prev, cur))
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

    @property
    def _wants_threads(self) -> bool:
        return self.options.wants(*COMMENT_KINDS)

    @property
    def _wants_pipeline(self) -> bool:
        return self.options.wants("head_pipeline_changed", *MR_PIPELINE_KINDS)

    async def _fetch_snapshot(self) -> MrSnapshot:
        return await fetch_snapshot(
            self.ctx.client, self.ref, with_threads=self._wants_threads, with_pipeline=self._wants_pipeline
        )

    async def _is_force_push(self, old_sha: str, new_sha: str) -> tuple[bool, str | None]:
        return await is_force_push(self.ctx.client, self.ref.project, old_sha, new_sha)

    async def poll(self) -> list[RawEvent]:
        prev = self.snapshot
        cur = settle(prev, await self._fetch_snapshot())

        if prev is None or (prev.threads is None and cur.threads is not None):
            # Baseline, or comment tracking just started: report nothing about threads. Each client fetches the
            # threads its own files lack, through the engine.
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
        if snap.head_pipeline_id is not None:
            summary["head_pipeline"] = {
                "id": snap.head_pipeline_id,
                "status": snap.head_pipeline_status,
                "web_url": snap.head_pipeline_web_url,
                "watch_command": pipeline_watch_command(snap, self.ref.host),
            }
        if snap.threads is not None:
            summary["threads"] = {
                "total": len(snap.threads),
                "unresolved": sum(1 for t in snap.threads if t.resolvable and not t.resolved),
            }
        return summary


__all__ = ["MrPoller", "MrSnapshot", "NoteState", "ThreadState", "diff_mr", "parse_mr", "parse_threads", "settle"]
