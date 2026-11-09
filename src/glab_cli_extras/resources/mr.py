"""Merge request watching: one GraphQL snapshot per poll, follow-up requests only for what moved.

- A moved `diffHeadSha` is a push. One `merge_base` request tells a fast-forward from a force-push.
- A thread whose notes changed (`updatedAt` moves on edit, resolve and unresolve) is re-fetched alone into the engine's
  cache, for the clients to write. Many changed threads at once are fetched with one list request instead.
- The head pipeline is a separate, shared `pipeline` resource; the daemon relays its events into the MR's stream.

The MR's own `updated_at` is not used: GitLab skips touching it within a minute of the last touch, and head pipeline
changes and single resolves never touch it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, ClassVar

from glab_cli_extras.events import GROUPS, PIPELINE_KINDS, RawEvent
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources.base import PollContext, Poller, PollerOptions, is_force_push
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

log = logging.getLogger(__name__)

TRANSIENT_MERGE_STATUSES = frozenset({"checking", "unchecked", "preparing"})
"""GitLab reports these while it recomputes mergeability; the last settled value stays until a new one arrives."""
TERMINAL_STATES = frozenset({"merged", "closed"})
INTERVAL = 30.0
INTERVAL_PREMIUM = 15.0

COMMENT_KINDS = GROUPS["comments"]


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

    @property
    def _wants_threads(self) -> bool:
        return self.options.wants(*COMMENT_KINDS)

    @property
    def _wants_pipeline(self) -> bool:
        return self.options.wants("head_pipeline_changed", *PIPELINE_KINDS)

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
        events = resolved_events

        if cur.threads is not None:
            changed = {str(e.data["discussion_id"]) for e in events if e.kind in COMMENT_KINDS}
            if changed:
                await prefetch_threads(self.ctx, self.ref, cur, changed)

        self.snapshot = cur
        self._check_terminal()
        return events

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


__all__ = ["MrPoller", "MrSnapshot", "NoteState", "ThreadState", "diff_mr", "parse_mr", "parse_threads", "settle"]
