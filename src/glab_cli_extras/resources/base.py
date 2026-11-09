"""What every resource poller has in common.

A poller owns one watched resource. Each `poll()` fetches what changed, diffs it against the previous snapshot and
returns the events. The diff itself lives in a pure function per resource, so it can be tested without any I/O.
The daemon decides when to call `poll()`, using `next_delay()`, `expected_cost()` and the host's budget.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

from glab_cli_extras.daemon.cache import ByteBoundedCache
from glab_cli_extras.events import RawEvent
from glab_cli_extras.gitlab.client import ApiError, GitLabClient, encode_project
from glab_cli_extras.gitlab.refs import ResourceKind, ResourceRef
from glab_cli_extras.gitlab.values import gid_to_int, lower
from glab_cli_extras.serde import from_jsonable, stable_hash, to_jsonable
from glab_cli_extras.sync.job_logs import LogContent, LogsMode

__all__ = ["PollContext", "Poller", "PollerOptions", "PushFeedCache", "gid_to_int", "is_force_push", "lower"]


async def is_force_push(client: GitLabClient, project: str, old_sha: str, new_sha: str) -> tuple[bool, str | None]:
    """Whether moving a ref from `old_sha` to `new_sha` rewrote history. Costs one `merge_base` request.

    The push is a fast-forward exactly when the merge base of the two commits is the old head. If GitLab no longer
    has the old commit (pruned after a force-push), the push is reported as forced, with the reason.
    """
    try:
        base = await client.get_json(
            f"projects/{encode_project(project)}/repository/merge_base?refs[]={old_sha}&refs[]={new_sha}",
            kind="merge_base",
        )
    except ApiError as exc:
        if exc.status not in (400, 404):
            raise
        return True, "the previous head is no longer in the repository"
    return (not isinstance(base, dict) or base.get("id") != old_sha), None


@dataclass(frozen=True, slots=True)
class PollerOptions:
    """What the subscribers of a resource need, as the union over all of them."""

    events: frozenset[str]
    logs: LogsMode = "failed"

    def wants(self, *kinds: str) -> bool:
        return any(kind in self.events for kind in kinds)


@dataclass
class PushFeedCache:
    """One `GET /projects/:id/events?action=pushed` call serves every watched branch of a project."""

    max_age: float = 15.0
    _entries: dict[tuple[str, str], tuple[float, list[Any]]] = field(default_factory=dict)

    def get(self, host: str, project: str, now: float) -> list[Any] | None:
        entry = self._entries.get((host, project))
        if entry is None or now - entry[0] > self.max_age:
            return None
        return entry[1]

    def put(self, host: str, project: str, now: float, events: list[Any]) -> None:
        self._entries[(host, project)] = (now, events)


@dataclass
class PollContext:
    """What a poller shares with the engine. The caches hold what the pollers fetched ahead for the clients: the logs
    of jobs that ended and the threads that changed, so the client's request right after the event is a hit."""

    client: GitLabClient
    clock: Callable[[], float] = time.time
    push_feeds: PushFeedCache = field(default_factory=PushFeedCache)
    logs: ByteBoundedCache[LogContent] = field(default_factory=ByteBoundedCache)
    threads: ByteBoundedCache[dict[str, Any]] = field(default_factory=ByteBoundedCache)


S = TypeVar("S")


class Poller(ABC, Generic[S]):
    kind: ClassVar[ResourceKind]
    snapshot_type: ClassVar[type[Any]]

    def __init__(self, ref: ResourceRef, ctx: PollContext, options: PollerOptions) -> None:
        self.ref = ref
        self.ctx = ctx
        self.options = options
        self.snapshot: S | None = None
        self.terminal_reason: str | None = None
        self.terminal_data: dict[str, Any] = {}

    def set_options(self, options: PollerOptions) -> None:
        self.options = options

    def seed(self, data: Any) -> bool:
        """Start from a snapshot a client kept from an earlier daemon, so the first poll reports what changed."""
        if self.snapshot is not None or data is None:
            return False
        try:
            self.snapshot = from_jsonable(self.snapshot_type, data)
        except ValueError:
            return False
        return True

    @abstractmethod
    async def poll(self) -> list[RawEvent]:
        """Fetch, diff, update the snapshot. The first poll without a seed is the baseline and returns no events."""

    @abstractmethod
    def next_delay(self) -> float:
        """Base seconds until the next poll, before the daemon stretches it under budget pressure."""

    def expected_cost(self) -> float:
        """Average requests per poll, for the budget's demand estimate."""
        return 1.0

    @abstractmethod
    def summary(self) -> dict[str, Any]:
        """A compact description of the current snapshot, sent in `watch_started`."""

    def children(self) -> frozenset[ResourceRef]:
        """Other resources whose events this one relays (an MR relays its head pipeline)."""
        return frozenset()

    def child_options(self, child: ResourceRef) -> PollerOptions | None:
        return None

    @property
    def terminal(self) -> bool:
        return self.terminal_reason is not None

    def snapshot_json(self) -> Any:
        return to_jsonable(self.snapshot) if self.snapshot is not None else None

    def snapshot_hash(self) -> str | None:
        return stable_hash(self.snapshot) if self.snapshot is not None else None
