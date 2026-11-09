"""The watch client writes every file; the daemon only hands it the content."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from glab_cli_extras.events import Event
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.serde import to_jsonable
from glab_cli_extras.sync.job_logs import LogContent
from glab_cli_extras.sync.mr import parse_mr, parse_threads
from glab_cli_extras.watch.sinks import StatusOutput
from glab_cli_extras.watch.writer import ResourceWriter
from shapes import HOST, PROJECT, gql_mr, gql_thread, rest_discussion

MR = ResourceRef("mr", HOST, PROJECT, 42)
PIPELINE = ResourceRef("pipeline", HOST, PROJECT, 5001)


class FakeBackend:
    def __init__(self) -> None:
        self.discussions: dict[str, dict[str, Any]] = {}
        self.thread_requests: list[set[str]] = []
        self.log_requests: list[int] = []

    def fetch_threads(self, ref: ResourceRef, ids: set[str]) -> dict[str, dict[str, Any]]:
        self.thread_requests.append(set(ids))
        return {i: self.discussions[i] for i in ids if i in self.discussions}

    def fetch_job_log(self, host: str, project: str, job_id: int) -> LogContent:
        self.log_requests.append(job_id)
        return LogContent(b"line 1\nERROR: tests failed\n", False, 26)


def mr_snapshot(*threads: dict[str, Any]) -> Any:
    node = gql_mr(threads=list(threads))["project"]["mergeRequest"]
    return to_jsonable(parse_mr(node, PROJECT, parse_threads(node["discussions"]["nodes"])))


def event(kind: str, **data: Any) -> Event:
    return Event(kind, MR.key, 1, "2026-11-09T10:00:00.000Z", data)


def writer(tmp_path: Path, backend: FakeBackend, ref: ResourceRef = MR) -> ResourceWriter:
    return ResourceWriter(tmp_path, ref, backend, "failed", StatusOutput(io.StringIO()))  # type: ignore[arg-type]


def test_mr_snapshot_writes_state_and_only_the_threads_whose_notes_changed(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.discussions = {
        "aaa111": rest_discussion("aaa111", [(1, "reviewer-one", "Looks fine.")]),
        "bbb222": rest_discussion("bbb222", [(7, "reviewer-two", "Please rename this.")], diff=True),
    }
    a1 = gql_thread("aaa111", [(1, "2026-11-09T09:00:00Z", "reviewer-one")])
    b1 = gql_thread("bbb222", [(7, "2026-11-09T10:00:00Z", "reviewer-two")])
    w = writer(tmp_path, backend)
    w.snapshot(mr_snapshot(a1, b1))
    mr_dir = tmp_path / HOST / "group" / "project" / "-" / "merge_requests" / "42"
    state = json.loads((mr_dir / "state.json").read_text())
    assert state["resource"] == MR.key and state["snapshot"]["iid"] == 42
    assert (mr_dir / "README.txt").is_file()
    assert backend.thread_requests == [{"aaa111", "bbb222"}]
    files = sorted(p.name for p in (mr_dir / "threads").glob("*.txt"))
    assert len(files) == 2

    # A second writer, as in a new watch of the same session, fetches nothing that is already on disk.
    again = writer(tmp_path, backend)
    again.snapshot(mr_snapshot(a1, b1))
    assert backend.thread_requests == [{"aaa111", "bbb222"}]

    # One thread gets a reply, the other is deleted: one fetch, one file removed, and the event points at the file.
    a2 = gql_thread("aaa111", [(1, "2026-11-09T09:00:00Z", "reviewer-one"), (2, "2026-11-09T11:00:00Z", "author-one")])
    again.snapshot(mr_snapshot(a2))
    assert backend.thread_requests[-1] == {"aaa111"}
    assert len(list((mr_dir / "threads").glob("*.txt"))) == 1
    [updated] = again.attach([event("thread_updated", discussion_id="aaa111")])
    assert Path(updated.artifacts[0]).parent == mr_dir / "threads"
    [started] = again.attach([event("watch_started", summary={"threads": {"total": 1}})])
    assert started.data["summary"]["dir"] == str(mr_dir)
    assert started.data["summary"]["threads"]["dir"] == str(mr_dir / "threads")


def test_a_thread_gitlab_did_not_return_keeps_its_file_and_is_fetched_again(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.discussions = {"aaa111": rest_discussion("aaa111", [(1, "reviewer-one", "Looks fine.")])}
    a1 = gql_thread("aaa111", [(1, "2026-11-09T09:00:00Z", "reviewer-one")])
    w = writer(tmp_path, backend)
    w.snapshot(mr_snapshot(a1))
    del backend.discussions["aaa111"]
    a2 = gql_thread("aaa111", [(1, "2026-11-09T09:05:00Z", "reviewer-one")])
    w.snapshot(mr_snapshot(a2))
    threads = tmp_path / HOST / "group" / "project" / "-" / "merge_requests" / "42" / "threads"
    assert len(list(threads.glob("*.txt"))) == 1
    backend.discussions["aaa111"] = rest_discussion("aaa111", [(1, "reviewer-one", "Edited.")])
    w.attach([])
    assert backend.thread_requests[-1] == {"aaa111"}
    assert "Edited." in next(threads.glob("*.txt")).read_text()


def test_job_finished_writes_the_log_into_the_jobs_pipeline_dir_once(tmp_path: Path) -> None:
    backend = FakeBackend()
    w = writer(tmp_path, backend, PIPELINE)
    finished = Event(
        "job_finished",
        PIPELINE.key,
        3,
        "2026-11-09T10:00:00.000Z",
        {
            "pipeline_id": 6001,
            "project": "group/deploy-tools",
            "job_id": 9,
            "name": "deploy:prod",
            "stage": "deploy",
            "status": "failed",
        },
    )
    [out] = w.attach([finished])
    path = Path(out.artifacts[0])
    assert path == tmp_path / HOST / "group" / "deploy-tools" / "-" / "pipelines" / "6001" / "job-logs" / (
        "deploy-deploy-prod-9.log"
    )
    assert path.read_bytes().endswith(b"ERROR: tests failed\n")
    state = json.loads((path.parent.parent / "state.json").read_text())
    assert state["resource"] == "pipeline gitlab.example.com/group/deploy-tools!6001"
    assert state["logs"]["9"]["log_state"] == "downloaded" and state["logs"]["9"]["complete"] is True
    writer(tmp_path, backend, PIPELINE).attach([finished])
    assert backend.log_requests == [9]
    passed = Event("job_finished", PIPELINE.key, 4, "x", {**finished.data, "job_id": 10, "status": "success"})
    assert w.attach([passed])[0].artifacts == ()
    assert backend.log_requests == [9]
