"""Watching the user's merge request dashboard: the MRs that enter or leave its lists, review states, merges.

Each poll reads the whole open set again (`dashboard.query`), one request for up to 50 MRs per side. A watermark
like the `mrs` watch's cannot work here: a reviewer's review state changes without touching the MR's `updated_at`.
An MR that left the open set is looked up once more, in one request for all of them, to tell a merge or a close
from an MR that only left the user's lists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, ClassVar

from glab_cli_extras.dashboard.lists import (
    DUO_BOT,
    LISTS_BY_LAYOUT,
    MERGED_LIST_BY_SIDE,
    SIDES,
    classify,
    effective_layout,
)
from glab_cli_extras.dashboard.model import DashboardMr, Lists, rest_shape
from glab_cli_extras.dashboard.query import fetch_merged, fetch_open, fetch_states
from glab_cli_extras.dashboard.view import Dashboard, Entry
from glab_cli_extras.events import RawEvent
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources.base import Poller
from glab_cli_extras.resources.mr_list import TRANSIENT_MERGE_STATUSES, _event_data, changed_fields, tracked_fields

INTERVAL = 900.0
MIN_INTERVAL = 60.0


@dataclass(frozen=True, slots=True)
class MyMrsSnapshot:
    host: str
    started_at: str
    username: str
    layout: str
    show_drafts: bool
    duo_bot: bool = True
    entries: tuple[Entry, ...] = ()
    merged_counts: dict[str, int] = field(default_factory=dict)
    """The Merged tab's counts when the watch started; not kept up to date."""

    def dashboard(self) -> Dashboard:
        return Dashboard(self.host, self.username, self.layout, self.show_drafts, self.entries, self.duo_bot)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def tracked(mr: DashboardMr) -> dict[str, Any]:
    """The fields an `mr_updated` reports changes of: the `mrs` watch's, and the head pipeline's status."""
    return {**tracked_fields(rest_shape(mr)), "pipeline_status": mr.pipeline_status}


def changes_between(old: DashboardMr, new: DashboardMr) -> dict[str, list[Any]]:
    before, after = tracked(old), tracked(new)
    changes = changed_fields(before, after)
    if before["pipeline_status"] != after["pipeline_status"]:
        changes["pipeline_status"] = [before["pipeline_status"], after["pipeline_status"]]
    return changes


def settled(old: DashboardMr | None, new: DashboardMr) -> DashboardMr:
    """Keep the last settled merge status while GitLab reports a transient one."""
    if old is not None and new.detailed_merge_status in TRANSIENT_MERGE_STATUSES and old.detailed_merge_status:
        return replace(new, detailed_merge_status=old.detailed_merge_status)
    return new


def event_data(mr: DashboardMr, lists: Lists) -> dict[str, Any]:
    """What every event about an MR carries: the `mrs` watch's data, the reference and project, and its lists."""
    return {"reference": mr.reference, "project": mr.project, **_event_data(rest_shape(mr)), "lists": lists.to_dict()}


def _leave_reason(side: str, still_member: bool) -> str:
    if still_member:
        # Still the user's MR, but in no list of this side, such as a draft with "show drafts" off.
        return "hidden"
    return "unassigned" if side == "author" else "review_request_removed"


def _member(mr: DashboardMr, side: str) -> bool:
    return mr.mine if side == "author" else mr.review_requested


def classify_all(mrs: Sequence[DashboardMr], username: str, layout: str, show_drafts: bool) -> tuple[Entry, ...]:
    return tuple(Entry(mr, classify(mr, username, layout, show_drafts)) for mr in mrs)


def diff_my_mrs(
    prev: MyMrsSnapshot, current: MyMrsSnapshot, departed: Mapping[str, tuple[str, str | None]] | None
) -> tuple[MyMrsSnapshot, list[RawEvent]]:
    """The events between two snapshots of the open set.

    `departed` maps the gid of each MR that left the open set to its state and merge time now; an MR missing from it
    is gone (deleted or no longer visible). With `departed=None` the MRs that left are not reported, because nobody
    asked GitLab what became of them.
    """
    known = {e.mr.reference: e for e in prev.entries}
    started = _parse(prev.started_at)
    events: list[RawEvent] = []
    entries: list[Entry] = []
    for entry in current.entries:
        old = known.pop(entry.mr.reference, None)
        mr = settled(old.mr if old is not None else None, entry.mr)
        entry = replace(entry, mr=mr)
        entries.append(entry)
        if old is None:
            if mr.created_at and _parse(mr.created_at) >= started:
                events.append(RawEvent("mr_opened", event_data(mr, entry.lists)))
                continue
            for side in SIDES:
                to = getattr(entry.lists, side)
                if to is not None:
                    data = event_data(mr, entry.lists) | {"side": side, "from": None, "to": to}
                    events.append(RawEvent("list_changed", data))
            continue
        old_states = {r.username: r for r in old.mr.reviewers}
        for reviewer in mr.reviewers:
            before = old_states.get(reviewer.username)
            if before is not None and before.state != reviewer.state:
                data = event_data(mr, entry.lists) | {
                    "reviewer": reviewer.username,
                    "from": before.state.lower() if before.state else None,
                    "to": reviewer.state.lower() if reviewer.state else None,
                    "at": reviewer.updated_at,
                }
                events.append(RawEvent("review_state_changed", data))
        for side in SIDES:
            before_list, after_list = getattr(old.lists, side), getattr(entry.lists, side)
            if before_list != after_list:
                data = event_data(mr, entry.lists) | {"side": side, "from": before_list, "to": after_list}
                if after_list is None:
                    data["reason"] = _leave_reason(side, _member(mr, side))
                events.append(RawEvent("list_changed", data))
        changes = changes_between(old.mr, mr)
        if mr.updated_at > old.mr.updated_at or changes:
            data = event_data(mr, entry.lists)
            if changes:
                data["changes"] = changes
            events.append(RawEvent("mr_updated", data))
    if departed is not None:
        for old in known.values():
            events.extend(_departure(old, departed.get(old.mr.gid)))
    else:
        # Not asked what became of them: keep them, so the next real poll reports them.
        entries.extend(known.values())
    return replace(current, started_at=prev.started_at, entries=tuple(entries)), events


