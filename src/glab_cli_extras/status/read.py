"""`glab-mr-status`: read merge requests once, report what changed since this reader's last read, and store it.

Each MR is read with the REST MR object, the newest note, the default branch (once) and, when the MR moved, its
approvals. The threads come through the `glab-discussion read` code path and the head pipeline's dump through the
`glab-pipeline inspect` code path, into the dirs every command shares.

The reader's baseline is `status-baseline.json` in the MR's dir, apart from the watcher's `baseline.json`, so a
one-shot read never swallows a change a running watcher has not reported yet, and the other way round. The output of
a run is printed and flushed before the baseline advances: a run killed in between reports the same change again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from glab_cli_extras.discussion import read as discussion_read
from glab_cli_extras.discussion.models import MrContext
from glab_cli_extras.events import utc_now_iso
from glab_cli_extras.gitlab.client import ApiError, GitLabClient, encode_project
from glab_cli_extras.gitlab.glab import GlabNotFoundError
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.gitlab.sync_api import GlabApiError
from glab_cli_extras.pipeline.inspect import InspectOptions, run_inspect
from glab_cli_extras.status.diff import (
    APPROVALS_UNAVAILABLE,
    Change,
    Details,
    approvals_from_api,
    details_lines,
    diff_snapshots,
    external_statuses,
    failed_job_lines,
    note_from_api,
    pipeline_job_names,
    scorecard,
    settle,
    snapshot_from_mr,
    snapshot_lines,
)
from glab_cli_extras.store.atomic import atomic_write_json
from glab_cli_extras.store.layout import ensure_readme, mr_dir, pipeline_dir
from glab_cli_extras.store.locks import LockTimeout
from glab_cli_extras.store.state import load_state, locked, save_state

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_READ_FAILED = 5

BASELINE_FILE = "status-baseline.json"
EVENTS_LOG = "status-events.log"
MR_FILE = "mr.json"
EXTERNAL_STATUSES_FILE = "external-statuses.json"

READ_ERRORS = (ApiError, GlabApiError, GlabNotFoundError, LockTimeout, OSError, ValueError)
"""What a read of one MR can fail with. The other MRs of the set are still read."""


@dataclass(frozen=True)
class ReadOptions:
    base: Path
    comments: bool
    pipeline: bool
    reset: bool = False


@dataclass
class MrRead:
    """One MR of the set, from its stored baseline to the snapshot this run read."""

    ref: ResourceRef
    directory: Path
    last: dict[str, Any] | None = None
    default_branch: str | None = None
    snapshot: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    changes: list[Change] = field(default_factory=list)
    details: Details = field(default_factory=Details)

    @property
    def label(self) -> str:
        return f"{self.ref.host}/{self.ref.project}!{self.ref.id}"

    @property
    def url(self) -> str:
        return f"https://{self.ref.host}/{self.ref.project}/-/merge_requests/{self.ref.id}"

    def load(self, reset: bool) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        ensure_readme(self.directory, "mr")
        path = self.directory / BASELINE_FILE
        if reset:
            path.unlink(missing_ok=True)
        # The log exists from the first read on, so a caller that opens it never finds it missing.
        (self.directory / EVENTS_LOG).touch()
        try:
            saved = json.loads(path.read_text(encoding="utf-8"), strict=False)
        except (OSError, ValueError):
            return
        if isinstance(saved, dict) and isinstance(saved.get("snapshot"), dict):
            self.last = saved["snapshot"]
            self.default_branch = saved.get("default_branch")

    def commit(self) -> None:
        """Advance the baseline and record the read. Called only after the output has been printed and flushed."""
        assert self.snapshot is not None
        snapshot = settle(self.last, self.snapshot)
        now = utc_now_iso()
        atomic_write_json(
            self.directory / BASELINE_FILE,
            {"snapshot": snapshot, "default_branch": self.default_branch, "written_at": now},
        )
        with locked(self.directory):
            state = load_state(self.directory, self.ref.key)
            state["status_read"] = {"observed_at": now, "snapshot": snapshot}
            save_state(self.directory, state)
        self.last = snapshot

    def log(self, changes: list[Change]) -> None:
        with open(self.directory / EVENTS_LOG, "a", encoding="utf-8") as fh:
            for change in changes:
                fh.write(f"{utc_now_iso()}\t{self.label}\t{change.id}\t{change.detail}\n")


async def probe(read: MrRead, client: GitLabClient) -> None:
    """Read the MR object, the newest note and the default branch side by side, then the approvals if needed."""
    ref = read.ref
    enc = encode_project(ref.project)
    mr_path = f"projects/{enc}/merge_requests/{ref.id}"
    params = {"include_diverged_commits_count": "true", "include_rebase_in_progress": "true"}

    async def newest_note() -> tuple[dict[str, Any] | None, str]:
        # A host that refuses notes refuses them every time; the read carries on with the note count.
        try:
            notes = await client.get_json(
                f"{mr_path}/notes", {"per_page": 1, "order_by": "updated_at", "sort": "desc"}, kind="notes"
            )
        except ApiError:
            return None, "count-only"
        if not isinstance(notes, list) or not notes or not isinstance(notes[0], dict):
            return None, "exact"
        return note_from_api(notes[0]), "exact"

    async def default_branch() -> str | None:
        if read.default_branch is not None:
            return read.default_branch
        try:
            project = await client.get_json(f"projects/{enc}", kind="project")
        except ApiError:
            return None
        return project.get("default_branch") if isinstance(project, dict) else None

    mr, (note, mode), branch = await asyncio.gather(
        client.get_json(mr_path, params, kind="mr"), newest_note(), default_branch()
    )
    if not isinstance(mr, dict):
        raise ApiError(f"GitLab returned no merge request object for {mr_path}")
    read.raw = mr
    atomic_write_json(read.directory / MR_FILE, mr)
    snap = snapshot_from_mr(mr, ref.host, ref.project)
    snap["newest_note"], snap["notes_probe"] = note, mode
    last = read.last
    if last is None or last.get("updated_at") != snap.get("updated_at") or last.get("approvals") is None:
        # Nothing else in the MR object reflects an approval, so only a moved updated_at is worth a request.
        try:
            approvals = await client.get_json(f"{mr_path}/approvals", kind="approvals")
            snap["approvals"] = approvals_from_api(approvals) if isinstance(approvals, dict) else None
        except ApiError:
            snap["approvals"] = dict(APPROVALS_UNAVAILABLE)
    else:
        snap["approvals"] = last.get("approvals")
    read.default_branch = branch
    snap["default_branch"] = branch
    read.snapshot = snap


async def fetch_discussions(read: MrRead, client: GitLabClient, base: Path) -> None:
    ref = read.ref
    assert ref.id is not None
    ctx = MrContext(ref.host, encode_project(ref.project), ref.project, ref.id, read.url)
    try:
        store, changes, first_run = await discussion_read.sync(ctx, client, full=False, base=base)
    except READ_ERRORS as exc:
        read.details.errors.append(f"glab-discussion read failed: {exc}")
        return
    read.details.discussions_dir = str(store.directory)
    read.details.discussions_summary = discussion_read.dump_lines(store, changes, first_run)[1:]


async def _pipeline_project(read: MrRead, client: GitLabClient, project_id: Any) -> str:
    """The path of the head pipeline's project: the MR's own, or for a fork's pipeline, the fork's."""
    if project_id is None or project_id in (read.raw.get("project_id"), read.raw.get("target_project_id")):
        return read.ref.project
    project = await client.get_json(f"projects/{project_id}", kind="project")
    path = project.get("path_with_namespace") if isinstance(project, dict) else None
    return str(path) if path else read.ref.project


async def fetch_pipeline(read: MrRead, client: GitLabClient, base: Path) -> None:
    snap = read.snapshot or {}
    hp = snap.get("head_pipeline") or {}
    if not hp or not isinstance(hp.get("id"), int):
        read.details.notes.append("no pipeline has run for this head, so there is nothing to dump")
        return
    try:
        project = await _pipeline_project(read, client, hp.get("project_id"))
        ref = ResourceRef("pipeline", read.ref.host, project, int(hp["id"]))
        directory = pipeline_dir(base, ref.host, ref.project, int(hp["id"]))
        url = f"https://{ref.host}/{ref.project}/-/pipelines/{ref.id}"
        command = shlex.join(["glab-pipeline", "inspect", "--pipeline-url", url, "--logs=all"])
        read.details.pipeline_dir = str(directory)
        read.details.pipeline_summary_file = str(directory / "summary.txt")
        options = InspectOptions(ref=ref, directory=directory, command=command, branch=snap.get("source_branch"))
        summary = await run_inspect(options, client)
    except READ_ERRORS as exc:
        read.details.errors.append(f"glab-pipeline inspect failed: {exc}")
        return
    read.details.failed_jobs = failed_job_lines(summary)
    sha = hp.get("sha")
    if not sha:
        return
    # External commit statuses (a code-quality gate, say) live on the commit, not in the pipeline, so the pipeline
    # dump does not report them.
    try:
        statuses = await client.get_pages(
            f"projects/{encode_project(read.ref.project)}/repository/commits/{sha}/statuses", kind="statuses"
        )
    except ApiError as exc:
        read.details.errors.append(f"commit statuses unavailable: {exc}")
        statuses = []
    read.details.external_statuses = external_statuses(statuses, hp.get("id"), pipeline_job_names(directory))
    atomic_write_json(read.directory / EXTERNAL_STATUSES_FILE, read.details.external_statuses)


ClientFactory = Callable[[str], GitLabClient]


async def _read_all(reads: list[MrRead], options: ReadOptions, client_for: ClientFactory) -> None:
    clients: dict[str, GitLabClient] = {}

    async def one(read: MrRead) -> None:
        # One client per host, so the MRs of one host share its concurrency limit.
        if read.ref.host not in clients:
            clients[read.ref.host] = client_for(read.ref.host)
        client = clients[read.ref.host]
        try:
            await probe(read, client)
        except READ_ERRORS as exc:
            read.error = str(exc)
            return
        read.changes = diff_snapshots(read.last, read.snapshot or {})
        details = []
        if options.comments:
            details.append(fetch_discussions(read, client, options.base))
        if options.pipeline:
            details.append(fetch_pipeline(read, client, options.base))
        await asyncio.gather(*details)

    await asyncio.gather(*(one(read) for read in reads))


def run(refs: list[ResourceRef], options: ReadOptions, client_for: ClientFactory, out: TextIO = sys.stdout) -> int:
    reads = [MrRead(ref, mr_dir(options.base, ref.host, ref.project, ref.id or 0)) for ref in refs]
    for read in reads:
        read.load(options.reset)
    asyncio.run(_read_all(reads, options, client_for))

    def emit(lines: list[str]) -> None:
        out.write("".join(f"{line}\n" for line in lines))
        out.flush()

    read_any = False
    for read in reads:
        if read.error is not None or read.snapshot is None:
            failed = Change("READ_FAILED", f"probe failed: {read.error}")
            emit([f"== {read.label}", f"  {failed.line()}"])
            with contextlib.suppress(OSError):
                read.log([failed])
            continue
        read_any = True
        lines = [f"== {read.label}"]
        first = read.last is None
        if first:
            lines.append("  baseline established (no earlier state for this MR)")
            lines += snapshot_lines(read.snapshot)
        lines += [f"  {change.line()}" for change in read.changes]
        if not first and not read.changes:
            lines.append("  no change since the last run")
            lines += snapshot_lines(read.snapshot)
        lines += details_lines(read.details)
        lines.append(f"  scorecard: {scorecard(read.snapshot)}")
        emit(lines)
        if read.changes:
            read.log(read.changes)
        read.commit()

    if not read_any:
        emit(["result: nothing could be read; every MR failed (see the READ_FAILED lines above)"])
        return EXIT_READ_FAILED
    emit([f"state: {reads[0].directory if len(reads) == 1 else options.base}"])
    return EXIT_OK
