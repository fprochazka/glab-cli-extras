"""Daemon lifecycle with real processes: spawn, the single-instance lock, leases, idle shutdown, restart.

Each test gets a short private state dir (Unix socket paths are limited to about 104 bytes) and a fake `glab` first
on PATH, and stops every daemon it started.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fakeglab import Route, install_fake_glab, write_spec
from glab_cli_extras.daemon.lifecycle import daemon_running, ensure_daemon, spawn_daemon, stop_daemon
from glab_cli_extras.daemon.transport import DaemonFiles
from glab_cli_extras.daemon.transport import request as daemon_request
from shapes import HOST, PROJECT, gql_job, gql_pipeline

SRC = str(Path(__file__).resolve().parents[1] / "src")
PIPELINE_URL = f"https://{HOST}/{PROJECT}/-/pipelines/5001"


@dataclass
class DaemonEnv:
    files: DaemonFiles
    spec: Path
    calls: Path
    root: Path

    def routes(self, *routes: Route) -> None:
        write_spec(self.spec, list(routes))

    def env(self, **extra: str) -> dict[str, str]:
        return {**os.environ, **extra}


def wait_for(condition: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(params=["unix", "tcp"] if os.name != "nt" else ["tcp"])
def daemon_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[DaemonEnv]:
    root = Path(tempfile.mkdtemp(prefix="gce-"))
    files = DaemonFiles(root / "s")
    install_fake_glab(root / "bin")
    spec, calls = root / "spec.json", root / "calls.jsonl"
    write_spec(spec, [])
    monkeypatch.setenv("PATH", str(root / "bin") + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("GLAB_CLI_EXTRAS_STATE_DIR", str(files.state_dir))
    monkeypatch.setenv("GLAB_CLI_EXTRAS_CACHE_DIR", str(root / "cache"))
    monkeypatch.setenv("GLAB_CLI_EXTRAS_IDLE_TIMEOUT", "2")
    monkeypatch.setenv("GLAB_CLI_EXTRAS_LEASE_TIMEOUT", "2")
    monkeypatch.setenv("GLAB_CLI_EXTRAS_CLIENT_POLL_MS", "200")
    monkeypatch.setenv("FAKE_GLAB_SPEC", str(spec))
    monkeypatch.setenv("FAKE_GLAB_LOG", str(calls))
    monkeypatch.setenv("PYTHONPATH", SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    if request.param == "tcp":
        monkeypatch.setenv("GLAB_CLI_EXTRAS_TRANSPORT", "tcp")
    else:
        monkeypatch.delenv("GLAB_CLI_EXTRAS_TRANSPORT", raising=False)
    env = DaemonEnv(files, spec, calls, root)
    try:
        yield env
    finally:
        stop_daemon(files)
        assert not daemon_running(files), "a daemon survived the test"
        shutil.rmtree(root, ignore_errors=True)


CLIENT = (
    "from glab_cli_extras.daemon.lifecycle import ensure_daemon; from glab_cli_extras.daemon.transport import "
    "DaemonFiles; print(ensure_daemon(DaemonFiles.default())['pid'])"
)


def test_concurrent_clients_start_exactly_one_daemon(daemon_env: DaemonEnv) -> None:
    clients = [
        subprocess.Popen([sys.executable, "-c", CLIENT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    pids = set()
    for client in clients:
        out, err = client.communicate(timeout=30)
        assert client.returncode == 0, err
        pids.add(int(out.strip()))
    assert len(pids) == 1
    assert daemon_running(daemon_env.files)
    log = daemon_env.files.log.read_text()
    assert log.count("daemon 0.1.0 started") == 1


def test_a_second_daemon_exits_at_once(daemon_env: DaemonEnv) -> None:
    first = ensure_daemon(daemon_env.files)
    second = spawn_daemon(daemon_env.files)
    assert second.wait(timeout=15) == 0
    assert ensure_daemon(daemon_env.files)["pid"] == first["pid"]
    assert "another daemon holds" in daemon_env.files.log.read_text()


def _subscribe(files: DaemonFiles, client_id: str) -> dict[str, Any]:
    return daemon_request(
        files,
        {
            "op": "subscribe",
            "client_id": client_id,
            "resource": {"kind": "pipeline", "host": HOST, "project": PROJECT, "id": 5001},
            "options": {"events": ["status_changed", "job_finished", "finished"], "logs": "none"},
        },
    )


def test_leases_expire_and_the_daemon_shuts_down_when_idle(daemon_env: DaemonEnv) -> None:
    daemon_env.routes(
        Route("PipelineTree", {"data": gql_pipeline(5001, "running", [gql_job(1, "t", "running")])}, etag='W/"1"')
    )
    ensure_daemon(daemon_env.files)
    first, second = _subscribe(daemon_env.files, "client-a"), _subscribe(daemon_env.files, "client-b")
    assert first["ok"] and second["ok"]

    def started() -> bool:
        response = daemon_request(
            daemon_env.files, {"op": "poll", "client_id": "client-a", "cursors": {first["subscription_id"]: 0}}
        )
        return bool(response["events"])

    wait_for(started, 15, "watch_started")
    status = daemon_request(daemon_env.files, {"op": "status"})
    assert len(status["subscriptions"]) == 2 and len(status["resources"]) == 1

    # client-a keeps polling; client-b went silent, so its lease runs out.
    def only_a_left() -> bool:
        daemon_request(
            daemon_env.files, {"op": "poll", "client_id": "client-a", "cursors": {first["subscription_id"]: 0}}
        )
        return len(daemon_request(daemon_env.files, {"op": "status"})["subscriptions"]) == 1

    wait_for(only_a_left, 15, "client-b's lease to expire")
    # Now client-a goes silent too: the lease expires, then the idle timeout stops the daemon.
    wait_for(lambda: not daemon_running(daemon_env.files), 20, "idle shutdown")
    assert not daemon_env.files.socket.exists() and not daemon_env.files.endpoint.exists()
    assert "no subscriptions for 2 s, exiting" in daemon_env.files.log.read_text()


def _watch(args: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", "from glab_cli_extras.cli.watch import main; main()", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_watch_command_streams_jsonl_and_header_goes_to_stderr(daemon_env: DaemonEnv) -> None:
    daemon_env.routes(
        Route("PipelineTree", {"data": gql_pipeline(5001, "success", [gql_job(1, "t", "success")])}, etag='W/"1"')
    )
    changelog = daemon_env.root / "changelog"
    watch = _watch(
        ["pipeline", PIPELINE_URL, "--events-stream=stdout", "--changelog-dir", str(changelog)], daemon_env.env()
    )
    out, err = watch.communicate(timeout=30)
    assert watch.returncode == 0, err
    lines = out.splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["watch_started", "watch_ended"]
    assert json.loads(lines[1])["data"] == {"reason": "finished", "status": "success"}
    header = [line for line in err.splitlines() if line.startswith("glab-watch: ")]
    assert header[0] == f"glab-watch: resource: pipeline {HOST}/{PROJECT}!5001"
    assert any(line.startswith("glab-watch: ignored: (none)") for line in header)
    assert any(line.startswith("glab-watch: restart with: glab-watch pipeline ") for line in header)
    events_file = changelog / HOST / "group" / "project" / "pipeline-5001" / "events.jsonl"
    assert events_file.read_text().splitlines() == lines
    state = json.loads((events_file.parent / "state.json").read_text())
    assert state["resource"] == f"pipeline {HOST}/{PROJECT}!5001"


def test_under_claude_code_the_header_still_goes_to_stderr(daemon_env: DaemonEnv) -> None:
    daemon_env.routes(Route("PipelineTree", {"data": gql_pipeline(5001, "success", [])}, etag='W/"1"'))
    watch = _watch(
        [
            "pipeline",
            PIPELINE_URL,
            "--events-stream=stdout",
            "--exclude-events=finished",
            "--changelog-dir",
            str(daemon_env.root / "c"),
        ],
        daemon_env.env(CLAUDECODE="1"),
    )
    out, err = watch.communicate(timeout=30)
    assert watch.returncode == 0, err
    assert "glab-watch: ignored: finished" in err.splitlines()
    assert all(line.startswith("{") for line in out.splitlines()), "stdout must carry only events"
    assert [json.loads(line)["kind"] for line in out.splitlines()] == [
        "watch_started",
        "watch_ended",
    ]


def test_watcher_survives_a_daemon_restart_without_a_duplicate_start(daemon_env: DaemonEnv) -> None:
    daemon_env.routes(
        Route("PipelineTree", {"data": gql_pipeline(5001, "running", [gql_job(1, "t", "running")])}, etag='W/"1"')
    )
    watch = _watch(
        [
            "pipeline",
            PIPELINE_URL,
            "--events-stream=stdout",
            "--timeout=6s",
            "--logs=none",
            "--changelog-dir",
            str(daemon_env.root / "c"),
        ],
        daemon_env.env(),
    )
    assert watch.stdout is not None
    first_line = watch.stdout.readline()
    assert json.loads(first_line)["kind"] == "watch_started"
    old_pid = daemon_request(daemon_env.files, {"op": "hello"})["pid"]
    stop_daemon(daemon_env.files)
    rest, err = watch.communicate(timeout=30)
    assert watch.returncode == 0, err
    kinds = [json.loads(line)["kind"] for line in rest.splitlines()]
    assert kinds == ["watch_ended"], (kinds, err)
    assert json.loads(rest.splitlines()[-1])["data"]["reason"] == "timeout"
    assert "daemon restarted, subscribing again" in err
    assert ensure_daemon(daemon_env.files)["pid"] != old_pid


def test_daemon_cli_status_and_stop(daemon_env: DaemonEnv) -> None:
    ensure_daemon(daemon_env.files)
    status = subprocess.run(
        [sys.executable, "-c", "from glab_cli_extras.cli.watch import main; main()", "daemon", "status", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["protocol"] == 1
    stop = subprocess.run(
        [sys.executable, "-c", "from glab_cli_extras.cli.watch import main; main()", "daemon", "stop"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert stop.stdout.strip() == "daemon: stopped"
    assert not daemon_running(daemon_env.files)


def _descendants(pid: int) -> list[int]:
    rows = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=True).stdout.splitlines()
    pairs = [(int(a), int(b)) for a, b in (row.split() for row in rows if row.strip())]
    found, todo = [], [pid]
    while todo:
        parent = todo.pop()
        children = [child for child, ppid in pairs if ppid == parent]
        found += children
        todo += children
    return found


def _running_pipeline(daemon_env: DaemonEnv) -> None:
    daemon_env.routes(
        Route("PipelineTree", {"data": gql_pipeline(5001, "running", [gql_job(1, "t", "running")])}, etag='W/"1"')
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process trees; Windows uses job objects")
def test_killing_a_watchers_whole_process_tree_leaves_the_shared_daemon_running(daemon_env: DaemonEnv) -> None:
    _running_pipeline(daemon_env)
    common = ["pipeline", PIPELINE_URL, "--events-stream=stdout", "--logs=none"]
    first = _watch([*common, "--changelog-dir", str(daemon_env.root / "c1")], daemon_env.env())
    assert first.stdout is not None and json.loads(first.stdout.readline())["kind"] == "watch_started"
    daemon_pid = daemon_request(daemon_env.files, {"op": "hello"})["pid"]
    assert daemon_pid not in _descendants(first.pid)

    second = _watch([*common, "--timeout=4s", "--changelog-dir", str(daemon_env.root / "c2")], daemon_env.env())
    assert second.stdout is not None and json.loads(second.stdout.readline())["kind"] == "watch_started"

    # What a tree kill does: SIGKILL the process and everything below it.
    for pid in [first.pid, *_descendants(first.pid)]:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    first.wait(timeout=10)

    rest, err = second.communicate(timeout=30)
    assert second.returncode == 0, err
    assert [json.loads(line)["data"]["reason"] for line in rest.splitlines()] == ["timeout"]
    assert "daemon restarted" not in err
    assert daemon_request(daemon_env.files, {"op": "hello"})["pid"] == daemon_pid
    assert daemon_env.files.log.read_text().count("daemon 0.1.0 started") == 1
