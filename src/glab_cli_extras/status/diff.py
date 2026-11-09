"""The one-shot MR read's snapshot, its diff and its text: what `glab-mr-status` prints, with no I/O.

The snapshot is the REST merge request reduced to the fields the diff reads, plus the newest note and the approvals.
The REST object, not the GraphQL snapshot the watcher uses, because only REST has the behind count, the labels, the
assignees, `user_notes_count` and `blocking_discussions_resolved`.

The ids and the line formats are a contract: the mr-status skill and the commands that call it read them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeGuard

APPROVALS_UNAVAILABLE: dict[str, Any] = {"unavailable": True}
"""Approvals the host would not hand over. Kept in the snapshot so the next run can tell "nobody has approved" from
"we never found out", and does not ask a refused endpoint again while the MR stands still."""

TRANSIENT_MERGE_STATUS = frozenset({"checking", "unchecked"})
TRANSIENT_DETAILED_MERGE_STATUS = frozenset({"checking", "unchecked", "preparing"})
"""Values GitLab reports while it recomputes. They are not a change; the last settled value stays until a settled
value replaces it."""


@dataclass(frozen=True)
class Change:
    id: str
    detail: str

    def line(self) -> str:
        return f"{self.id}: {self.detail}"


def usernames(items: Any) -> list[str]:
    return sorted(str(i.get("username", "")) for i in (items or []) if isinstance(i, dict))


def snapshot_from_mr(mr: dict[str, Any], host: str, project_path: str) -> dict[str, Any]:
    """Reduce a merge request API object to the fields the diff reads."""
    hp = mr.get("head_pipeline") or {}
    return {
        "host": host,
        "project_path": project_path,
        "iid": mr.get("iid"),
        "web_url": mr.get("web_url"),
        "title": mr.get("title"),
        "author": (mr.get("author") or {}).get("username"),
        "state": mr.get("state"),
        "draft": bool(mr.get("draft", mr.get("work_in_progress", False))),
        "sha": mr.get("sha"),
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "diverged_commits_count": mr.get("diverged_commits_count"),
        "rebase_in_progress": bool(mr.get("rebase_in_progress", False)),
        "merge_status": mr.get("merge_status"),
        "detailed_merge_status": mr.get("detailed_merge_status"),
        "has_conflicts": bool(mr.get("has_conflicts", False)),
        "blocking_discussions_resolved": mr.get("blocking_discussions_resolved"),
        "assignees": usernames(mr.get("assignees")),
        "reviewers": usernames(mr.get("reviewers")),
        "labels": sorted(mr.get("labels") or []),
        "user_notes_count": mr.get("user_notes_count"),
        "updated_at": mr.get("updated_at"),
        "head_pipeline": {
            "id": hp.get("id"),
            "project_id": hp.get("project_id"),
            "status": hp.get("status"),
            "sha": hp.get("sha"),
            "web_url": hp.get("web_url"),
            "created_at": hp.get("created_at"),
            "updated_at": hp.get("updated_at"),
        }
        if hp
        else None,
        "newest_note": None,
        "notes_probe": None,
        "approvals": None,
    }


def note_from_api(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": note.get("id"),
        "updated_at": note.get("updated_at"),
        "created_at": note.get("created_at"),
        "author": (note.get("author") or {}).get("username"),
        "system": bool(note.get("system", False)),
        "resolved": note.get("resolved"),
    }


def approvals_from_api(approvals: dict[str, Any]) -> dict[str, Any]:
    return {
        "approved_by": sorted(
            ((x.get("user") or {}).get("username") or "") for x in (approvals.get("approved_by") or [])
        ),
        "approvals_left": approvals.get("approvals_left"),
        "approved": approvals.get("approved"),
    }


def settle(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Keep the last settled merge status while GitLab reports a transient one."""
    if old is None:
        return new
    settled = dict(new)
    if new.get("merge_status") in TRANSIENT_MERGE_STATUS:
        settled["merge_status"] = old.get("merge_status")
    if new.get("detailed_merge_status") in TRANSIENT_DETAILED_MERGE_STATUS:
        settled["detailed_merge_status"] = old.get("detailed_merge_status")
    if new.get("rebase_in_progress"):
        # A rebase in flight also reports divergence and conflicts mid-way; hold both.
        settled["diverged_commits_count"] = old.get("diverged_commits_count")
        settled["has_conflicts"] = old.get("has_conflicts")
    return settled


