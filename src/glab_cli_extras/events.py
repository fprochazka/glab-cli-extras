"""The event model, the catalogue of event kinds per resource, and the `--events`/`--exclude-events` filter."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

WATCH_STARTED = "watch_started"
WATCH_DEGRADED = "watch_degraded"
WATCH_ENDED = "watch_ended"
LIFECYCLE_KINDS: tuple[str, ...] = (WATCH_STARTED, WATCH_DEGRADED, WATCH_ENDED)

PIPELINE_KINDS: tuple[str, ...] = ("status_changed", "job_started", "job_finished", "downstream_created", "finished")
MR_OWN_KINDS: tuple[str, ...] = (
    "state_changed",
    "draft_changed",
    "title_changed",
    "description_changed",
    "target_branch_changed",
    "pushed",
    "merge_status_changed",
    "approvals_changed",
    "reviewers_changed",
    "assignees_changed",
    "labels_changed",
    "blocking_threads_resolved_changed",
    "head_pipeline_changed",
    "external_status_changed",
    "thread_created",
    "thread_updated",
    "thread_resolved",
    "thread_unresolved",
)
MRS_KINDS: tuple[str, ...] = ("mr_opened", "mr_updated", "mr_merged", "mr_closed", "mr_reopened")
BRANCH_KINDS: tuple[str, ...] = (
    "pushed",
    "force_pushed",
    "created",
    "deleted",
    "pipeline_created",
    "pipeline_finished",
)

MR_PIPELINE_KINDS: tuple[str, ...] = ("pipeline_status_changed", "pipeline_finished")
"""What an MR watch reports about its head pipeline, besides `head_pipeline_changed`. Job-level detail needs a
pipeline watch, whose command every one of these events carries."""

QUIET = "quiet"
PIPELINE_STUCK = "pipeline_stuck"
THRESHOLD_OPTIONS: Mapping[str, str] = {QUIET: "--quiet-after", PIPELINE_STUCK: "--stuck-after"}
"""The kinds the watch client computes from what it already receives, and the option that switches each one on.
They are never sent to the daemon, and a watch reports one only when its option is given."""

KINDS_BY_RESOURCE: Mapping[str, tuple[str, ...]] = {
    "pipeline": (*PIPELINE_KINDS, PIPELINE_STUCK),
    "mr": (*MR_OWN_KINDS, *MR_PIPELINE_KINDS, QUIET, PIPELINE_STUCK),
    "mrs": MRS_KINDS,
    "branch": BRANCH_KINDS,
}

GROUPS: Mapping[str, tuple[str, ...]] = {
    "status": (
        "state_changed",
        "draft_changed",
        "merge_status_changed",
        "approvals_changed",
        "reviewers_changed",
        "assignees_changed",
        "labels_changed",
        "blocking_threads_resolved_changed",
        "title_changed",
        "description_changed",
        "target_branch_changed",
        QUIET,
        *MRS_KINDS,
    ),
    "comments": ("thread_created", "thread_updated", "thread_resolved", "thread_unresolved"),
    "push": ("pushed", "force_pushed", "created", "deleted"),
    "pipeline": (
        "head_pipeline_changed",
        "external_status_changed",
        *PIPELINE_KINDS,
        "pipeline_status_changed",
        "pipeline_created",
        "pipeline_finished",
        PIPELINE_STUCK,
    ),
    "lifecycle": LIFECYCLE_KINDS,
}

ALL_KINDS: frozenset[str] = frozenset(k for kinds in KINDS_BY_RESOURCE.values() for k in kinds) | frozenset(
    LIFECYCLE_KINDS
)


class FilterError(ValueError):
    pass


def _expand(resource_kind: str, values: Iterable[str], option: str, enabled: frozenset[str]) -> set[str]:
    produced = set(KINDS_BY_RESOURCE[resource_kind]) | set(LIFECYCLE_KINDS)
    switched_off = set(THRESHOLD_OPTIONS) - enabled
    expanded: set[str] = set()
    for raw in values:
        name = raw.strip()
        if not name:
            continue
        if name in GROUPS:
            # A group brings a threshold kind only when its option is given: `--events=status` stays usable
            # without `--quiet-after`.
            members = set(GROUPS[name]) & produced - switched_off
            if not members:
                raise FilterError(f"{option}: the group {name!r} has no events for a {resource_kind} watch")
            expanded |= members
        elif name in ALL_KINDS:
            if name not in produced:
                raise FilterError(f"{option}: a {resource_kind} watch never produces {name!r}")
            if name in switched_off and option == "--events":
                raise FilterError(f"{option}: {name!r} is reported only with {THRESHOLD_OPTIONS[name]}")
            expanded.add(name)
        else:
            known = ", ".join(sorted(GROUPS))
            raise FilterError(f"{option}: unknown event kind or group {name!r} (groups: {known})")
    return expanded


def split_values(values: Iterable[str] | None) -> list[str]:
    """Flatten repeated and comma-separated option values."""
    return [part for value in values or () for part in value.split(",") if part.strip()]


def resolve_filter(
    resource_kind: str,
    include: Iterable[str] | None,
    exclude: Iterable[str] | None,
    enabled: Iterable[str] = (),
) -> frozenset[str]:
    """Return the event kinds a watch reports. Lifecycle events are always in, and cannot be excluded.

    `enabled` names the threshold kinds whose option was given. Without its option a threshold kind is never
    reported, and with it the kind must survive the filter, so an option never silently does nothing.
    """
    enabled_set = frozenset(enabled)
    include_list = split_values(include)
    exclude_list = split_values(exclude)
    if include_list:
        selected = _expand(resource_kind, include_list, "--events", enabled_set)
    else:
        selected = set(KINDS_BY_RESOURCE[resource_kind]) - (set(THRESHOLD_OPTIONS) - enabled_set)
    if exclude_list:
        excluded = _expand(resource_kind, exclude_list, "--exclude-events", enabled_set)
        if excluded & set(LIFECYCLE_KINDS):
            raise FilterError("--exclude-events: lifecycle events are always reported and cannot be excluded")
        selected -= excluded
    for kind in sorted(enabled_set):
        if kind not in KINDS_BY_RESOURCE[resource_kind]:
            raise FilterError(f"{THRESHOLD_OPTIONS[kind]}: a {resource_kind} watch never produces {kind!r}")
        if kind not in selected:
            raise FilterError(f"{THRESHOLD_OPTIONS[kind]}: {kind!r} is filtered out by --events/--exclude-events")
    return frozenset(selected | set(LIFECYCLE_KINDS))


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True, slots=True)
class RawEvent:
    """An event as a poller produces it, before the daemon assigns its sequence number."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    resource: str
    seq: int
    observed_at: str
    data: dict[str, Any]
    artifacts: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "resource": self.resource,
            "seq": self.seq,
            "observed_at": self.observed_at,
            "data": self.data,
        }
        if self.artifacts:
            payload["artifacts"] = list(self.artifacts)
        return payload

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Event:
        return cls(
            kind=str(data["kind"]),
            resource=str(data["resource"]),
            seq=int(data["seq"]),
            observed_at=str(data["observed_at"]),
            data=dict(data.get("data") or {}),
            artifacts=tuple(str(a) for a in data.get("artifacts") or ()),
        )
