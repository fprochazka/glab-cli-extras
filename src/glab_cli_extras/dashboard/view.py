"""The dashboard as `glab-my-mrs` prints it and `glab-watch my-mrs` stores it in `state.json`: the same JSON shape in
both, and a text form for the terminal."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from glab_cli_extras.dashboard.lists import ListDef, classify, effective_layout, merged_lists
from glab_cli_extras.dashboard.model import DashboardMr, Lists
from glab_cli_extras.dashboard.query import DashboardRead


@dataclass(frozen=True, slots=True)
class Entry:
    mr: DashboardMr
    lists: Lists


@dataclass(frozen=True, slots=True)
class Dashboard:
    host: str
    username: str
    layout: str
    show_drafts: bool
    entries: tuple[Entry, ...]
    """The open MRs, and the merged ones when the Merged tab was read."""
    duo_bot: bool = True
    truncated: bool = False


def build(host: str, read: DashboardRead, layout_option: str | None, merged: Sequence[DashboardMr] = ()) -> Dashboard:
    """Classify a read into the lists of the layout in use."""
    layout = effective_layout(layout_option, read.list_type)
    entries = [Entry(mr, classify(mr, read.username, layout, read.show_drafts)) for mr in read.mrs]
    entries += [Entry(mr, merged_lists(mr)) for mr in merged]
    return Dashboard(host, read.username, layout, read.show_drafts, tuple(entries), read.duo_bot, read.truncated)


def mr_dict(mr: DashboardMr, lists: Lists) -> dict[str, Any]:
    """One MR of a list. The first keys are the ones every `glab-watch my-mrs` event carries."""
    data: dict[str, Any] = {
        "reference": mr.reference,
        "iid": mr.iid,
        "project": mr.project,
        "title": mr.title,
        "web_url": mr.web_url,
        "author": mr.author,
        "draft": mr.draft,
        "lists": lists.to_dict(),
        "reviewers": [{"username": r.username, "state": r.state.lower() if r.state else None} for r in mr.reviewers],
        "pipeline_status": mr.pipeline_status,
        "detailed_merge_status": mr.detailed_merge_status,
        "updated_at": mr.updated_at,
    }
    if mr.merged_at:
        data["merged_at"] = mr.merged_at
    return data


def members(dashboard: Dashboard, list_def: ListDef) -> list[Entry]:
    """The MRs of one list, newest first, as the dashboard orders them."""
    found = [e for e in dashboard.entries if getattr(e.lists, list_def.side) == list_def.id]
    key = (lambda e: e.mr.merged_at or "") if list_def.merged else (lambda e: e.mr.updated_at)
    return sorted(found, key=key, reverse=True)


def to_json(dashboard: Dashboard, lists: Sequence[ListDef]) -> dict[str, Any]:
    return {
        "host": dashboard.host,
        "username": dashboard.username,
        "layout": dashboard.layout,
        "show_drafts": dashboard.show_drafts,
        "lists": [
            {
                "id": d.id,
                "side": d.side,
                "title": d.title,
                "count": len(entries),
                "merge_requests": [mr_dict(e.mr, e.lists) for e in entries],
            }
            for d in lists
            for entries in [members(dashboard, d)]
        ],
    }


def _reviewers(mr: DashboardMr) -> str:
    if not mr.reviewers:
        return "no reviewers"
    return "reviewers " + ", ".join(
        f"{r.username} {(r.state or 'unknown').lower().replace('_', ' ')}" for r in mr.reviewers
    )


def mr_line(mr: DashboardMr) -> str:
    parts = [mr.reference]
    if mr.draft:
        parts.append("draft")
    parts.append(f"by {mr.author or '?'}")
    parts.append(f"pipeline {mr.pipeline_status or 'none'}")
    parts.append(_reviewers(mr))
    parts.append(f"merged {mr.merged_at}" if mr.merged_at else f"updated {mr.updated_at}")
    parts.append(mr.title)
    return " · ".join(parts)


def to_text(dashboard: Dashboard, lists: Sequence[ListDef]) -> str:
    drafts = "drafts shown" if dashboard.show_drafts else "drafts hidden"
    lines = [f"{dashboard.host} · {dashboard.username} · {dashboard.layout} · {drafts}"]
    for d in lists:
        entries = members(dashboard, d)
        lines.append(f"== {d.id} ({d.side}) · {len(entries)} · {d.title}")
        for entry in entries:
            lines.append(f"  {mr_line(entry.mr)}")
            lines.append(f"    {entry.mr.web_url}")
    return "\n".join(lines) + "\n"
