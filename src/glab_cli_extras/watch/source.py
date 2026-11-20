"""glab-watch's pollers as a cli-watch-kit source: the daemon polls GitLab through them.

- One account per GitLab host, so each host has its own budget. glab polls with the user's glab login, so no params
  travel with a subscription.
- One target per poll call, as before: each poller sends its own requests (an MR its GraphQL query, a pipeline its
  ETag probe). The stateful glab `Poller` of a resource lives in `PollItem.state`, with its ETag and the job logs it
  holds back until GitLab archived them.
- `--logs` is a source option, merged over every subscriber of a pipeline the way the old daemon merged it.
- The fetch ops (a job log, thread discussions, a finished pipeline's dump) run in the daemon within the host's
  budget, from caches of their own size, so every watcher of one resource costs one fetch.
- `diff` reports the gap of a watcher that resubscribes to a resource the daemon already polls again. It is pure, so
  it cannot ask GitLab whether a push was forced: its `pushed` has `forced: null`.
"""

from __future__ import annotations

import base64
import json
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

from cli_watch_kit.budget import Budget, DimensionSpec
from cli_watch_kit.daemon.cache import ByteBoundedCache
from cli_watch_kit.events import RawEvent
from cli_watch_kit.source import (
    NotFound,
    OpContext,
    OpHandler,
    Params,
    PollContext,
    PollItem,
    PollOutcome,
    RateLimited,
    Source,
    SourceError,
    Target,
    Transient,
    Unauthorized,
)

from glab_cli_extras.events import CATALOG
from glab_cli_extras.gitlab import client as gl
from glab_cli_extras.gitlab.budget import DEFAULT_BURST, DEFAULT_FIXED_PER_MINUTE, DEFAULT_SHARE, HostBudget
from glab_cli_extras.gitlab.glab import GlabNotFoundError, GlabRunner, run_glab
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.resources import POLLERS
from glab_cli_extras.resources import base as glab_base
from glab_cli_extras.resources import branch as branch_res
from glab_cli_extras.resources import mr as mr_res
from glab_cli_extras.resources import mr_list as mr_list_res
from glab_cli_extras.resources import my_mrs as my_mrs_res
from glab_cli_extras.resources import pipeline as pipeline_res
from glab_cli_extras.resources.mr import prefetch_threads, thread_cache_key
from glab_cli_extras.resources.pipeline import PipelinePoller, log_cache_key, prefetch_log
from glab_cli_extras.serde import from_jsonable, stable_hash, to_jsonable
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

REQUESTS = "requests"
CACHE_BYTES = 64 * 1024 * 1024
DUMP_CACHE_BYTES = 16 * 1024 * 1024
BASE_INTERVALS: Mapping[str, float] = {
    "mr": mr_res.INTERVAL,
    "pipeline": pipeline_res.INTERVAL_RUNNING,
    "mrs": mr_list_res.INTERVAL,
    "branch": branch_res.PUSH_INTERVAL,
    "my-mrs": my_mrs_res.INTERVAL,
}
"""The interval of a kind before its first poll; after it, the poller's own `next_delay` decides."""


def target_of(ref: ResourceRef) -> Target:
    """The kit target of a glab ref. Its key is the ref's key, `mr gitlab.com/group/project!123`."""
    return Target(ref.kind, ref.short, ref.to_wire())


def ref_of(target: Target) -> ResourceRef:
    return ResourceRef.from_wire(dict(target.data))


class KitHostBudget(HostBudget):
    """glab's `HostBudget` API over the account's kit `Budget`, for the `GitLabClient` the daemon polls with.

    The engine paces, blocks a host on a 429 and backs off; this only turns GitLab's `RateLimit-*` headers into kit
    readings and charges each request to the poll that sent it (a 304 at a tenth), so the engine refunds its estimate.
    The glab fields stay up to date, because the pollers read `premium_level` from them.
    """

    def __init__(self, budget: Budget) -> None:
        super().__init__()
        self.kit = budget

    def observe(self, headers: Mapping[str, str], now: float) -> None:
        super().observe(headers, now)
        ceiling = self.limit_per_minute / 60.0 if self.limit_per_minute else None
        self.kit.observe(REQUESTS, now, remaining=self.remaining, reset_at=self.reset_at, ceiling_per_second=ceiling)

    def charge(self, cost: float, now: float) -> None:
        poll = PollContext.current()
        if poll is not None:
            poll.spend({REQUESTS: cost})
        else:
            self.kit.charge({REQUESTS: cost}, now)

    def can_spend(self, cost: float, now: float) -> bool:
        return self.kit.can_spend({REQUESTS: cost}, now)

    def wait_time(self, cost: float, now: float) -> float:
        return self.kit.wait_time({REQUESTS: cost}, now)

    def on_rate_limited(self, retry_after: float | None, now: float) -> None:
        self.rate_limited_count += 1
        poll = PollContext.current()
        if poll is None:
            # A fetch op hit the limit.
            self.kit.on_rate_limited(retry_after, now)
        else:
            # Also a 429 the poller swallows, such as a held-back job log it tries again later: the engine blocks the
            # host after the poll and does not count it as a success.
            poll.rate_limited(retry_after)

    def on_success(self) -> None:
        pass


