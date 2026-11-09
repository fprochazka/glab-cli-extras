"""Watching every merge request of a project through the MR list, ordered by `updated_at`.

Each poll asks for MRs updated after the watermark minus an overlap of two minutes. The overlap covers clock skew,
asynchronous system notes, and GitLab's throttled touch (it skips touching an MR within a minute of the last touch).
Because of that throttling, `mr_updated` is best effort: a second change within a minute of the first shows up only
with the next change after it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from glab_cli_extras.events import RawEvent
from glab_cli_extras.gitlab.client import encode_project
from glab_cli_extras.resources.base import Poller

OVERLAP = timedelta(minutes=2)
INTERVAL = 60.0
MAX_PAGES = 5


@dataclass(frozen=True, slots=True)
class MrEntry:
    iid: int
    state: str
    updated_at: str
    fields: dict[str, Any] = field(default_factory=dict)
    """The TRACKED_FIELDS values from the last list response, to tell what an update changed."""


@dataclass(frozen=True, slots=True)
class MrListSnapshot:
    project: str
    started_at: str
    watermark: str
    mrs: tuple[MrEntry, ...] = ()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


TRACKED_FIELDS = (
    "title",
    "draft",
    "target_branch",
    "sha",
    "detailed_merge_status",
    "labels",
    "assignees",
    "reviewers",
)
"""Fields of the list response compared between polls. They come with every list response, so this costs nothing."""
TRANSIENT_MERGE_STATUSES = frozenset({"checking", "unchecked", "preparing"})


def _usernames(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return sorted(str(u.get("username")) for u in value if isinstance(u, dict) and u.get("username"))


def tracked_fields(mr: dict[str, Any]) -> dict[str, Any]:
    """The comparable values of the TRACKED_FIELDS present in one list item."""
    values: dict[str, Any] = {}
    for name in ("title", "draft", "target_branch", "sha", "detailed_merge_status"):
        if name in mr:
            values[name] = mr[name]
    if isinstance(mr.get("labels"), list):
        values["labels"] = sorted(
            str(label.get("name", label)) if isinstance(label, dict) else str(label) for label in mr["labels"]
        )
    for name in ("assignees", "reviewers"):
        users = _usernames(mr.get(name))
        if users is not None:
            values[name] = users
    return values


def changed_fields(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[Any]]:
    """`{field: [old, new]}` for the tracked fields both sides know and that differ."""
    changes = {}
    for name in TRACKED_FIELDS:
        if name in old and name in new and old[name] != new[name]:
            if name == "detailed_merge_status" and new[name] in TRANSIENT_MERGE_STATUSES:
                continue
            changes[name] = [old[name], new[name]]
    return changes


def _settled(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Keep the last settled merge status while GitLab reports a transient one."""
    if new.get("detailed_merge_status") in TRANSIENT_MERGE_STATUSES and "detailed_merge_status" in old:
        return {**new, "detailed_merge_status": old["detailed_merge_status"]}
    return new


def _event_data(mr: dict[str, Any]) -> dict[str, Any]:
    return {
        "iid": mr.get("iid"),
        "title": mr.get("title"),
        "state": mr.get("state"),
        "author": (mr.get("author") or {}).get("username"),
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "draft": mr.get("draft"),
        "web_url": mr.get("web_url"),
        "updated_at": mr.get("updated_at"),
    }


def diff_mr_list(prev: MrListSnapshot, items: list[dict[str, Any]]) -> tuple[MrListSnapshot, list[RawEvent]]:
    """Fold one page of the MR list into the snapshot and return the events it implies."""
    known = {m.iid: m for m in prev.mrs}
    started = _parse(prev.started_at)
    watermark = _parse(prev.watermark)
    events: list[RawEvent] = []
    for mr in sorted(items, key=lambda m: str(m.get("updated_at") or "")):
        iid = int(mr["iid"])
        state = str(mr.get("state") or "")
        updated_at = str(mr.get("updated_at") or "")
        old = known.get(iid)
        values = _settled(old.fields, tracked_fields(mr)) if old is not None else tracked_fields(mr)
        changes = changed_fields(old.fields, values) if old is not None else {}
        kind: str | None = None
        if old is None:
            created_at = mr.get("created_at")
            if updated_at and _parse(updated_at) < started:
                # Seen through the overlap window, but last changed before the watch began: nothing new.
                kind = None
            elif created_at and _parse(str(created_at)) >= started:
                kind = "mr_opened"
            elif state == "merged":
                kind = "mr_merged"
            elif state == "closed":
                kind = "mr_closed"
            else:
                kind = "mr_updated"
        elif old.state != state:
            if state == "merged":
                kind = "mr_merged"
            elif state == "closed":
                kind = "mr_closed"
            elif state == "opened" and old.state == "closed":
                kind = "mr_reopened"
            else:
                kind = "mr_updated"
        elif updated_at > old.updated_at or changes:
            kind = "mr_updated"
        if kind is not None:
            data = _event_data(mr)
            if changes:
                data["changes"] = changes
            events.append(RawEvent(kind, data))
        known[iid] = MrEntry(iid, state, updated_at, values)
        if updated_at:
            watermark = max(watermark, _parse(updated_at))
    snapshot = replace(prev, watermark=_iso(watermark), mrs=tuple(sorted(known.values(), key=lambda m: m.iid)))
    return snapshot, events


class MrListPoller(Poller[MrListSnapshot]):
    kind: ClassVar = "mrs"
    snapshot_type: ClassVar = MrListSnapshot

    async def _fetch(self, since: datetime) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            batch = await self.ctx.client.get_json(
                f"projects/{encode_project(self.ref.project)}/merge_requests",
                {
                    "state": "all",
                    "order_by": "updated_at",
                    "sort": "desc",
                    "updated_after": _iso(since),
                    "per_page": 100,
                    "page": page,
                },
                kind="merge_requests",
            )
            if not isinstance(batch, list):
                break
            items.extend(m for m in batch if isinstance(m, dict))
            if len(batch) < 100:
                break
        return items

    async def poll(self) -> list[RawEvent]:
        now = datetime.fromtimestamp(self.ctx.clock(), UTC)
        if self.snapshot is None:
            start = now - OVERLAP
            baseline = MrListSnapshot(self.ref.project, _iso(now), _iso(start))
            self.snapshot, _ = diff_mr_list(baseline, await self._fetch(start))
            return []
        items = await self._fetch(_parse(self.snapshot.watermark) - OVERLAP)
        self.snapshot, events = diff_mr_list(self.snapshot, items)
        return events

    def next_delay(self) -> float:
        return INTERVAL

    def summary(self) -> dict[str, Any]:
        snap = self.snapshot
        if snap is None:
            return {}
        return {"project": snap.project, "watermark": snap.watermark, "recently_updated": len(snap.mrs)}