def _departure(old: Entry, now: tuple[str, str | None] | None) -> list[RawEvent]:
    mr = old.mr
    state, merged_at = now if now is not None else ("gone", None)
    if state == "merged":
        merged_list = MERGED_LIST_BY_SIDE["author"] if mr.mine else MERGED_LIST_BY_SIDE["reviewer"]
        data = event_data(replace(mr, state="merged"), old.lists) | {"merged_at": merged_at, "merged_list": merged_list}
        return [RawEvent("mr_merged", data)]
    if state == "closed":
        return [RawEvent("mr_closed", event_data(replace(mr, state="closed"), old.lists))]
    events = []
    for side in SIDES:
        before = getattr(old.lists, side)
        if before is not None:
            reason = "gone" if state == "gone" else _leave_reason(side, False)
            data = event_data(mr, old.lists) | {"side": side, "from": before, "to": None, "reason": reason}
            events.append(RawEvent("list_changed", data))
    return events


def summarize_snapshot(snap: MyMrsSnapshot) -> dict[str, Any]:
    counts = {d.id: 0 for d in LISTS_BY_LAYOUT[snap.layout] if not d.merged}
    for entry in snap.entries:
        for list_id in entry.lists.ids():
            if list_id in counts:
                counts[list_id] += 1
    summary: dict[str, Any] = {
        "username": snap.username,
        "layout": snap.layout,
        "show_drafts": snap.show_drafts,
        "lists": counts,
        "merged_2_weeks": dict(snap.merged_counts),
    }
    if not snap.duo_bot:
        summary["warning"] = f"no user {DUO_BOT} on this host, so no reviewer is ignored as the Duo bot"
    return summary


class MyMrsPoller(Poller[MyMrsSnapshot]):
    kind: ClassVar = "my-mrs"
    snapshot_type: ClassVar = MyMrsSnapshot

    _requests: float = 1.0

    async def _read(self) -> MyMrsSnapshot:
        read = await fetch_open(self.ctx.client)
        layout = effective_layout(self.ref.layout, read.list_type)
        self._requests = float(read.requests)
        now = _iso(datetime.fromtimestamp(self.ctx.clock(), UTC))
        return MyMrsSnapshot(
            host=self.ref.host,
            started_at=now,
            username=read.username,
            layout=layout,
            show_drafts=read.show_drafts,
            duo_bot=read.duo_bot,
            entries=classify_all(read.mrs, read.username, layout, read.show_drafts),
        )

    async def poll(self) -> list[RawEvent]:
        current = await self._read()
        if self.snapshot is None:
            merged, _, _ = await fetch_merged(self.ctx.client, datetime.fromtimestamp(self.ctx.clock(), UTC))
            counts = dict.fromkeys(MERGED_LIST_BY_SIDE.values(), 0)
            for mr in merged:
                if mr.mine:
                    counts[MERGED_LIST_BY_SIDE["author"]] += 1
                if mr.review_requested:
                    counts[MERGED_LIST_BY_SIDE["reviewer"]] += 1
            self.snapshot = replace(current, merged_counts=counts)
            return []
        now_refs = {e.mr.reference for e in current.entries}
        gone = [e.mr.gid for e in self.snapshot.entries if e.mr.reference not in now_refs]
        departed = await fetch_states(self.ctx.client, gone) if gone else {}
        if gone:
            self._requests += 1
        snapshot, events = diff_my_mrs(self.snapshot, current, departed)
        self.snapshot = replace(snapshot, merged_counts=self.snapshot.merged_counts)
        return events

    def next_delay(self) -> float:
        return max(MIN_INTERVAL, self.options.interval or INTERVAL)

    def expected_cost(self) -> float:
        return self._requests

    @classmethod
    def summarize(cls, ref: ResourceRef, snap: MyMrsSnapshot) -> dict[str, Any]:
        return summarize_snapshot(snap)
