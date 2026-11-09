"""The events a watcher derives from time, `quiet` and `pipeline_stuck`, and the idle end of a watch.

Both come from the snapshots and events the watcher already receives, so they cost no request. The daemon never
sees them: they depend on what this watcher reported, which differs per watcher of the same resource.

- `quiet` (MR): the head pipeline succeeded on the current head, and nothing else was reported for a threshold's
  length. Once per head SHA and threshold. A push, a new green pipeline and every reported event start the count
  again.
- `pipeline_stuck` (MR head pipeline, or the watched pipeline): still `running` or `pending` a threshold's length
  after the watcher first saw it in one of those statuses. Once per pipeline id.
- idle: nothing was reported for `--idle-after`, so the watch ends with `watch_ended` reason `idle`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from glab_cli_extras.events import PIPELINE_STUCK, QUIET, Event

ACTIVE_STATUSES = frozenset({"running", "pending"})
TIMER_KINDS = frozenset({QUIET, PIPELINE_STUCK})
"""Events about the passing of time. They do not count as activity: a quiet MR stays quiet after `quiet`."""


def format_duration(seconds: float) -> str:
    """`90` -> `1m30s`, `7200` -> `2h`: the spelling `--timeout` and the threshold options accept."""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    text = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "") + (f"{secs}s" if secs else "")
    return text or "0s"


@dataclass(frozen=True)
class Thresholds:
    quiet_after: tuple[float, ...] = ()
    stuck_after: float | None = None
    idle_after: float | None = None

    def describe(self) -> list[str]:
        parts = []
        if self.quiet_after:
            parts.append(f"quiet after {', '.join(format_duration(t) for t in self.quiet_after)}")
        if self.stuck_after is not None:
            parts.append(f"pipeline_stuck after {format_duration(self.stuck_after)}")
        return parts


@dataclass
class _Pipeline:
    id: int | None
    status: str | None
    sha: str | None
    web_url: str | None
    running_jobs: tuple[str, ...] = ()


class WatchTimers:
    """Tracks what the threshold events need and says which are due. The caller passes a monotonic `now`."""

    def __init__(self, resource_kind: str, thresholds: Thresholds, now: float) -> None:
        self.kind = resource_kind
        self.thresholds = thresholds
        self._last_activity = now
        self._head_sha: str | None = None
        self._head_since = now
        self._pipeline = _Pipeline(None, None, None, None)
        self._green_since: float | None = None
        self._active_since: dict[int, float] = {}
        self._quiet_sent: set[tuple[str, float]] = set()
        self._stuck_sent: set[int] = set()

    def snapshot(self, data: Any, now: float) -> None:
        """Take in a snapshot the daemon sent, in the JSON form of the resource's snapshot dataclass."""
        if not isinstance(data, Mapping):
            return
        if self.kind == "mr":
            head = data.get("head_sha")
            if isinstance(head, str) and head != self._head_sha:
                self._head_sha = head
                self._head_since = now
            pipeline = _Pipeline(
                data.get("head_pipeline_id"),
                data.get("head_pipeline_status"),
                data.get("head_pipeline_sha"),
                data.get("head_pipeline_web_url"),
            )
        elif self.kind == "pipeline":
            jobs = data.get("jobs") or []
            running = tuple(
                str(j.get("name")) for j in jobs if isinstance(j, Mapping) and j.get("status") in ACTIVE_STATUSES
            )
            pipeline = _Pipeline(data.get("id"), data.get("status"), data.get("sha"), data.get("web_url"), running)
        else:
            return
        previous = self._pipeline
        self._pipeline = pipeline
        if (pipeline.id, pipeline.status) != (previous.id, previous.status):
            self._green_since = now if pipeline.status == "success" else None
        if isinstance(pipeline.id, int):
            if pipeline.status in ACTIVE_STATUSES:
                self._active_since.setdefault(pipeline.id, now)
            else:
                self._active_since.pop(pipeline.id, None)

    def reported(self, events: Iterable[Event], now: float) -> None:
        """Every reported event other than a threshold event restarts the quiet and idle counts."""
        if any(e.kind not in TIMER_KINDS for e in events):
            self._last_activity = now

    def _quiet_reference(self) -> float | None:
        p = self._pipeline
        if self.kind != "mr" or self._head_sha is None or p.status != "success" or self._green_since is None:
            return None
        # A daemon of an older release sends no pipeline SHA; then the pipeline is taken to be on the head.
        if p.sha is not None and p.sha != self._head_sha:
            return None
        return max(self._last_activity, self._head_since, self._green_since)

    def due(self, now: float) -> list[tuple[str, dict[str, Any]]]:
        """The threshold events that are due now, as (kind, data). Each is returned once."""
        found: list[tuple[str, dict[str, Any]]] = []
        reference = self._quiet_reference()
        if reference is not None and self._head_sha is not None:
            for threshold in self.thresholds.quiet_after:
                key = (self._head_sha, threshold)
                if key not in self._quiet_sent and now - reference >= threshold:
                    self._quiet_sent.add(key)
                    found.append(
                        (
                            QUIET,
                            {
                                "after": format_duration(threshold),
                                "head_sha": self._head_sha,
                                "pipeline_id": self._pipeline.id,
                            },
                        )
                    )
        stuck = self.thresholds.stuck_after
        p = self._pipeline
        if stuck is not None and isinstance(p.id, int) and p.status in ACTIVE_STATUSES and p.id not in self._stuck_sent:
            since = self._active_since.get(p.id)
            if since is not None and now - since >= stuck:
                self._stuck_sent.add(p.id)
                data: dict[str, Any] = {
                    "after": format_duration(stuck),
                    "pipeline_id": p.id,
                    "status": p.status,
                    "web_url": p.web_url,
                }
                if self.kind == "mr":
                    data["watch_command"] = f"glab-watch pipeline {p.web_url}" if p.web_url else None
                else:
                    data["running_jobs"] = list(p.running_jobs)
                found.append((PIPELINE_STUCK, data))
        return found

    def idle(self, now: float) -> bool:
        idle_after = self.thresholds.idle_after
        return idle_after is not None and now - self._last_activity >= idle_after

    def seconds_until_next(self, now: float) -> float | None:
        """How long until the next threshold can fire, so the watcher does not sleep past it."""
        candidates: list[float] = []
        if self.thresholds.idle_after is not None:
            candidates.append(self._last_activity + self.thresholds.idle_after - now)
        reference = self._quiet_reference()
        if reference is not None and self._head_sha is not None:
            candidates += [
                reference + t - now for t in self.thresholds.quiet_after if (self._head_sha, t) not in self._quiet_sent
            ]
        p = self._pipeline
        if self.thresholds.stuck_after is not None and isinstance(p.id, int) and p.id not in self._stuck_sent:
            since = self._active_since.get(p.id)
            if since is not None:
                candidates.append(since + self.thresholds.stuck_after - now)
        return max(0.0, min(candidates)) if candidates else None