def short(sha: Any) -> str:
    return str(sha)[:8] if sha else "-"


def approvals_known(approvals: Any) -> TypeGuard[dict[str, Any]]:
    """False for approvals never read and for approvals the host refused."""
    return isinstance(approvals, dict) and not approvals.get("unavailable")


def is_older(a: Any, b: Any) -> bool:
    """True when timestamp a is strictly earlier than b. Unparsable input is never older."""
    if not a or not b:
        return False
    try:
        return datetime.fromisoformat(str(a).replace("Z", "+00:00")) < datetime.fromisoformat(
            str(b).replace("Z", "+00:00")
        )
    except ValueError:
        return False


def diff_snapshots(old: dict[str, Any] | None, new: dict[str, Any]) -> list[Change]:
    """Every change between two snapshots of one MR, in a stable order."""
    if old is None:
        return []
    new = settle(old, new)
    changes: list[Change] = []

    def changed(key: str) -> bool:
        return bool(old.get(key) != new.get(key))

    if changed("state"):
        changes.append(Change("STATE_CHANGED", f"{old.get('state')} -> {new.get('state')}"))
    if changed("draft"):
        changes.append(Change("DRAFT_CHANGED", "draft" if new["draft"] else "ready"))
    if changed("sha"):
        divergence = new.get("diverged_commits_count")
        rebased = divergence == 0 and (old.get("diverged_commits_count") or 0) > 0
        hint = "rebase likely: behind count dropped to 0" if rebased else "new head"
        # Full SHAs: a caller pins a review to the SHA this line names.
        changes.append(Change("HEAD_CHANGED", f"{old.get('sha')} -> {new.get('sha')} ({hint})"))
    if changed("target_branch"):
        changes.append(Change("TARGET_CHANGED", f"{old.get('target_branch')} -> {new.get('target_branch')}"))
    if changed("title"):
        changes.append(Change("TITLE_CHANGED", f"{old.get('title')!r} -> {new.get('title')!r}"))

    op, np_ = old.get("head_pipeline") or {}, new.get("head_pipeline") or {}
    if (op.get("id"), op.get("status")) != (np_.get("id"), np_.get("status")):
        if op.get("id") != np_.get("id"):
            detail = (
                f"new pipeline {np_.get('id')} ({np_.get('status')}) on {short(np_.get('sha'))},"
                f" was {op.get('id')} ({op.get('status')})"
            )
        else:
            detail = f"pipeline {np_.get('id')}: {op.get('status')} -> {np_.get('status')}"
        if new.get("diverged_commits_count"):
            detail += (
                f"; branch is {new['diverged_commits_count']} behind {new.get('target_branch')},"
                " so this ran on a superseded base"
            )
        changes.append(Change("PIPELINE_CHANGED", detail))

    if changed("diverged_commits_count"):
        changes.append(
            Change(
                "BEHIND_CHANGED",
                f"behind {new.get('target_branch')} by {old.get('diverged_commits_count')}"
                f" -> {new.get('diverged_commits_count')}",
            )
        )
    if changed("merge_status") or changed("detailed_merge_status") or changed("has_conflicts"):

        def merge(s: dict[str, Any]) -> str:
            return f"{s.get('merge_status')}/{s.get('detailed_merge_status')}/conflicts={s.get('has_conflicts')}"

        changes.append(Change("MERGE_STATUS_CHANGED", f"{merge(old)} -> {merge(new)}"))
    if changed("blocking_discussions_resolved"):
        changes.append(
            Change(
                "DISCUSSIONS_RESOLVED_CHANGED",
                f"all blocking discussions resolved: {old.get('blocking_discussions_resolved')}"
                f" -> {new.get('blocking_discussions_resolved')}",
            )
        )
    for key, change_id in (
        ("reviewers", "REVIEWERS_CHANGED"),
        ("assignees", "ASSIGNEES_CHANGED"),
        ("labels", "LABELS_CHANGED"),
    ):
        if changed(key):
            added = sorted(set(new.get(key) or []) - set(old.get(key) or []))
            removed = sorted(set(old.get(key) or []) - set(new.get(key) or []))
            changes.append(Change(change_id, f"added {added or '-'}, removed {removed or '-'}"))
    oa, na = old.get("approvals"), new.get("approvals")
    if (
        approvals_known(oa)
        and approvals_known(na)
        and (oa.get("approved_by"), oa.get("approvals_left")) != (na.get("approved_by"), na.get("approvals_left"))
    ):
        added = sorted(set(na.get("approved_by") or []) - set(oa.get("approved_by") or []))
        removed = sorted(set(oa.get("approved_by") or []) - set(na.get("approved_by") or []))
        changes.append(
            Change(
                "APPROVALS_CHANGED",
                f"approved by +{added or '-'} -{removed or '-'}; approvals left {na.get('approvals_left')}",
            )
        )

    # Notes come last on purpose. The count-only probe blames a moved updated_at on note traffic only when nothing
    # else in this run explains it, and "nothing else" has to mean every field compared above: a reviewer assignment
    # bumps updated_at too.
    changes.extend(note_changes(old, new, explained=bool(changes)))
    return changes


