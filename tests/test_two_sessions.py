"""One MR and its pipeline watched from two Claude Code sessions at once, with real processes.

Each session has its own scratchpad, so each watcher writes its own base dir: its own `events.jsonl` with a gapless
seq, `baseline.json`, `threads/` and `job-logs/`, and its own stream. Both share one resource of one daemon, so
GitLab is polled once. The library's tests cover the same cases with a fake source; these cover glab's own files.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from cli_watch_kit.daemon.transport import request as daemon_request

from fakeglab import EE_PROBE_ANSWER, Route
from shapes import HOST, PROJECT, gql_job, gql_mr, gql_pipeline, gql_thread, rest_discussion
from test_daemon import DaemonEnv, _watch, daemon_env, wait_for

__all__ = ["daemon_env"]  # the fixture, imported for pytest

MR_URL = f"https://{HOST}/{PROJECT}/-/merge_requests/42"
PIPELINE_URL = f"https://{HOST}/{PROJECT}/-/pipelines/5001"
MR_KEY = f"mr {HOST}/{PROJECT}!42"
PIPELINE_KEY = f"pipeline {HOST}/{PROJECT}!5001"
A1 = gql_thread("aaa111", [(1, "2026-11-09T09:00:00Z", "reviewer-one")])
B1 = gql_thread("bbb222", [(7, "2026-11-09T10:00:00Z", "reviewer-two")])
B2 = gql_thread("bbb222", [(7, "2026-11-09T10:00:00Z", "reviewer-two"), (8, "2026-11-09T11:00:00Z", "author-one")])
JOBS = [
    {"id": 1, "name": "build", "stage": "build", "status": "success", "web_url": "u", "started_at": "x"},
    {"id": 2, "name": "unit", "stage": "test", "status": "failed", "web_url": "u", "started_at": "x"},
]


@pytest.fixture(autouse=True)
def _one_transport(daemon_env: DaemonEnv) -> None:
    if os.environ.get("GLAB_CLI_EXTRAS_TRANSPORT") == "tcp":
        pytest.skip("the library runs its two-session tests over both transports; glab's files need only one")


def routes(env: DaemonEnv, threads: list[dict[str, Any]], pipeline: str, unit: str) -> None:
    env.routes(
        Route("GitLabCapabilities", EE_PROBE_ANSWER),
        Route("MergeRequestSnapshot", {"data": gql_mr(threads=threads, pipeline_id=5001)}),
        Route(
            "PipelineTree",
            {
                "data": gql_pipeline(
                    5001, pipeline, [gql_job(1, "build", "success", stage="build"), gql_job(2, "unit", unit)]
                )
            },
            etag=f'W/"{pipeline}-{unit}"',
        ),
        Route("discussions/aaa111", rest_discussion("aaa111", [(1, "reviewer-one", "Looks fine.")])),
        Route(
            "discussions/bbb222",
            rest_discussion(
                "bbb222",
                [(7, "reviewer-two", "Please rename this.")]
                + ([(8, "author-one", "Renamed.")] if threads == [A1, B2] else []),
            ),
        ),
        Route("pipelines/5001/jobs", JOBS),
        Route("pipelines/5001/bridges", []),
        Route("pipelines/5001/test_report_summary", {"total": {"failed": 1, "count": 3}}),
        Route(
            "pipelines/5001", {"id": 5001, "status": pipeline, "ref": "main", "sha": "a" * 40, "web_url": PIPELINE_URL}
        ),
        Route("/statuses", []),
        Route("version", {"version": "18.11.0"}),
        Route("jobs/2/trace", "FAILED: unit\n"),
    )


def mr_dir(base: Path) -> Path:
    return base / HOST / "group" / "project" / "-" / "merge_requests" / "42"


def pipeline_dir(base: Path) -> Path:
    return base / HOST / "group" / "project" / "-" / "pipelines" / "5001"


def events(directory: Path) -> list[dict[str, Any]]:
    path = directory / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def kinds(directory: Path) -> list[str]:
    return [e["kind"] for e in events(directory)]


def has(directory: Path, kind: str) -> bool:
    return kind in kinds(directory)


def finish(watch: subprocess.Popen[str], directory: Path) -> list[dict[str, Any]]:
    """Wait for the end; the stream and the changelog agree, and the seq has no gap."""
    out, err = watch.communicate(timeout=60)
    assert watch.returncode == 0, err
    found = events(directory)
    assert [json.loads(line) for line in out.splitlines()] == found, err
    assert [e["seq"] for e in found] == list(range(1, len(found) + 1)), found
    return found


def same_except_paths(a: list[dict[str, Any]], a_base: Path, b: list[dict[str, Any]], b_base: Path) -> None:
    def normalized(found: list[dict[str, Any]], base: Path) -> Any:
        text = json.dumps([{k: e.get(k) for k in ("kind", "data", "artifacts", "seq")} for e in found])
        return json.loads(text.replace(str(base), "<BASE>"))

    assert normalized(a, a_base) == normalized(b, b_base)


def subscriptions(env: DaemonEnv, key: str) -> int:
    resources = [r for r in daemon_request(env.files, {"op": "status"})["resources"] if r["resource"] == key]
    assert len(resources) <= 1
    return resources[0]["subscriptions"] if resources else 0


def thread_count_is(base: Path, count: int) -> bool:
    return len(thread_files(base)) == count


def thread_files(base: Path) -> list[str]:
    return sorted(p.read_text().split("\n", 1)[0] for p in (mr_dir(base) / "threads").glob("*.txt"))


def test_two_sessions_watch_one_mr_and_its_pipeline_through_one_resource_each(daemon_env: DaemonEnv) -> None:
    routes(daemon_env, [A1], "running", "running")
    one, two = daemon_env.root / "session-one", daemon_env.root / "session-two"
    mr_args = ["mr", MR_URL, "--events-stream=stdout", "--timeout=90s"]
    pipeline_args = ["pipeline", PIPELINE_URL, "--events-stream=stdout", "--timeout=90s"]
    mr_one = _watch([*mr_args, "--base-dir", str(one)], daemon_env.env())
    mr_two = _watch([*mr_args, "--base-dir", str(two)], daemon_env.env())
    pipeline_one = _watch([*pipeline_args, "--base-dir", str(one)], daemon_env.env())
    pipeline_two = _watch([*pipeline_args, "--base-dir", str(two)], daemon_env.env())
    for base in (one, two):
        wait_for(partial(has, mr_dir(base), "watch_started"), 30, f"the MR watch into {base}")
        wait_for(partial(has, pipeline_dir(base), "watch_started"), 30, f"the pipeline watch into {base}")
        wait_for(partial(thread_count_is, base, 1), 30, f"the thread file in {base}")
    wait_for(lambda: subscriptions(daemon_env, MR_KEY) == 2, 10, "one MR resource with two subscriptions")
    wait_for(lambda: subscriptions(daemon_env, PIPELINE_KEY) == 2, 10, "one pipeline resource with two subscriptions")

    # (a) A new thread and a failed job: each session gets both, with the files in its own dir.
    routes(daemon_env, [A1, B1], "failed", "failed")
    for base in (one, two):
        wait_for(partial(has, mr_dir(base), "thread_created"), 40, f"thread_created in {base}")
        wait_for(partial(has, pipeline_dir(base), "watch_ended"), 40, f"the pipeline end in {base}")
        assert len(thread_files(base)) == 2
        log = pipeline_dir(base) / "job-logs" / "test-unit-2.log"
        assert log.read_text() == "FAILED: unit\n"
        [finished] = [e for e in events(pipeline_dir(base)) if e["kind"] == "job_finished" and e["data"]["job_id"] == 2]
        assert finished["artifacts"] == [str(log)]
        [created] = [e for e in events(mr_dir(base)) if e["kind"] == "thread_created"]
        assert Path(created["artifacts"][0]).parent == mr_dir(base) / "threads"
    same_except_paths(finish(pipeline_one, pipeline_dir(one)), one, finish(pipeline_two, pipeline_dir(two)), two)
    # The poller fetched the failed job's log once, and both watchers got it from the daemon's cache.
    assert daemon_env.calls.read_text().count("jobs/2/trace") == 1

    # (c) Session one stops its MR watch. Session two keeps the resource and gets the next change.
    mr_one.send_signal(signal.SIGTERM)
    ended_one = finish(mr_one, mr_dir(one))
    assert ended_one[-1]["data"]["reason"] == "signal SIGTERM"
    wait_for(lambda: subscriptions(daemon_env, MR_KEY) == 1, 10, "the MR resource to keep session two's subscription")
    routes(daemon_env, [A1, B2], "failed", "failed")
    wait_for(lambda: has(mr_dir(two), "thread_updated"), 40, "thread_updated in session two")
    mr_two.send_signal(signal.SIGTERM)
    ended_two = finish(mr_two, mr_dir(two))
    assert [e["kind"] for e in ended_two] == ["watch_started", "thread_created", "thread_updated", "watch_ended"]
    same_except_paths(ended_one[:-1], one, ended_two[: len(ended_one) - 1], two)
    assert not has(mr_dir(one), "thread_updated")


@pytest.mark.skipif(os.name == "nt", reason="uses SIGSTOP")
def test_two_sessions_each_get_the_gap_once_after_a_restart_and_a_late_one_gets_no_backlog(
    daemon_env: DaemonEnv,
) -> None:
    routes(daemon_env, [A1], "running", "running")
    one, two, late = (daemon_env.root / name for name in ("session-one", "session-two", "session-late"))
    args = ["mr", MR_URL, "--events-stream=stdout", "--timeout=60s", "--events=comments"]
    first = _watch([*args, "--base-dir", str(one)], daemon_env.env())
    second = _watch([*args, "--base-dir", str(two)], daemon_env.env())
    for base in (one, two):
        wait_for(partial(has, mr_dir(base), "watch_started"), 30, f"the MR watch into {base}")
        wait_for(partial(thread_count_is, base, 1), 30, f"the thread file in {base}")

    # (b) The daemon dies while both sessions' watchers are frozen, and a thread is added meanwhile.
    for watch in (first, second):
        os.kill(watch.pid, signal.SIGSTOP)
    os.kill(daemon_request(daemon_env.files, {"op": "hello"})["pid"], signal.SIGKILL)
    wait_for(lambda: not daemon_env.files.socket.exists() or not _answers(daemon_env), 10, "the daemon to die")
    routes(daemon_env, [A1, B1], "running", "running")
    os.kill(first.pid, signal.SIGCONT)
    wait_for(lambda: has(mr_dir(one), "thread_created"), 40, "the gap in session one")
    # Session two comes back to a resource the new daemon already polls, and still gets its own gap.
    os.kill(second.pid, signal.SIGCONT)
    wait_for(lambda: has(mr_dir(two), "thread_created"), 40, "the gap in session two")
    for base in (one, two):
        assert kinds(mr_dir(base)) == ["watch_started", "watch_started", "thread_created"]
        assert len(thread_files(base)) == 2

    # (d) A third session starts late: its own watch_started and thread files, and none of the events before it.
    third = _watch([*args, "--base-dir", str(late), "--timeout=5s"], daemon_env.env())
    late_events = finish(third, mr_dir(late))
    assert [e["kind"] for e in late_events] == ["watch_started", "watch_ended"]
    assert late_events[0]["data"]["summary"]["threads"]["total"] == 2
    assert len(thread_files(late)) == 2
    assert json.loads((mr_dir(late) / "baseline.json").read_text())["resource"] == MR_KEY

    for watch, base in ((first, one), (second, two)):
        watch.send_signal(signal.SIGTERM)
        finish(watch, mr_dir(base))
    same_except_paths(events(mr_dir(one)), one, events(mr_dir(two)), two)


def _answers(env: DaemonEnv) -> bool:
    try:
        daemon_request(env.files, {"op": "hello"}, timeout=1)
    except OSError:
        return False
    return True