def kit_error(exc: Exception) -> SourceError:
    """A GitLab error as the kit's error of the same kind."""
    if isinstance(exc, gl.NotFound):
        return NotFound(str(exc))
    if isinstance(exc, gl.RateLimited):
        return RateLimited(str(exc), exc.retry_after)
    if isinstance(exc, gl.Unauthorized):
        return Unauthorized(str(exc))
    return Transient(str(exc) or type(exc).__name__)


def shortest_interval(options: Sequence[Mapping[str, Any]]) -> float | None:
    """The shortest `--interval` of the subscribers of a `my-mrs` watch, None when nobody sent one."""
    intervals = [float(o["interval"]) for o in options if isinstance(o.get("interval"), int | float)]
    return min(intervals) if intervals else None


def logs_mode(options: Sequence[Mapping[str, Any]]) -> LogsMode:
    """The merged `--logs` mode of every subscriber; `none` when nobody sent one (an MR watch)."""
    modes = [cast(LogsMode, o["logs"]) for o in options if o.get("logs") in LOGS_MODES]
    return merge_logs_modes(modes) if modes else "none"


def encode_log(content: LogContent) -> dict[str, Any]:
    """A log on the wire: its bytes base64-encoded, because a trace need not be valid UTF-8."""
    body = base64.b64encode(content.body).decode("ascii")
    return {"body_b64": body, "truncated": content.truncated, "size": content.size}


def decode_log(data: Any) -> LogContent:
    if not isinstance(data, dict) or not isinstance(data.get("body_b64"), str):
        raise ValueError("the daemon sent no log")
    size = data.get("size")
    known_size = size if isinstance(size, int) else None
    return LogContent(base64.b64decode(data["body_b64"]), bool(data.get("truncated")), known_size)


