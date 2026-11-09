"""The watch client's writer: it turns what the daemon reports into files in the resource dir.

The daemon writes nothing. Each poll response can carry a new snapshot, which goes to `state.json`; for an MR, every
thread whose notes changed since its file was written is fetched through the daemon and rewritten. A `job_finished`
whose log the watcher wants gets the log fetched through the daemon and written into the job's pipeline dir. The
events then point at those files in `artifacts`, so the agent that reads an event can open the file at once.
"""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from glab_cli_extras.daemon.transport import DaemonMismatch
from glab_cli_extras.events import WATCH_DEGRADED, WATCH_ENDED, WATCH_STARTED, Event, utc_now_iso
from glab_cli_extras.gitlab.client import ApiError
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.paths import base_dir
from glab_cli_extras.pipeline.dump import write_dump
from glab_cli_extras.serde import from_jsonable
from glab_cli_extras.store.job_logs import has_log, write_log, write_placeholder
from glab_cli_extras.store.layout import THREADS_DIR, ensure_readme, pipeline_dir, resource_dir
from glab_cli_extras.store.locks import LockTimeout
from glab_cli_extras.store.state import load_state, locked, put_snapshot, save_state
from glab_cli_extras.store.threads import ThreadStore, render_thread
from glab_cli_extras.sync.job_logs import FINAL_JOB_STATUSES, LogsMode, wants_log
from glab_cli_extras.sync.mr import MrSnapshot, snapshot_users, thread_sig
from glab_cli_extras.sync.pipeline import JobState, PipelineSnapshot
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
        self._default_base = base == base_dir()
        self._mr: MrSnapshot | None = None
        self._pending_threads: set[str] = set()
        self._mismatch: str | None = None
        self._mismatch_reported = False

    def _fail(self, what: str, exc: BaseException) -> None:
        if isinstance(exc, DaemonMismatch) and self._mismatch is None:
            # Every later fetch fails the same way: one watch_degraded event says why, instead of an error field on
            # each event that needed a file.
            self._mismatch = str(exc)
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
        if self.ref.kind == "pipeline":
            try:
                self._placeholders(from_jsonable(PipelineSnapshot, data))
            except ValueError as exc:
                self._fail("the job log placeholders", exc)
        if self.ref.kind == "mr":
            try:
                snap = from_jsonable(MrSnapshot, data)
            except ValueError as exc:
                self._fail("the thread files", exc)
                return
            self._mr = snap
            self._sync_threads(set())

    def inspect_command(self, project: str, pipeline_id: int) -> str:
        """The glab-pipeline command that downloads every log of a pipeline into the same dir."""
        parts = [
            "glab-pipeline",
            "inspect",
            "--pipeline-url",
            f"https://{self.ref.host}/{project}/-/pipelines/{pipeline_id}",
        ]
        if not self._default_base:
            parts += ["--output-dir", str(pipeline_dir(self.base, self.ref.host, project, pipeline_id))]
        parts.append("--logs=all")
        return shlex.join(parts)

    def _placeholders(self, snap: PipelineSnapshot) -> None:
        """Every job of the tree gets a file at its log's path: a placeholder until its real log is written."""
        by_pipeline: dict[tuple[str, int], list[JobState]] = {}
        for job in snap.jobs:
            by_pipeline.setdefault((job.project, job.pipeline_id), []).append(job)
        for (project, pipeline_id), jobs in by_pipeline.items():
            directory = pipeline_dir(self.base, self.ref.host, project, pipeline_id)
            resource = ResourceRef("pipeline", self.ref.host, project, pipeline_id).key
            try:
                with locked(directory, LOCK_TIMEOUT):
                    ensure_readme(directory, "pipeline")
                    state = load_state(directory, resource)
                    command = self.inspect_command(project, pipeline_id)
                    for job in jobs:
                        write_placeholder(
                            directory,
                            state,
                            job_id=job.id,
                            stage=job.stage,
                            name=job.name,
                            status=job.status,
                            logs=self.logs,
                            command=command,
                            coming=job.status in FINAL_JOB_STATUSES and wants_log(self.logs, job.status),
                        )
                    save_state(directory, state)
            except (LockTimeout, OSError) as exc:
                self._fail(f"the job log placeholders in {directory}", exc)

    def _thread_store(self) -> ThreadStore:
        return ThreadStore(self.dir / THREADS_DIR)

    def _mr_url(self) -> str:
        url = self._mr.web_url if self._mr is not None else None
        return url or f"https://{self.ref.host}/{self.ref.project}/-/merge_requests/{self.ref.id}"

    def _sync_threads(self, force: set[str]) -> None:
        snap = self._mr
        if snap is None or snap.threads is None:
            return
        try:
            with locked(self.dir, LOCK_TIMEOUT):
                store = self._thread_store()
                current = {t.id: thread_sig(t) for t in snap.threads}
                gone = store.discussion_ids() - set(current)
                for discussion_id in gone:
                    store.remove(discussion_id)
                if gone:
                    store.save()
                wanted = {
                    tid
                    for tid, sig in current.items()
                    if store.sig(tid) != sig or store.path_for(tid) is None or tid in force
                } | (self._pending_threads & set(current))
                if not wanted:
                    self._pending_threads.clear()
                    return
                discussions = self.backend.fetch_threads(self.ref, wanted)
                users = snapshot_users(snap)
                for tid, discussion in discussions.items():
                    filename, content = render_thread(discussion, self._mr_url(), users)
                    # The watch renders published notes only; `glab-discussion read` adds the user's draft replies
                    # back, because their empty `drafts_sig` no longer matches.
                    store.write(tid, filename, content, sig=current.get(tid), drafts_sig="")
                store.save()
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
            elif event.kind == "finished":
                event = self._pipeline_finished(event)
            elif event.kind == WATCH_ENDED:
                event = self._watch_ended(event)
            attached.append(event)
        if self._mismatch is not None and not self._mismatch_reported:
            self._mismatch_reported = True
            data = {"reason": "daemon_mismatch", "message": self._mismatch}
            degraded = Event(WATCH_DEGRADED, self.ref.key, 0, utc_now_iso(), data)
            ended = [i for i, e in enumerate(attached) if e.kind == WATCH_ENDED]
            attached.insert(ended[0] if ended else len(attached), degraded)
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

    def _pipeline_finished(self, event: Event) -> Event:
        """Write the dump glab-pipeline inspect writes, so a finished watch leaves the same files."""
        data = event.data
        try:
            pipeline_id = int(data["pipeline_id"])
        except (KeyError, TypeError, ValueError):
            return event
        project = str(data.get("project") or self.ref.project)
        try:
            # The failed jobs' logs come with their own job_finished events: GitLab archives a trace a few seconds
            # after the job ends, and the daemon waits for that. Until then their files are placeholders.
            directory = self.write_finished_dump(project, pipeline_id, fetch_logs=False)
        except (LockTimeout, OSError, ApiError, RuntimeError, ValueError, KeyError) as exc:
            self._fail(f"the dump of pipeline {pipeline_id}", exc)
            if isinstance(exc, DaemonMismatch):
                return event
            return replace(event, data={**data, "dump_error": str(exc)})
        return replace(event, data={**data, "dir": str(directory)}, artifacts=(str(directory / "summary.txt"),))

    def _watch_ended(self, event: Event) -> Event:
        """When a pipeline watch ends, the dump is complete: every wanted log is fetched and the summary rewritten.
        This also covers a pipeline that had already finished when the watch started and so reported no `finished`.
        The daemon serves the dump and the logs from its cache, so this costs no new GitLab request."""
        if self.ref.kind != "pipeline" or event.data.get("reason") != "finished":
            return event
        assert self.ref.id is not None
        try:
            directory = self.write_finished_dump(self.ref.project, self.ref.id, fetch_logs=True)
        except (LockTimeout, OSError, ApiError, RuntimeError, ValueError, KeyError) as exc:
            self._fail(f"the dump of pipeline {self.ref.id}", exc)
            return event
        return replace(event, artifacts=(str(directory / "summary.txt"),))

    def write_finished_dump(self, project: str, pipeline_id: int, *, fetch_logs: bool) -> Path:
        """Fetch the dump through the daemon, with `fetch_logs` also the wanted logs still missing, and write it all."""
        ref = ResourceRef("pipeline", self.ref.host, project, pipeline_id)
        directory = pipeline_dir(self.base, self.ref.host, project, pipeline_id)
        dump = self.backend.fetch_dump(ref)
        with locked(directory, LOCK_TIMEOUT):
            ensure_readme(directory, "pipeline")
            state = load_state(directory, ref.key)
            for job in dump.get("jobs") or [] if fetch_logs else []:
                status = str(job.get("status", ""))
                if not wants_log(self.logs, status) or status not in FINAL_JOB_STATUSES:
                    continue
                if has_log(directory, state, int(job["id"]), full=False) is None:
                    content = self.backend.fetch_job_log(self.ref.host, project, int(job["id"]))
                    write_log(
                        directory,
                        state,
                        job_id=int(job["id"]),
                        stage=job.get("stage"),
                        name=str(job.get("name", "")),
                        status=status,
                        content=content,
                    )
            write_dump(directory, state, ref, dump, self.logs, self.inspect_command(project, pipeline_id))
            save_state(directory, state)
        return directory

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
            if isinstance(exc, DaemonMismatch):
                return event
            return replace(event, data={**data, "log_error": str(exc)})
        return replace(event, data={**data, "log_truncated": truncated}, artifacts=(str(path),))