def note_changes(old: dict[str, Any], new: dict[str, Any], *, explained: bool) -> list[Change]:
    """What the notes moved, as far as this run's probe mode can tell."""
    on, nn = old.get("newest_note") or {}, new.get("newest_note") or {}
    old_count, new_count = old.get("user_notes_count"), new.get("user_notes_count")
    if new.get("notes_probe") == "exact":
        if (on.get("id"), on.get("updated_at")) != (nn.get("id"), nn.get("updated_at")):
            if on.get("id") != nn.get("id"):
                # The newest note by updated_at went back in time: the one seen last run is gone and an older note
                # took its place. Nothing was posted.
                older = is_older(nn.get("updated_at"), on.get("updated_at"))
                kind = "newest note deleted, now the newest is a note" if older else "new note"
            else:
                kind = "note edited or resolved"
            system = " (system note)" if nn.get("system") else ""
            return [Change("NOTES_CHANGED", f"{kind} by @{nn.get('author')}{system} at {nn.get('updated_at')}")]
        if old_count != new_count:
            # The newest note did not move, yet the count did: a note further down the list came or went, which the
            # exact probe alone cannot see.
            return [
                Change(
                    "NOTES_CHANGED",
                    f"user note count {old_count} -> {new_count} while the newest note stayed the same"
                    " (a note elsewhere in the list came or went)",
                )
            ]
        return []
    if old_count != new_count:
        return [Change("NOTES_CHANGED", f"user note count {old_count} -> {new_count} (count-only probe)")]
    if old.get("updated_at") != new.get("updated_at") and not explained:
        return [
            Change(
                "NOTES_CHANGED",
                f"updated_at moved to {new.get('updated_at')} with no other field explaining it"
                " (count-only probe; may be a note edit, a resolve, or bot traffic)",
            )
        ]
    return []


def scorecard(snap: dict[str, Any]) -> str:
    hp = snap.get("head_pipeline") or {}
    behind = snap.get("diverged_commits_count")
    stacked = ""
    if snap.get("default_branch") and snap.get("target_branch") and snap["target_branch"] != snap["default_branch"]:
        stacked = f" · stacked on {snap['target_branch']}"
    approvals = snap.get("approvals")
    approved_by = (", ".join(approvals.get("approved_by") or []) or "-") if approvals_known(approvals) else "?"
    older = "" if hp.get("sha") in (None, snap.get("sha")) else " (on an older head)"
    return (
        f"{snap['host']}/{snap['project_path']}!{snap['iid']} · {snap.get('state')}"
        f" · {'draft' if snap.get('draft') else 'ready'}"
        f" · head {short(snap.get('sha'))} · pipeline {hp.get('status') or 'none'}{older}"
        f" · behind {behind if behind is not None else '?'}{stacked}"
        f" · conflicts {'yes' if snap.get('has_conflicts') else 'no'}"
        f" · notes {snap.get('user_notes_count')}"
        f" · approved by {approved_by} · updated {snap.get('updated_at')}"
    )


