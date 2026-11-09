"""The watcher: subscribe through a backend, poll for events, write the changelog, deliver to a sink.

Two backends share one interface. `DaemonBackend` talks to the shared per-user daemon and restarts it when it
disappears. `InProcessBackend` (`--no-daemon`) runs the same engine in a thread of this process, with a private
budget.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import signal
import threading
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from types import FrameType
from typing import Any, Protocol, TypeVar

from glab_cli_extras.daemon.engine import Engine
from glab_cli_extras.daemon.lifecycle import ensure_daemon
from glab_cli_extras.daemon.server import decode_log
from glab_cli_extras.daemon.transport import DaemonFiles, DaemonMismatch, DaemonUnavailable, request
from glab_cli_extras.events import (
    MR_PIPELINE_KINDS,
    THRESHOLD_OPTIONS,
    WATCH_ENDED,
    WATCH_STARTED,
    Event,
    utc_now_iso,
)
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.sync.job_logs import LogContent, LogsMode
from glab_cli_extras.watch.changelog import Changelog
from glab_cli_extras.watch.sinks import Sink, StatusOutput
from glab_cli_extras.watch.timers import Thresholds, WatchTimers
from glab_cli_extras.watch.writer import ResourceWriter

T = TypeVar("T")

FETCH_TIMEOUT = 300.0
"""A fetch may wait for the host's budget and then download a large log."""
EXIT_OK = 0
EXIT_ERROR = 1


class BackendError(RuntimeError):
    pass


class Backend(Protocol):
    def describe(self) -> str: ...

    def subscribe(
        self, client_id: str, ref: ResourceRef, events: frozenset[str], logs: LogsMode, baseline: Any
    ) -> tuple[str, int]: ...

    def poll(self, client_id: str, cursors: dict[str, int]) -> dict[str, Any]: ...

    def unsubscribe(self, client_id: str, subscription_id: str) -> None: ...

    def fetch_job_log(self, host: str, project: str, job_id: int) -> LogContent:
        """A job's log, fetched by the daemon within its budget; ended jobs come from its cache."""
        ...

    def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> dict[str, dict[str, Any]]:
        """These discussions of the MR; one GitLab no longer has is left out."""
        ...

    def fetch_dump(self, ref: ResourceRef) -> dict[str, Any]:
        """A finished pipeline's REST dump and extras, with the daemon's snapshot and ETag of it."""
        ...

    def begin_shutdown(self) -> None:
        """The watcher is stopping: from now on, never start a daemon."""
        ...

    def close(self) -> None: ...


def _subscribe_payload(
    client_id: str, ref: ResourceRef, events: frozenset[str], logs: LogsMode, baseline: Any
) -> dict[str, Any]:
    options: dict[str, Any] = {"events": sorted(events), "logs": logs}
    if baseline is not None:
        options["baseline"] = baseline
    return {"op": "subscribe", "client_id": client_id, "resource": ref.to_wire(), "options": options}


