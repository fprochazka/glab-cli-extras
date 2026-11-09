"""The watch client's writer: it turns what the daemon reports into files in the resource dir.

The daemon writes nothing. Each poll response can carry a new snapshot, which goes to `state.json`; for an MR, every
thread whose notes changed since its file was written is fetched through the daemon and rewritten. A `job_finished`
whose log the watcher wants gets the log fetched through the daemon and written into the job's pipeline dir. The
events then point at those files in `artifacts`, so the agent that reads an event can open the file at once.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from glab_cli_extras.events import WATCH_STARTED, Event, utc_now_iso
from glab_cli_extras.gitlab.client import ApiError
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.serde import from_jsonable
from glab_cli_extras.store.job_logs import has_log, write_log
from glab_cli_extras.store.layout import THREADS_DIR, ensure_readme, pipeline_dir, resource_dir
from glab_cli_extras.store.locks import LockTimeout
from glab_cli_extras.store.state import load_state, locked, put_snapshot, save_state
from glab_cli_extras.store.threads import ThreadStore
from glab_cli_extras.sync.job_logs import LogsMode, wants_log
from glab_cli_extras.sync.mr import MrSnapshot, thread_sig
from glab_cli_extras.watch.sinks import StatusOutput

if TYPE_CHECKING:
    from glab_cli_extras.watch.client import Backend

COMMENT_KINDS = frozenset({"thread_created", "thread_updated", "thread_resolved", "thread_unresolved"})
LOCK_TIMEOUT = 60.0


class WriteError(RuntimeError):
    pass


class ResourceWriter:
    def __init__(self, base: Path, ref: ResourceRef, backend: Backend, logs: LogsMode, status: StatusOutput) -> None:
        self.base = base
        self.ref = ref
        self.backend = backend
        self.logs = logs
        self.status = status
        self.dir = resource_dir(base, ref)
        self._mr: MrSnapshot | None = None
        self._pending_threads: set[str] = set()

    def _fail(self, what: str, exc: BaseException) -> None:
        self.status.line(f"could not write {what}: {exc}")

    # ------------------------------------------------------------------------------------------------------------
    # Snapshots

    def snapshot(self, data: Any) -> None:
        """Write a new snapshot to `state.json`, and bring an MR's thread files up to date with it."""
        if data is None or self.ref.kind not in ("mr", "pipeline"):
            return
        try:
            with locked(self.dir, LOCK_TIMEOUT):
                ensure_readme(self.dir, self.ref.kind)
                state = load_state(self.dir, self.ref.key)
                if self.ref.kind == "mr" and isinstance(data, dict) and data.get("threads") is None:
                    # This watch does not follow threads: keep what another writer knows about them.
                    previous = state.get("snapshot")
                    if isinstance(previous, dict) and previous.get("threads") is not None:
                        data = {**data, "threads": previous["threads"]}
                put_snapshot(state, data, utc_now_iso())
                save_state(self.dir, state)
        except (LockTimeout, OSError) as exc:
            self._fail("state.json", exc)
        if self.ref.kind == "mr":
            try:
                snap = from_jsonable(MrSnapshot, data)
            except ValueError as exc:
                self._fail("the thread files", exc)
                return
            self._mr = snap
            self._sync_threads(set())

    def _thread_store(self) -> ThreadStore:
        assert self.ref.id is not None
        url = self._mr.web_url if self._mr is not None else None
        fallback = f"https://{self.ref.host}/{self.ref.project}/-/merge_requests/{self.ref.id}"
        return ThreadStore(self.dir / THREADS_DIR, url or fallback)

    def _sync_threads(self, force: set[str]) -> None:
        snap = self._mr
        if snap is None or snap.threads is None:
            return
        try:
            with locked(self.dir, LOCK_TIMEOUT):
                store = self._thread_store()
                current = {t.id: thread_sig(t) for t in snap.threads}
                for gone in store.discussion_ids() - set(current):
                    store.remove(gone)
                wanted = {
                    tid
                    for tid, sig in current.items()
                    if store.sig(tid) != sig or store.path_for(tid) is None or tid in force
                } | (self._pending_threads & set(current))
                if not wanted:
                    self._pending_threads.clear()
                    return
                discussions = self.backend.fetch_threads(self.ref, wanted)
                for tid, discussion in discussions.items():
                    store.write(discussion, current.get(tid))
                # A thread GitLab did not return (rewritten for a moment after a force-push) keeps its old file.
                self._pending_threads = wanted - set(discussions)
        except (LockTimeout, OSError, ApiError, RuntimeError, ValueError) as exc:
            self._fail("the thread files", exc)

    # ------------------------------------------------------------------------------------------------------------
    # Events

    def attach(self, events: list[Event]) -> list[Event]:
        """Write the files the events announce and point the events at them."""
        if self._pending_threads:
            self._sync_threads(set())
        attached = []
        for event in events:
            if event.kind == WATCH_STARTED:
                event = self._started(event)
            elif event.kind in COMMENT_KINDS and self.ref.kind == "mr":
                event = self._thread_event(event)
            elif event.kind == "job_finished":
                event = self._job_finished(event)
            attached.append(event)
        return attached

    def _started(self, event: Event) -> Event:
        data = dict(event.data)
        summary = dict(data.get("summary") or {})
        summary["dir"] = str(self.dir)
        if isinstance(summary.get("threads"), dict):
            summary["threads"] = {**summary["threads"], "dir": str(self.dir / THREADS_DIR)}
        data["summary"] = summary
        return replace(event, data=data)

    def _thread_event(self, event: Event) -> Event:
        path = self._thread_store().path_for(str(event.data.get("discussion_id")))
        return replace(event, artifacts=(str(path),)) if path is not None else event

    def _job_finished(self, event: Event) -> Event:
        data = event.data
        status = str(data.get("status", ""))
        if not wants_log(self.logs, status):
            return event
        try:
            job_id = int(data["job_id"])
            pipeline_id = int(data["pipeline_id"])
        except (KeyError, TypeError, ValueError):
            return event
        project = str(data.get("project") or self.ref.project)
        directory = pipeline_dir(self.base, self.ref.host, project, pipeline_id)
        resource = ResourceRef("pipeline", self.ref.host, project, pipeline_id).key
        try:
            with locked(directory, LOCK_TIMEOUT):
                ensure_readme(directory, "pipeline")
                state = load_state(directory, resource)
                path = has_log(directory, state, job_id, full=False)
                truncated = bool(((state.get("logs") or {}).get(str(job_id)) or {}).get("truncated"))
                if path is None:
                    content = self.backend.fetch_job_log(self.ref.host, project, job_id)
                    path = write_log(
                        directory,
                        state,
                        job_id=job_id,
                        stage=data.get("stage"),
                        name=str(data.get("name", "")),
                        status=status,
                        content=content,
                    )
                    truncated = content.truncated
                    save_state(directory, state)
        except (LockTimeout, OSError, ApiError, RuntimeError, ValueError) as exc:
            self._fail(f"the log of job {job_id}", exc)
            return replace(event, data={**data, "log_error": str(exc)})
        return replace(event, data={**data, "log_truncated": truncated}, artifacts=(str(path),))
