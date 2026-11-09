"""The watch client writes every file; the daemon only hands it the content."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from glab_cli_extras.daemon.transport import DaemonFiles, DaemonMismatch
from glab_cli_extras.events import Event
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.serde import to_jsonable
from glab_cli_extras.store.job_logs import PLACEHOLDER_MARKER
from glab_cli_extras.sync.job_logs import LogContent
from glab_cli_extras.sync.mr import parse_mr, parse_threads
from glab_cli_extras.sync.pipeline import parse_pipeline
from glab_cli_extras.watch import client
from glab_cli_extras.watch.client import BackendError, DaemonBackend
from glab_cli_extras.watch.sinks import StatusOutput
from glab_cli_extras.watch.writer import ResourceWriter
from shapes import HOST, PROJECT, gql_job, gql_mr, gql_pipeline, gql_thread, rest_discussion

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


def pipeline_snapshot(*jobs: dict[str, Any], status: str = "running") -> Any:
    return to_jsonable(parse_pipeline(gql_pipeline(5001, status, list(jobs)), HOST, PROJECT))


def test_every_job_gets_a_placeholder_until_its_log_arrives(tmp_path: Path) -> None:
    backend = FakeBackend()
    w = writer(tmp_path, backend, PIPELINE)
    w.snapshot(
        pipeline_snapshot(gql_job(1, "build", "success"), gql_job(2, "unit", "running"), gql_job(3, "e2e", "skipped"))
    )
    logs = tmp_path / HOST / "group" / "project" / "-" / "pipelines" / "5001" / "job-logs"
    texts = {p.name: p.read_text() for p in logs.iterdir()}
    assert set(texts) == {"test-build-1.log", "test-unit-2.log", "test-e2e-3.log"}
    assert all(t.startswith(PLACEHOLDER_MARKER) for t in texts.values())
    # The writer's base is not the default one, so the command names the dir too.
    url = f"https://{HOST}/{PROJECT}/-/pipelines/5001"
    command = f"glab-pipeline inspect --pipeline-url {url} --output-dir {logs.parent} --logs=all"
    assert command in texts["test-build-1.log"] and "the job passed" in texts["test-build-1.log"]
    assert "still running" in texts["test-unit-2.log"] and "skipped" in texts["test-e2e-3.log"]

    # The running job fails: its placeholder says the log is coming, then job_finished replaces it with the log.
    w.snapshot(
        pipeline_snapshot(gql_job(1, "build", "success"), gql_job(2, "unit", "failed"), gql_job(3, "e2e", "skipped"))
    )
    assert "being downloaded" in (logs / "test-unit-2.log").read_text()
    finished = Event(
        "job_finished",
        PIPELINE.key,
        5,
        "2026-11-09T10:00:00.000Z",
        {"pipeline_id": 5001, "project": PROJECT, "job_id": 2, "name": "unit", "stage": "test", "status": "failed"},
    )
    [out] = w.attach([finished])
    assert Path(out.artifacts[0]) == logs / "test-unit-2.log"
    assert (logs / "test-unit-2.log").read_bytes().endswith(b"ERROR: tests failed\n")
    state = json.loads((logs.parent / "state.json").read_text())
    assert {k: v["log_state"] for k, v in state["logs"].items()} == {
        "1": "placeholder",
        "2": "downloaded",
        "3": "placeholder",
    }
    # Later snapshots never put a placeholder over a real log.
    w.snapshot(pipeline_snapshot(gql_job(1, "build", "success"), gql_job(2, "unit", "failed"), status="failed"))
    assert not (logs / "test-unit-2.log").read_text().startswith(PLACEHOLDER_MARKER)


class OlderDaemon(FakeBackend):
    """A daemon started from older code under the same protocol version: it does not know the newer requests."""

    def fetch_job_log(self, host: str, project: str, job_id: int) -> LogContent:
        raise DaemonMismatch("the daemon answered unknown op 'fetch_job_log'")

    def fetch_dump(self, ref: ResourceRef) -> dict[str, Any]:
        raise DaemonMismatch("the daemon answered unknown op 'fetch_dump'")


def test_a_daemon_that_does_not_know_a_request_is_reported_once_as_watch_degraded(tmp_path: Path) -> None:
    w = writer(tmp_path, OlderDaemon(), PIPELINE)
    failed = event("job_finished", job_id=901, pipeline_id=5001, stage="test", name="unit", status="failed")
    finished = event("finished", pipeline_id=5001, status="failed")
    out = w.attach([failed, finished])
    assert [e.kind for e in out] == ["job_finished", "finished", "watch_degraded"]
    assert out[2].data["reason"] == "daemon_mismatch" and "fetch_job_log" in out[2].data["message"]
    assert "log_error" not in out[0].data and "dump_error" not in out[1].data
    # Reported once: the next events that need the daemon fail the same way, silently.
    assert [e.kind for e in w.attach([failed])] == ["job_finished"]


def test_the_daemon_backend_turns_an_unknown_op_into_a_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = DaemonBackend.__new__(DaemonBackend)
    backend.files, backend.hello, backend._shutting_down = (
        DaemonFiles(Path("unused")),
        {"pid": 7, "version": "0.1.0"},
        False,
    )
    monkeypatch.setattr(
        client, "request", lambda files, payload, timeout: {"ok": False, "error": "unknown op 'fetch_dump'"}
    )
    with pytest.raises(DaemonMismatch, match=r"pid 7, glab-cli-extras 0\.1\.0.*glab-watch daemon stop"):
        backend.fetch_dump(PIPELINE)
    monkeypatch.setattr(client, "request", lambda files, payload, timeout: {"ok": False, "error": "lease expired"})
    with pytest.raises(BackendError, match="lease expired"):
        backend.fetch_dump(PIPELINE)