class DaemonBackend:
    def __init__(self, files: DaemonFiles | None = None) -> None:
        self.files = files or DaemonFiles.default()
        self.hello = ensure_daemon(self.files)
        self._shutting_down = False

    def describe(self) -> str:
        return f"pid {self.hello.get('pid')}, endpoint {self.files.describe_endpoint()}, log {self.files.log}"

    def _call(self, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        try:
            response = request(self.files, payload, timeout=timeout)
        except DaemonUnavailable:
            if self._shutting_down:
                raise
            # The daemon exited (idle, crash, or stopped by hand). Start a new one; subscriptions then come back
            # as unknown and the watcher subscribes again.
            self.hello = ensure_daemon(self.files)
            response = request(self.files, payload, timeout=timeout)
        if not response.get("ok"):
            error = str(response.get("error", "the daemon refused the request"))
            if error.startswith("unknown op"):
                raise DaemonMismatch(
                    f"the daemon (pid {self.hello.get('pid')}, glab-cli-extras {self.hello.get('version')}) answered"
                    f" {error}: it runs other code than this glab-watch. Run `glab-watch daemon stop`; the running"
                    " watchers then start a current daemon and subscribe again"
                )
            raise BackendError(error)
        return response

    def subscribe(
        self, client_id: str, ref: ResourceRef, events: frozenset[str], logs: LogsMode, baseline: Any
    ) -> tuple[str, int]:
        response = self._call(_subscribe_payload(client_id, ref, events, logs, baseline))
        return str(response["subscription_id"]), int(response["cursor"])

    def poll(self, client_id: str, cursors: dict[str, int]) -> dict[str, Any]:
        return self._call({"op": "poll", "client_id": client_id, "cursors": cursors})

    def fetch_job_log(self, host: str, project: str, job_id: int) -> LogContent:
        payload = {"op": "fetch_job_log", "host": host, "project": project, "job_id": job_id}
        return decode_log(self._call(payload, FETCH_TIMEOUT).get("log"))

    def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> dict[str, dict[str, Any]]:
        payload = {"op": "fetch_threads", "resource": ref.to_wire(), "ids": sorted(ids)}
        discussions = self._call(payload, FETCH_TIMEOUT).get("discussions")
        return dict(discussions) if isinstance(discussions, dict) else {}

    def fetch_dump(self, ref: ResourceRef) -> dict[str, Any]:
        dump = self._call({"op": "fetch_dump", "resource": ref.to_wire()}, FETCH_TIMEOUT).get("dump")
        return dict(dump) if isinstance(dump, dict) else {}

    def unsubscribe(self, client_id: str, subscription_id: str) -> None:
        """Best effort: one short attempt, never a spawn. If the daemon is gone, the lease expiry cleans up."""
        payload = {"op": "unsubscribe", "client_id": client_id, "subscription_id": subscription_id}
        with contextlib.suppress(DaemonUnavailable, OSError, ValueError):
            request(self.files, payload, timeout=1.0)

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    def close(self) -> None:
        pass


class InProcessBackend:
    """The engine on its own event loop in a daemon thread. Calls hop onto that loop and wait for the result."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._loop = asyncio.new_event_loop()
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="glab-watch-engine", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def main() -> None:
            self._stop = asyncio.Event()
            self._ready.set()
            await self.engine.run(self._stop)

        self._loop.run_until_complete(main())

    def _call(self, fn: Callable[[], T]) -> T:
        future: concurrent.futures.Future[T] = concurrent.futures.Future()

        def invoke() -> None:
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_exception(exc)

        self._loop.call_soon_threadsafe(invoke)
        return future.result(timeout=30)

    def _call_async(self, factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        return asyncio.run_coroutine_threadsafe(factory(), self._loop).result(timeout=FETCH_TIMEOUT)

    def describe(self) -> str:
        return "in-process (--no-daemon), private budget"

    def fetch_job_log(self, host: str, project: str, job_id: int) -> LogContent:
        return self._call_async(lambda: self.engine.fetch_job_log(host, project, job_id))

    def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> dict[str, dict[str, Any]]:
        return self._call_async(lambda: self.engine.fetch_threads(ref, ids))

    def fetch_dump(self, ref: ResourceRef) -> dict[str, Any]:
        return self._call_async(lambda: self.engine.fetch_dump(ref))

    def subscribe(
        self, client_id: str, ref: ResourceRef, events: frozenset[str], logs: LogsMode, baseline: Any
    ) -> tuple[str, int]:
        result = self._call(lambda: self.engine.subscribe(client_id, ref, events, logs, baseline))
        return str(result["subscription_id"]), int(result["cursor"])

    def poll(self, client_id: str, cursors: dict[str, int]) -> dict[str, Any]:
        return self._call(lambda: self.engine.poll(client_id, cursors))

    def unsubscribe(self, client_id: str, subscription_id: str) -> None:
        self._call(lambda: self.engine.unsubscribe(client_id, subscription_id))

    def begin_shutdown(self) -> None:
        pass

    def close(self) -> None:
        stop = self._stop
        if stop is not None:
            self._loop.call_soon_threadsafe(stop.set)
        self._thread.join(timeout=10)


@dataclass(frozen=True)
class WatchSettings:
    ref: ResourceRef
    events: frozenset[str]
    logs: LogsMode = "failed"
    until: str | None = None
    timeout: float | None = None
    thresholds: Thresholds = Thresholds()

    @property
    def subscribed_events(self) -> frozenset[str]:
        """What the daemon is asked for: the reported kinds without the client's own threshold kinds, plus the
        head pipeline for an MR watch whose thresholds read it, so the snapshot carries its status."""
        kinds = self.events - frozenset(THRESHOLD_OPTIONS)
        if self.ref.kind == "mr" and self.events & frozenset(THRESHOLD_OPTIONS):
            kinds |= frozenset({"head_pipeline_changed", *MR_PIPELINE_KINDS})
        return kinds


class Watcher:
    def __init__(
        self,
        settings: WatchSettings,
        backend: Backend,
        sink: Sink,
        changelog: Changelog,
        status: StatusOutput,
        *,
        writer: ResourceWriter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.writer = writer
        self.backend = backend
        self.sink = sink
        self.changelog = changelog
        self.status = status
        self.clock = clock
        self.client_id = uuid.uuid4().hex
        self._stop_reason: str | None = None
        self._wakeup = threading.Event()
        self._subscription: str | None = None
        # The daemon's sequence number of the last event received. It restarts when the daemon restarts, so it is
        # only the poll cursor. Emitted events carry the watcher's own `_seq`, which never goes back.
        self._cursor = 0
        self._seq = 0
        self._snapshot_hash: str | None = None

    def request_stop(self, reason: str = "stopped") -> None:
        self._stop_reason = reason
        self.backend.begin_shutdown()
        self._wakeup.set()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum: int, frame: FrameType | None) -> None:
            self.request_stop(f"signal {signal.Signals(signum).name}")

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _subscribe(self, resubscribe: bool) -> None:
        baseline = None
        if resubscribe:
            stored = self.changelog.load_baseline()
            baseline = stored.snapshot if stored else None
        s = self.settings
        self._subscription, self._cursor = self.backend.subscribe(
            self.client_id, s.ref, s.subscribed_events, s.logs, baseline
        )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _end_event(self, reason: str, **data: Any) -> Event:
        return Event(WATCH_ENDED, self.settings.ref.key, self._next_seq(), utc_now_iso(), {"reason": reason, **data})

    def _timer_event(self, kind: str, data: dict[str, object]) -> Event:
        """A threshold event, numbered with the rest when it is delivered."""
        return Event(kind, self.settings.ref.key, 0, utc_now_iso(), dict(data))

    def _deliver(self, events: list[Event]) -> None:
        if not events:
            return
        self.changelog.append(events)
        self.sink.emit(events)

    def run(self) -> int:
        self._install_signal_handlers()
        if self.settings.timeout is not None:
            deadline: float | None = self.clock() + self.settings.timeout
        else:
            deadline = None
        stored = self.changelog.load_baseline()
        self._snapshot_hash = stored.snapshot_hash if stored else None
        # Continue the changelog's numbering, so events.jsonl stays strictly increasing across watcher runs too.
        self._seq = self.changelog.last_seq()
        resubscribed = False
        exit_code = EXIT_OK
        timers = WatchTimers(self.settings.ref.kind, self.settings.thresholds, self.clock())
        try:
            self._subscribe(resubscribe=False)
            while True:
                if self._stop_reason is not None:
                    self._finish(self._end_event(self._stop_reason))
                    return EXIT_OK
                assert self._subscription is not None
                self.sink.tick()
                response = self.backend.poll(self.client_id, {self._subscription: self._cursor})
                if self._subscription in response.get("unknown", []):
                    self.status.line("daemon restarted, subscribing again")
                    self._subscribe(resubscribe=True)
                    resubscribed = True
                    continue
                snapshot = (response.get("snapshots") or {}).get(self._subscription)
                if snapshot:
                    previous_hash = self._snapshot_hash
                    self._snapshot_hash = str(snapshot["hash"])
                    self.changelog.save_baseline(self._snapshot_hash, snapshot.get("data"))
                    if self.writer is not None:
                        self.writer.snapshot(snapshot.get("data"))
                    timers.snapshot(snapshot.get("data"), self.clock())
                else:
                    previous_hash = self._snapshot_hash
                raw = [Event.from_json(e) for e in (response.get("events") or {}).get(self._subscription) or []]
                for event in raw:
                    self._cursor = max(self._cursor, event.seq)
                events = [e for e in raw if e.kind in self.settings.events]
                if resubscribed and any(e.kind == WATCH_STARTED for e in events):
                    # After a daemon restart, the new watch_started repeats what we already know unless the
                    # snapshot moved; changes in between arrive as normal events, diffed against our stored snapshot.
                    events = [
                        e
                        for e in events
                        if not (e.kind == WATCH_STARTED and e.data.get("snapshot_hash") == previous_hash)
                    ]
                    resubscribed = False
                if self.writer is not None:
                    events = self.writer.attach(events)
                now = self.clock()
                timers.reported(events, now)
                events += [self._timer_event(kind, data) for kind, data in timers.due(now)]
                events = [replace(e, seq=self._next_seq()) for e in events]
                ended = next((e for e in events if e.kind == WATCH_ENDED), None)
                hit = next((e for e in events if self.settings.until and e.kind == self.settings.until), None)
                self._deliver(events)
                if ended is not None:
                    reason = str(ended.data.get("reason"))
                    self.status.line(f"ended: {reason}")
                    return EXIT_ERROR if reason == "not_found" else EXIT_OK
                if hit is not None:
                    self._finish(self._end_event("until", event=hit.kind, seq=hit.seq))
                    return EXIT_OK
                if not self.sink.alive():
                    self._finish(self._end_event("session_gone"))
                    return EXIT_OK
                if deadline is not None and self.clock() >= deadline:
                    self._finish(self._end_event("timeout"))
                    return EXIT_OK
                wait = int(response.get("next_poll_ms", 5000)) / 1000.0
                if deadline is not None:
                    wait = max(0.0, min(wait, deadline - self.clock()))
                timer_in = timers.seconds_until_next(self.clock())
                if timer_in is not None:
                    wait = min(wait, timer_in)
                flush_in = self.sink.seconds_until_flush()
                if flush_in is not None:
                    wait = min(wait, flush_in)
                self._wakeup.wait(wait)
        except (BackendError, DaemonMismatch, DaemonUnavailable) as exc:
            if self._stop_reason is not None:
                # Stopped while a request was in flight, and the daemon is gone: end like any other stop.
                self._finish(self._end_event(self._stop_reason))
                return EXIT_OK
            self.status.line(f"error: {exc}")
            exit_code = EXIT_ERROR
            return exit_code
        finally:
            self.backend.begin_shutdown()
            if self._subscription is not None:
                with contextlib.suppress(BackendError, DaemonUnavailable, OSError, TimeoutError):
                    self.backend.unsubscribe(self.client_id, self._subscription)
            self.sink.close()

    def _finish(self, event: Event) -> None:
        self._deliver([event])
        self.status.line(f"ended: {event.data.get('reason')}")


def default_backend(no_daemon: bool) -> Backend:
    if no_daemon:
        return InProcessBackend(Engine())
    return DaemonBackend()