def snapshot_lines(snap: dict[str, Any]) -> list[str]:
    lines = [
        f"  title: {snap.get('title')}",
        f"  author: @{snap.get('author')} · source {snap.get('source_branch')} -> {snap.get('target_branch')}",
        f"  url: {snap.get('web_url')}",
    ]
    hp = snap.get("head_pipeline") or {}
    if hp:
        lines.append(f"  pipeline: {hp.get('id')} {hp.get('status')} on {short(hp.get('sha'))} · {hp.get('web_url')}")
    lines.append(
        f"  merge status: {snap.get('merge_status')} / {snap.get('detailed_merge_status')}"
        f" · conflicts {snap.get('has_conflicts')} · rebase in progress {snap.get('rebase_in_progress')}"
    )
    lines.append(
        f"  reviewers: {snap.get('reviewers') or '-'} · assignees: {snap.get('assignees') or '-'}"
        f" · labels: {snap.get('labels') or '-'}"
    )
    nn = snap.get("newest_note") or {}
    if nn:
        system = " (system)" if nn.get("system") else ""
        lines.append(f"  newest note: @{nn.get('author')}{system} at {nn.get('updated_at')} (note:{nn.get('id')})")
    elif snap.get("notes_probe") == "count-only":
        lines.append("  newest note: unavailable (notes endpoint refused; probing on note count and updated_at only)")
    return lines


@dataclass
class Details:
    discussions_dir: str | None = None
    discussions_summary: list[str] = field(default_factory=list)
    """The lines `glab-discussion read --dump` prints after its `Discussions:` line."""
    pipeline_dir: str | None = None
    pipeline_summary_file: str | None = None
    failed_jobs: list[str] = field(default_factory=list)
    external_statuses: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def details_lines(d: Details) -> list[str]:
    lines: list[str] = []
    if d.discussions_dir:
        lines.append(
            f"  discussions: {d.discussions_dir}/ (one file per thread, rewritten when its content changes;"
            " draft-<id>.txt files and [DRAFT] blocks are your own unpublished drafts, not MR events)"
        )
        summary = [line.strip() for line in d.discussions_summary if line.strip()]
        changed = [line for line in summary if line.startswith(("updated:", "new:", "deleted:"))]
        lines += [f"    {line}" for line in changed]
        if not changed and summary:
            lines.append(f"    {summary[-1]}")
    if d.pipeline_dir:
        lines.append(
            f"  pipeline dump: {d.pipeline_dir}/ (summary.json, jobs.json, job-logs/)"
            f" · summary text: {d.pipeline_summary_file}"
        )
        lines += [f"    failed: {job}" for job in d.failed_jobs]
        failed_ext = [s for s in d.external_statuses if s.get("status") == "failed"]
        for status in failed_ext:
            text = f"    failed external status: {status.get('name')} {status.get('description') or ''}"
            lines.append(f"{text} {status.get('target_url') or ''}".rstrip())
        if d.external_statuses and not failed_ext:
            lines.append(f"    external statuses: {len(d.external_statuses)}, none failed")
    lines += [f"  note: {note}" for note in d.notes]
    lines += [f"  ERROR: {error}" for error in d.errors]
    return lines


def failed_job_lines(summary: dict[str, Any]) -> list[str]:
    """`<name> [<stage>] <failure reason>` for each failed job of a `glab-pipeline inspect` summary."""
    return [
        f"{j.get('name')} [{j.get('stage')}] {j.get('failure_reason') or 'unknown'}"
        for j in summary.get("failed_jobs") or []
        if isinstance(j, dict)
    ]


def pipeline_job_names(pipeline_dir: Path) -> set[str]:
    """Every job name the pipeline dump knows, regular jobs and trigger jobs alike."""
    names: set[str] = set()
    for fname in ("jobs.json", "bridges.json"):
        try:
            entries = json.loads((pipeline_dir / fname).read_text(encoding="utf-8"), strict=False)
        except (OSError, ValueError):
            continue
        if isinstance(entries, list):
            names.update(str(j.get("name")) for j in entries if isinstance(j, dict) and j.get("name"))
    return names


def external_statuses(statuses: list[Any], pipeline_id: Any, job_names: set[str]) -> list[dict[str, Any]]:
    """Commit statuses that the head pipeline did not produce.

    Everything the pipeline ran also shows up as a commit status. GitLab sets the pipeline id on those; a status
    without one is matched by name against the jobs and the trigger jobs (bridges.json), or every downstream trigger
    would read as an external status.
    """
    return [
        s
        for s in statuses
        if isinstance(s, dict)
        and not (pipeline_id is not None and s.get("pipeline_id") == pipeline_id)
        and str(s.get("name")) not in job_names
    ]