class GlabSource(Source[Any]):
    catalog = CATALOG
    dimensions = {
        REQUESTS: DimensionSpec(per_minute=DEFAULT_FIXED_PER_MINUTE, burst=DEFAULT_BURST, share=DEFAULT_SHARE)
    }
    baseline_wait = 0.0

    def __init__(self, runner: GlabRunner = run_glab, clock: Callable[[], float] = time.time) -> None:
        self.runner = runner
        self.clock = clock
        self.clients: dict[str, gl.GitLabClient] = {}
        self.push_feeds = glab_base.PushFeedCache()
        self.logs: ByteBoundedCache[LogContent] = ByteBoundedCache(CACHE_BYTES)
        self.threads: ByteBoundedCache[dict[str, Any]] = ByteBoundedCache(CACHE_BYTES)
        self.dumps: ByteBoundedCache[dict[str, Any]] = ByteBoundedCache(DUMP_CACHE_BYTES)
        # The poller of each resource, which lives in its `PollItem.state`; weak, so it goes with its resource.
        self.pollers: weakref.WeakValueDictionary[str, glab_base.Poller[Any]] = weakref.WeakValueDictionary()
        self.ops: Mapping[str, OpHandler] = {
            "fetch_job_log": self._op_fetch_job_log,
            "fetch_threads": self._op_fetch_threads,
            "fetch_dump": self._op_fetch_dump,
        }

    # ------------------------------------------------------------------------------------------------------------
    # The Source protocol

    async def account(self, target: Target, params: Params) -> str:
        return ref_of(target).host

    def client(self, host: str, budget: Budget) -> gl.GitLabClient:
        client = self.clients.get(host)
        if client is None:
            client = gl.GitLabClient(host, runner=self.runner, budget=KitHostBudget(budget), clock=self.clock)
            self.clients[host] = client
        return client

    def check_options(self, target: Target, options: Mapping[str, Any]) -> Mapping[str, Any]:
        if target.kind == "my-mrs":
            interval = options.get("interval", my_mrs_res.INTERVAL)
            valid = isinstance(interval, int | float) and not isinstance(interval, bool)
            if set(options) - {"interval"} or not valid or interval < my_mrs_res.MIN_INTERVAL:
                raise ValueError(f"options.interval must be a number of seconds, at least {my_mrs_res.MIN_INTERVAL:g}")
            return {"interval": float(interval)}
        logs = options.get("logs", "failed")
        if logs not in LOGS_MODES or set(options) - {"logs"}:
            raise ValueError(f"options.logs must be one of {', '.join(LOGS_MODES)}")
        return {"logs": logs}

    def interval(self, target: Target, snapshot: Any) -> float:
        """The poller's own delay, which knows the options, the host's premium level and the pipeline's state, so
        the budget's stretch and the error backoff see the rate the resource really polls at."""
        poller = self.pollers.get(target.key)
        if poller is not None:
            return poller.next_delay()
        return BASE_INTERVALS.get(target.kind, 30.0)

    def cost(self, batch: Sequence[PollItem[Any]]) -> Mapping[str, float]:
        total = 0.0
        for item in batch:
            poller = item.state.get("poller")
            total += poller.expected_cost() if isinstance(poller, glab_base.Poller) else 1.0
        return {REQUESTS: total}

    def _context(self, host: str, budget: Budget) -> glab_base.PollContext:
        return glab_base.PollContext(self.client(host, budget), self.clock, self.push_feeds, self.logs, self.threads)

    def _poller(self, item: PollItem[Any], ctx: PollContext) -> glab_base.Poller[Any]:
        poller = item.state.get("poller")
        if not isinstance(poller, glab_base.Poller):
            ref = ref_of(item.target)
            poller = POLLERS[ref.kind](ref, self._context(ref.host, ctx.budget), glab_base.PollerOptions(frozenset()))
            item.state["poller"] = poller
            self.pollers[item.key] = poller
        if poller.snapshot is None and item.snapshot is not None:
            # A baseline a resubscribing watcher kept: the first poll reports what changed since.
            poller.snapshot = item.snapshot
        poller.set_options(
            glab_base.PollerOptions(item.events, logs_mode(item.options), shortest_interval(item.options))
        )
        return poller

    async def poll(self, batch: Sequence[PollItem[Any]], ctx: PollContext) -> Mapping[str, PollOutcome[Any]]:
        outcomes: dict[str, PollOutcome[Any]] = {}
        for item in batch:
            poller = self._poller(item, ctx)
            try:
                raws = await poller.poll()
            except (gl.ApiError, GlabNotFoundError) as exc:
                outcomes[item.key] = PollOutcome(error=kit_error(exc))
                continue
            outcomes[item.key] = PollOutcome(
                poller.snapshot,
                raws,
                ended=poller.terminal_reason,
                end_data=dict(poller.terminal_data),
                next_delay=None if poller.terminal else poller.next_delay(),
            )
        return outcomes

    def diff(self, target: Target, previous: Any, current: Any, events: frozenset[str]) -> Sequence[RawEvent]:
        """The events between a resubscribing watcher's baseline and the current snapshot.

        MR and pipeline report everything their poll would. A branch reports its pipelines, and an MR list nothing:
        their pushes and changes come from GitLab's event feed and MR list, which two snapshots do not hold. The
        dashboard reports everything but the MRs that left it, which the next poll reports.
        """
        raws: list[RawEvent] = []
        if isinstance(previous, MrSnapshot) and isinstance(current, MrSnapshot):
            if previous.threads is None and current.threads is not None:
                previous = replace(previous, threads=current.threads)
            if previous.external_statuses is None and current.external_statuses is not None:
                previous = replace(previous, external_statuses=current.external_statuses)
            for raw in mr_res.diff_mr(previous, current):
                if raw.kind == "pushed" and raw.data.get("from"):
                    raw = replace(raw, data={**raw.data, "forced": None})
                raws.append(raw)
        elif isinstance(previous, PipelineSnapshot) and isinstance(current, PipelineSnapshot):
            raws = pipeline_res.diff_pipeline(previous, current)
        elif isinstance(previous, my_mrs_res.MyMrsSnapshot) and isinstance(current, my_mrs_res.MyMrsSnapshot):
            # What became of an MR that left the open set needs a request; the next poll reports it.
            _, raws = my_mrs_res.diff_my_mrs(previous, current, None)
        elif isinstance(previous, branch_res.BranchSnapshot) and isinstance(current, branch_res.BranchSnapshot):
            ref = ref_of(target)
            raws = branch_res.diff_pipelines(previous.pipelines, current.pipelines, f"https://{ref.host}/{ref.project}")
        return [raw for raw in raws if raw.kind in events]

    def summary(self, target: Target, snapshot: Any) -> dict[str, Any]:
        if snapshot is None:
            return {}
        return POLLERS[target.kind].summarize(ref_of(target), snapshot)

    def encode(self, snapshot: Any) -> Any:
        return to_jsonable(snapshot)

    def decode(self, target: Target, data: Any) -> Any:
        return from_jsonable(POLLERS[target.kind].snapshot_type, data)

    def status(self) -> dict[str, Any]:
        return {
            "hosts": {host: c.stats.describe() for host, c in sorted(self.clients.items())},
            "caches": {
                "logs": self.logs.describe(),
                "threads": self.threads.describe(),
                "dumps": self.dumps.describe(),
            },
        }

    # ------------------------------------------------------------------------------------------------------------
    # Fetches for the watchers. The daemon writes no files: it hands the content over, the watcher writes it.

    async def _op(self, call: Callable[[], Any]) -> Any:
        try:
            return await call()
        except (gl.ApiError, GlabNotFoundError) as exc:
            raise kit_error(exc) from exc

    def _known_job(self, op: OpContext, host: str, job_id: int) -> JobState | None:
        for res in op.engine.resources.values():
            snap = res.snapshot
            if res.account == host and isinstance(snap, PipelineSnapshot):
                for job in snap.jobs:
                    if job.id == job_id:
                        return job
        return None

    async def _op_fetch_job_log(self, op: OpContext, request: dict[str, Any]) -> dict[str, Any]:
        """The log of a job, from the cache when a poller or another watcher already fetched it. Only the log of an
        ended job is cached: a running job's log still grows."""
        host, project, job_id = str(request.get("host")), str(request.get("project")), request.get("job_id")
        if not isinstance(job_id, int):
            raise ValueError("job_id must be an integer")
        context = self._context(host, op.engine.account(host).budget)
        job = self._known_job(op, host, job_id)
        if job is not None and job.status in FINAL_JOB_STATUSES:
            cached = self.logs.get(log_cache_key(host, job_id))
            if cached is not None:
                return {"log": encode_log(cached)}
            await op.engine.await_budget(host, {REQUESTS: 1.0}, charge=False)
            return {"log": encode_log(await self._op(lambda: prefetch_log(context, job)))}
        await op.engine.await_budget(host, {REQUESTS: 1.0}, charge=False)
        size = job.trace_size if job else None
        content = await self._op(lambda: fetch_log_content(context.client, project, job_id, size))
        return {"log": encode_log(content)}

    async def _op_fetch_threads(self, op: OpContext, request: dict[str, Any]) -> dict[str, Any]:
        """These discussions of a watched MR in their current version; a thread GitLab no longer has is left out."""
        ref = ResourceRef.from_wire(request.get("resource"))
        ids = request.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("ids must be a list of discussion ids")
        wanted = set(ids)
        context = self._context(ref.host, op.engine.account(ref.host).budget)
        res = op.engine.find(ref.host, target_of(ref))
        snap = res.snapshot if res is not None else None
        if isinstance(snap, MrSnapshot) and snap.threads is not None:
            sigs = {t.id: thread_sig(t) for t in snap.threads}
            if any(self.threads.get(thread_cache_key(ref, i, sigs.get(i, ""))) is None for i in wanted):
                await op.engine.await_budget(ref.host, {REQUESTS: 1.0}, charge=False)
            return {"discussions": await self._op(lambda: prefetch_threads(context, ref, snap, wanted))}
        await op.engine.await_budget(ref.host, {REQUESTS: 1.0}, charge=False)
        return {"discussions": await self._op(lambda: fetch_discussions(context.client, ref, wanted))}

    async def _op_fetch_dump(self, op: OpContext, request: dict[str, Any]) -> dict[str, Any]:
        """A finished pipeline's REST dump and extras, with the snapshot and ETag the daemon holds for it. Cached by
        that snapshot, so every watcher of one finished pipeline costs one fetch."""
        ref = ResourceRef.from_wire(request.get("resource"))
        res = op.engine.find(ref.host, target_of(ref))
        snap = res.snapshot if res is not None and isinstance(res.snapshot, PipelineSnapshot) else None
        key = ("dump", ref.key, stable_hash(snap) if snap is not None else None)
        cached = self.dumps.get(key) if snap is not None else None
        if cached is not None:
            return {"dump": cached}
        await op.engine.await_budget(ref.host, {REQUESTS: 1.0}, charge=False)
        client = self.client(ref.host, op.engine.account(ref.host).budget)
        dump: dict[str, Any] = await self._op(lambda: fetch_dump(client, ref))
        if snap is not None and res is not None:
            poller = res.state.get("poller")
            dump["snapshot"] = to_jsonable(snap)
            dump["snapshot_hash"] = stable_hash(snap)
            dump["etag"] = poller.etag if isinstance(poller, PipelinePoller) else None
            self.dumps.put(key, dump, len(json.dumps(dump)))
        return {"dump": dump}
