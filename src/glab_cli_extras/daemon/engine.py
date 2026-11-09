"""The polling engine: subscriptions with leases, refcounted resources, per-resource event logs, and the loop that
polls resources under each host's budget.

The daemon serves the engine over its socket. `--no-daemon` runs the same engine inside the client process.

- **Resources** are shared: two subscriptions to the same resource use one poller, which does the union of the work
  their filters need. A resource can also be held by a parent resource (an MR holds its head pipeline); the parent
  then receives the child's events in its own log.
- **Event log:** each resource keeps its last 500 events with sequence numbers. A new subscriber starts at the head of
  the log and gets a private `watch_started` with the current snapshot, never the backlog.
- **Leases:** every client poll renews all of that client's subscriptions. A subscription not renewed within the lease
  timeout is dropped, so a killed client never keeps a poller alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from glab_cli_extras.daemon.cache import ByteBoundedCache
from glab_cli_extras.daemon.scheduler import Scheduler
from glab_cli_extras.events import (
    LIFECYCLE_KINDS,
    WATCH_DEGRADED,
    WATCH_ENDED,
    WATCH_STARTED,
    Event,
    RawEvent,
    utc_now_iso,
)
from glab_cli_extras.gitlab.client import (
    ApiError,
    GitLabClient,
    NotFound,
    RateLimited,
    TransportError,
    Unauthorized,
)
from glab_cli_extras.gitlab.glab import GlabNotFoundError, GlabRunner, run_glab
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources import POLLERS
from glab_cli_extras.resources.base import PollContext, Poller, PollerOptions, PushFeedCache
from glab_cli_extras.resources.mr import prefetch_threads, thread_cache_key
from glab_cli_extras.resources.pipeline import PipelinePoller, log_cache_key, prefetch_log
from glab_cli_extras.serde import stable_hash, to_jsonable
from glab_cli_extras.sync.dump import fetch_dump
from glab_cli_extras.sync.job_logs import (
    FINAL_JOB_STATUSES,
    LOGS_MODES,
    LogContent,
    LogsMode,
    fetch_log_content,
    merge_logs_modes,
)
from glab_cli_extras.sync.mr import MrSnapshot, fetch_discussions, thread_sig
from glab_cli_extras.sync.pipeline import JobState, PipelineSnapshot

log = logging.getLogger(__name__)

EVENT_LOG_SIZE = 500
DEFAULT_LEASE_TIMEOUT = 20.0
DEFAULT_CLIENT_POLL_MS = 5000
MAX_INFLIGHT_PER_HOST = 4
MAX_ERROR_BACKOFF = 300.0


class EngineError(Exception):
    pass


@dataclass
class Subscription:
    id: str
    client_id: str
    ref: ResourceRef
    events: frozenset[str]
    logs: LogsMode
    renewed_at: float
    started: bool = False
    private: list[Event] = field(default_factory=list)
    sent_snapshot_hash: str | None = None


@dataclass
class Resource:
    ref: ResourceRef
    poller: Poller[Any]
    log: deque[Event] = field(default_factory=lambda: deque(maxlen=EVENT_LOG_SIZE))
    seq: int = 0
    subscriptions: set[str] = field(default_factory=set)
    parents: set[str] = field(default_factory=set)
    children: set[str] = field(default_factory=set)
    polled: bool = False
    polling: bool = False
    ended: bool = False
    end_event: Event | None = None
    degraded: str | None = None
    failures: int = 0
    polls: int = 0
    last_poll_at: float | None = None
    last_error: str | None = None

    @property
    def key(self) -> str:
        return self.ref.key

    @property
    def held(self) -> bool:
        return bool(self.subscriptions or self.parents)


class Engine:
    def __init__(
        self,
        *,
        runner: GlabRunner = run_glab,
        clock: Callable[[], float] = time.time,
        lease_timeout: float = DEFAULT_LEASE_TIMEOUT,
        client_poll_ms: int = DEFAULT_CLIENT_POLL_MS,
    ) -> None:
        self.clock = clock
        self.lease_timeout = lease_timeout
        self.client_poll_ms = client_poll_ms
        self.started_at = clock()
        self.resources: dict[str, Resource] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.scheduler = Scheduler()
        self._runner = runner
        self._clients: dict[str, GitLabClient] = {}
        self._push_feeds = PushFeedCache()
        self.logs: ByteBoundedCache[LogContent] = ByteBoundedCache()
        self.threads: ByteBoundedCache[dict[str, Any]] = ByteBoundedCache()
        self.dumps: ByteBoundedCache[dict[str, Any]] = ByteBoundedCache(16 * 1024 * 1024)
        self._inflight: dict[str, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._wake: asyncio.Event | None = None
        self._idle_since: float | None = clock()

    # ----------------------------------------------------------------------------------------------------------
    # Client-facing operations

    def client_for(self, host: str) -> GitLabClient:
        client = self._clients.get(host)
        if client is None:
            client = GitLabClient(host, runner=self._runner, clock=self.clock)
            self._clients[host] = client
        return client

    def subscribe(
        self,
        client_id: str,
        ref: ResourceRef,
        events: frozenset[str],
        logs: LogsMode = "failed",
        baseline: Any = None,
    ) -> dict[str, Any]:
        if logs not in LOGS_MODES:
            raise EngineError(f"unknown logs mode {logs!r}")
        now = self.clock()
        res = self._acquire(ref)
        if baseline is not None and not res.polled:
            res.poller.seed(baseline)
        sub = Subscription(uuid.uuid4().hex, client_id, ref, events | frozenset(LIFECYCLE_KINDS), logs, now)
        self.subscriptions[sub.id] = sub
        res.subscriptions.add(sub.id)
        self._idle_since = None
        cursor = res.seq
        if res.polled:
            sub.private.append(self._started_event(res))
            sub.started = True
        if res.end_event is not None:
            sub.private.append(res.end_event)
        self._recompute_options(res)
        log.info("subscribe %s client=%s sub=%s", ref.key, client_id[:8], sub.id[:8])
        self._kick()
        return {"subscription_id": sub.id, "cursor": cursor}

    def poll(self, client_id: str, cursors: Mapping[str, int]) -> dict[str, Any]:
        now = self.clock()
        for owned in self.subscriptions.values():
            if owned.client_id == client_id:
                owned.renewed_at = now
        events: dict[str, list[dict[str, Any]]] = {}
        snapshots: dict[str, dict[str, Any]] = {}
        unknown: list[str] = []
        for sid, cursor in cursors.items():
            sub = self.subscriptions.get(sid)
            if sub is None or sub.client_id != client_id:
                unknown.append(sid)
                continue
            res = self.resources.get(sub.ref.key)
            if res is None:
                unknown.append(sid)
                continue
            batch = list(sub.private)
            sub.private.clear()
            batch.extend(e for e in res.log if e.seq > cursor and e.kind in sub.events)
            if batch:
                events[sid] = [e.to_json() for e in batch]
            snapshot_hash = res.poller.snapshot_hash()
            if snapshot_hash is not None and snapshot_hash != sub.sent_snapshot_hash:
                snapshots[sid] = {"hash": snapshot_hash, "data": res.poller.snapshot_json()}
                sub.sent_snapshot_hash = snapshot_hash
        return {"events": events, "snapshots": snapshots, "unknown": unknown, "next_poll_ms": self.client_poll_ms}

    def unsubscribe(self, client_id: str, subscription_id: str) -> bool:
        sub = self.subscriptions.get(subscription_id)
        if sub is None or sub.client_id != client_id:
            return False
        self._drop_subscription(sub, "unsubscribed")
        return True

    def idle_for(self, now: float) -> float:
        """Seconds without any subscription, 0 while there is one."""
        if self.subscriptions or self._idle_since is None:
            return 0.0
        return now - self._idle_since

    def reap(self, now: float) -> None:
        expired = [s for s in self.subscriptions.values() if now - s.renewed_at > self.lease_timeout]
        for sub in expired:
            self._drop_subscription(sub, "lease expired")

    def status(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "subscriptions": [
                {
                    "id": s.id,
                    "client_id": s.client_id,
                    "resource": s.ref.key,
                    "idle_s": round(now - s.renewed_at, 1),
                    "logs": s.logs,
                    "events": sorted(s.events - frozenset(LIFECYCLE_KINDS)),
                }
                for s in self.subscriptions.values()
            ],
            "resources": [
                {
                    "resource": r.key,
                    "subscriptions": len(r.subscriptions),
                    "parents": sorted(r.parents),
                    "children": sorted(r.children),
                    "seq": r.seq,
                    "polls": r.polls,
                    "ended": r.ended,
                    "degraded": r.degraded,
                    "last_error": r.last_error,
                    "next_poll_in_s": round(d - now, 1) if (d := self.scheduler.due_at(r.key)) is not None else None,
                }
                for r in self.resources.values()
            ],
            "hosts": {
                host: {"budget": c.budget.describe(now), "requests": c.stats.describe()}
                for host, c in sorted(self._clients.items())
            },
            "caches": {
                "logs": self.logs.describe(),
                "threads": self.threads.describe(),
                "dumps": self.dumps.describe(),
            },
        }

    # ----------------------------------------------------------------------------------------------------------
    # Fetches on behalf of clients. The daemon writes no files: it hands the content to the client, which writes it.

    async def _await_budget(self, host: str) -> None:
        """Wait until the host's budget allows one more request, so on-demand fetches stay as gentle as polls."""
        budget = self.client_for(host).budget
        for _ in range(600):
            now = self.clock()
            if budget.can_spend(1.0, now):
                return
            await asyncio.sleep(min(5.0, max(0.05, budget.wait_time(1.0, now))))

    def _known_job(self, host: str, job_id: int) -> JobState | None:
        for res in self.resources.values():
            snap = res.poller.snapshot
            if res.ref.host == host and isinstance(snap, PipelineSnapshot):
                for job in snap.jobs:
                    if job.id == job_id:
                        return job
        return None

    async def fetch_job_log(self, host: str, project: str, job_id: int) -> LogContent:
        """The log of a job, from the cache when a poller or another client already fetched it.

        Only the log of an ended job is cached: a running job's log still grows.
        """
        job = self._known_job(host, job_id)
        if job is not None and job.status in FINAL_JOB_STATUSES:
            ctx = PollContext(self.client_for(host), self.clock, self._push_feeds, self.logs, self.threads)
            cached = self.logs.get(log_cache_key(host, job_id))
            if cached is not None:
                return cached
            await self._await_budget(host)
            return await prefetch_log(ctx, job)
        await self._await_budget(host)
        return await fetch_log_content(self.client_for(host), project, job_id, job.trace_size if job else None)

    async def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> dict[str, dict[str, Any]]:
        """These discussions of a watched MR in their current version; a thread GitLab no longer has is left out."""
        res = self.resources.get(ref.key)
        snap = res.poller.snapshot if res is not None else None
        ctx = PollContext(self.client_for(ref.host), self.clock, self._push_feeds, self.logs, self.threads)
        if isinstance(snap, MrSnapshot) and snap.threads is not None:
            sigs = {t.id: thread_sig(t) for t in snap.threads}
            if any(self.threads.get(thread_cache_key(ref, i, sigs.get(i, ""))) is None for i in ids):
                await self._await_budget(ref.host)
            return await prefetch_threads(ctx, ref, snap, ids)
        await self._await_budget(ref.host)
        return await fetch_discussions(ctx.client, ref, ids)

    async def fetch_dump(self, ref: ResourceRef) -> dict[str, Any]:
        """A finished pipeline's REST dump and extras, with the snapshot and ETag the daemon holds for it.

        Cached by the snapshot it belongs to, so every client of one finished pipeline costs one fetch.
        """
        res = self.resources.get(ref.key)
        poller = res.poller if res is not None else None
        snap = poller.snapshot if poller is not None else None
        if not isinstance(snap, PipelineSnapshot):
            snap = None
        key = ("dump", ref.key, stable_hash(snap) if snap is not None else None)
        cached = self.dumps.get(key) if snap is not None else None
        if cached is not None:
            return cached
        await self._await_budget(ref.host)
        dump = await fetch_dump(self.client_for(ref.host), ref)
        if snap is not None:
            dump["snapshot"] = to_jsonable(snap)
            dump["snapshot_hash"] = stable_hash(snap)
            dump["etag"] = poller.etag if isinstance(poller, PipelinePoller) else None
            self.dumps.put(key, dump, len(json.dumps(dump)))
        return dump

    # ----------------------------------------------------------------------------------------------------------
    # Resources

    def _acquire(self, ref: ResourceRef) -> Resource:
        res = self.resources.get(ref.key)
        if res is None:
            ctx = PollContext(self.client_for(ref.host), self.clock, self._push_feeds, self.logs, self.threads)
            poller = POLLERS[ref.kind](ref, ctx, PollerOptions(frozenset(), "none"))
            res = Resource(ref, poller)
            self.resources[ref.key] = res
            self.scheduler.schedule(ref.key, self.clock(), urgent=True)
            log.info("start polling %s", ref.key)
        return res

    def _release(self, res: Resource) -> None:
        if res.held or res.key not in self.resources:
            return
        del self.resources[res.key]
        self.scheduler.forget(res.key)
        log.info("stop polling %s", res.key)
        for child_key in list(res.children):
            self._unlink_child(res, child_key)

    def _unlink_child(self, parent: Resource, child_key: str) -> None:
        parent.children.discard(child_key)
        child = self.resources.get(child_key)
        if child is None:
            return
        child.parents.discard(parent.key)
        self._recompute_options(child)
        self._release(child)

    def _drop_subscription(self, sub: Subscription, why: str) -> None:
        self.subscriptions.pop(sub.id, None)
        log.info("drop subscription %s on %s: %s", sub.id[:8], sub.ref.key, why)
        res = self.resources.get(sub.ref.key)
        if res is not None:
            res.subscriptions.discard(sub.id)
            self._recompute_options(res)
            self._release(res)
        if not self.subscriptions:
            self._idle_since = self.clock()

    def _recompute_options(self, res: Resource) -> None:
        events: set[str] = set()
        modes: list[LogsMode] = []
        for sid in res.subscriptions:
            sub = self.subscriptions[sid]
            events |= sub.events
            modes.append(sub.logs)
        for parent_key in res.parents:
            parent = self.resources.get(parent_key)
            options = parent.poller.child_options(res.ref) if parent is not None else None
            if options is not None:
                events |= options.events
                modes.append(options.logs)
        new = PollerOptions(frozenset(events), merge_logs_modes(modes))
        if new == res.poller.options:
            return
        grew = not new.events <= res.poller.options.events
        res.poller.set_options(new)
        if grew and res.polled and not res.ended and not res.polling:
            self.scheduler.schedule(res.key, self.clock(), urgent=True)
            self._kick()
        for child_key in list(res.children):
            child = self.resources.get(child_key)
            if child is not None:
                self._recompute_options(child)

    def _reconcile_children(self, res: Resource) -> None:
        wanted = {ref.key: ref for ref in res.poller.children()} if not res.ended else {}
        for child_key in list(res.children):
            if child_key not in wanted:
                self._unlink_child(res, child_key)
        for key, ref in wanted.items():
            if key in res.children:
                continue
            child = self._acquire(ref)
            child.parents.add(res.key)
            res.children.add(key)
            self._recompute_options(child)

    # ----------------------------------------------------------------------------------------------------------
    # Events

    def _started_event(self, res: Resource) -> Event:
        data = {"summary": res.poller.summary(), "snapshot_hash": res.poller.snapshot_hash()}
        return Event(WATCH_STARTED, res.key, res.seq, utc_now_iso(), data)

    def _append(self, res: Resource, raw: RawEvent) -> None:
        res.seq += 1
        event = Event(raw.kind, res.key, res.seq, utc_now_iso(), dict(raw.data), raw.artifacts)
        res.log.append(event)
        if raw.kind in LIFECYCLE_KINDS:
            return
        for parent_key in res.parents:
            parent = self.resources.get(parent_key)
            if parent is not None and not parent.ended:
                self._append(parent, raw)

    def _end(self, res: Resource, reason: str, data: Mapping[str, Any]) -> None:
        self._append(res, RawEvent(WATCH_ENDED, {"reason": reason, **data}))
        res.ended = True
        res.end_event = res.log[-1]
        self.scheduler.remove(res.key)
        for child_key in list(res.children):
            self._unlink_child(res, child_key)
        log.info("ended %s: %s", res.key, reason)

    def _degrade(self, res: Resource, reason: str, message: str) -> None:
        res.last_error = message
        if res.degraded == reason:
            return
        res.degraded = reason
        self._append(res, RawEvent(WATCH_DEGRADED, {"reason": reason, "message": message}))
        log.warning("degraded %s: %s: %s", res.key, reason, message)

    # ----------------------------------------------------------------------------------------------------------
    # Polling

    def _kick(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def _stretch(self, host: str, now: float) -> float:
        client = self._clients[host]
        demand = sum(
            r.poller.expected_cost() / max(r.poller.next_delay(), 0.5)
            for r in self.resources.values()
            if r.ref.host == host and not r.ended
        )
        return client.budget.stretch_factor(demand, now)

    async def _poll(self, res: Resource) -> None:
        host = res.ref.host
        client = self.client_for(host)
        delay: float | None = None
        try:
            raws = await res.poller.poll()
        except NotFound as exc:
            if not res.polled:
                self._end(res, "not_found", {"message": str(exc)})
            else:
                self._degrade(res, "not_found", str(exc))
                delay = self._error_delay(res)
        except RateLimited as exc:
            self._degrade(res, "rate_limited", str(exc))
            delay = max(self._error_delay(res), client.budget.blocked_until - self.clock())
        except Unauthorized as exc:
            self._degrade(res, "auth", str(exc))
            delay = self._error_delay(res)
        except (TransportError, ApiError, GlabNotFoundError) as exc:
            self._degrade(res, "error", str(exc))
            delay = self._error_delay(res)
        except Exception as exc:
            log.exception("poll of %s failed", res.key)
            self._degrade(res, "error", f"{type(exc).__name__}: {exc}")
            delay = self._error_delay(res)
        else:
            res.failures = 0
            res.degraded = None
            res.last_error = None
            first = not res.polled
            res.polled = True
            for raw in raws:
                self._append(res, raw)
            if first:
                for sid in res.subscriptions:
                    sub = self.subscriptions[sid]
                    if not sub.started:
                        sub.private.append(self._started_event(res))
                        sub.started = True
            if res.poller.terminal:
                self._end(res, res.poller.terminal_reason or "finished", res.poller.terminal_data)
            else:
                self._reconcile_children(res)
                now = self.clock()
                delay = res.poller.next_delay() * self._stretch(host, now)
        finally:
            res.polling = False
            res.polls += 1
            res.last_poll_at = self.clock()
            self._inflight[host] = self._inflight.get(host, 1) - 1
        if delay is not None and not res.ended and res.key in self.resources:
            self.scheduler.schedule(res.key, self.clock() + delay)
        self._kick()

    def _error_delay(self, res: Resource) -> float:
        res.failures += 1
        return float(min(res.poller.next_delay() * (2 ** min(res.failures, 8)), MAX_ERROR_BACKOFF))

    def _start_due(self, now: float) -> float:
        """Start every due poll the budgets allow. Return seconds until the loop should look again."""
        wait = 1.0
        for key in self.scheduler.ready(now):
            res = self.resources.get(key)
            if res is None:
                self.scheduler.forget(key)
                continue
            host = res.ref.host
            if self._inflight.get(host, 0) >= MAX_INFLIGHT_PER_HOST:
                continue
            budget = self.client_for(host).budget
            cost = res.poller.expected_cost()
            if not budget.can_spend(cost, now):
                wait = min(wait, max(0.05, budget.wait_time(cost, now)))
                continue
            self.scheduler.mark_served(key, now)
            res.polling = True
            self._inflight[host] = self._inflight.get(host, 0) + 1
            task = asyncio.create_task(self._poll(res))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        next_due = self.scheduler.next_due()
        if next_due is not None:
            wait = min(wait, max(0.05, next_due - now))
        return wait

    async def poll_due_once(self) -> None:
        """Start every due poll and wait for all of them: one deterministic step, for tests and debugging."""
        self.reap(self.clock())
        self._start_due(self.clock())
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def run(self, stop: asyncio.Event) -> None:
        self._wake = asyncio.Event()
        try:
            while not stop.is_set():
                now = self.clock()
                self.reap(now)
                wait = self._start_due(now)
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), wait)
        finally:
            for task in list(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
