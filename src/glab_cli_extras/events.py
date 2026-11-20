"""glab-watch's event kinds per resource, their groups and threshold options, as one cli-watch-kit catalog.

The event model (`Event`, `RawEvent`) and the `--events`/`--exclude-events` filter are cli-watch-kit's; this module
re-exports them, so the rest of glab-cli-extras imports them from one place.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from cli_watch_kit.events import (
    LIFECYCLE_KINDS,
    WATCH_DEGRADED,
    WATCH_ENDED,
    WATCH_STARTED,
    Event,
    FilterError,
    KindCatalog,
    RawEvent,
    split_values,
    utc_now_iso,
)

__all__ = [
    "ALL_KINDS",
    "CATALOG",
    "GROUPS",
    "KINDS_BY_RESOURCE",
    "LIFECYCLE_KINDS",
    "MR_PIPELINE_KINDS",
    "PIPELINE_STUCK",
    "QUIET",
    "THRESHOLD_OPTIONS",
    "WATCH_DEGRADED",
    "WATCH_ENDED",
    "WATCH_STARTED",
    "Event",
    "FilterError",
    "RawEvent",
    "resolve_filter",
    "split_values",
    "utc_now_iso",
]

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
MY_MRS_KINDS: tuple[str, ...] = (
    "mr_opened",
    "mr_updated",
    "mr_merged",
    "mr_closed",
    "list_changed",
    "review_state_changed",
)
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
    "my-mrs": MY_MRS_KINDS,
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
        "list_changed",
        "review_state_changed",
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


CATALOG = KindCatalog(
    kinds_by_resource=KINDS_BY_RESOURCE,
    groups={name: kinds for name, kinds in GROUPS.items() if name != "lifecycle"},
    thresholds=THRESHOLD_OPTIONS,
)


def resolve_filter(
    resource_kind: str,
    include: Iterable[str] | None,
    exclude: Iterable[str] | None,
    enabled: Iterable[str] = (),
) -> frozenset[str]:
    """The event kinds a watch reports; see `KindCatalog.resolve`."""
    return CATALOG.resolve(resource_kind, include, exclude, enabled)
