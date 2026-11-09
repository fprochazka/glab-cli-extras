"""glab-pipeline inspect against a fake glab: what it writes, what it reuses, and what a rerun costs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.cli.pipeline import main as pipeline_main
from glab_cli_extras.gitlab import sync_api
from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.pipeline.inspect import InspectOptions, run_inspect
from glab_cli_extras.store.job_logs import PLACEHOLDER_MARKER
from shapes import HOST, PROJECT, gql_job, gql_pipeline

REF = ResourceRef("pipeline", HOST, PROJECT, 5001)
COMMAND = f"glab-pipeline inspect --pipeline-url https://{HOST}/{PROJECT}/-/pipelines/5001 --logs=all"


def rest_job(job_id: int, name: str, status: str, stage: str = "test") -> dict[str, Any]:
    return {
        "id": job_id,
        "name": name,
        "stage": stage,
        "status": status,
        "failure_reason": "script_failure" if status == "failed" else None,
        "allow_failure": False,
        "web_url": f"https://{HOST}/{PROJECT}/-/jobs/{job_id}",
        "duration": 12.5,
        "started_at": None if status in ("skipped", "manual") else "2026-11-09T10:00:00Z",
        "finished_at": "2026-11-09T10:05:00Z",
        "created_at": "2026-11-09T09:59:00Z",
        "artifacts": [],
        "artifacts_expire_at": None,
    }


REST_PIPELINE = {
    "id": 5001,
    "iid": 77,
    "status": "failed",
    "source": "push",
    "ref": "feature/login-form",
    "sha": "a" * 40,
    "web_url": f"https://{HOST}/{PROJECT}/-/pipelines/5001",
    "yaml_errors": None,
    "created_at": "2026-11-09T09:59:00Z",
    "duration": 300,
}
JOBS = [rest_job(1, "build", "success", "build"), rest_job(2, "unit tests", "failed"), rest_job(3, "deploy", "skipped")]
TREE_JOBS = [
    gql_job(1, "build", "success", stage="build"),
    gql_job(2, "unit tests", "failed"),
    gql_job(3, "deploy", "skipped"),
]
TREE = gql_pipeline(5001, "failed", TREE_JOBS)


def routes() -> list[Route]:
    return [
        Route("PipelineTree", {"data": TREE}, etag='W/"tree-1"'),
        Route("pipelines/5001/jobs", JOBS),
        Route("pipelines/5001/bridges", []),
        Route("pipelines/5001/test_report_summary", {"total": {"failed": 1, "count": 9}}),
        Route("pipelines/5001", REST_PIPELINE),
        Route("jobs/2/trace", "...\nAssertionError: expected 3\n"),
        Route("jobs/1/trace", "build ok\n"),
    ]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGlab:
    fake = FakeGlab(routes())
    monkeypatch.setattr(sync_api, "RUNNER", fake.sync)
    return fake


def inspect(fake: FakeGlab, clock: Clock, directory: Path, **options: Any) -> dict[str, Any]:
    opts = InspectOptions(ref=REF, directory=directory, command=COMMAND, **options)
    return asyncio.run(run_inspect(opts, GitLabClient(HOST, runner=fake, clock=clock)))


def test_first_run_dumps_failed_logs_and_placeholders_and_a_rerun_costs_one_etag_probe(
    fake: FakeGlab, clock: Clock, tmp_path: Path
) -> None:
    summary = inspect(fake, clock, tmp_path)
    logs = tmp_path / "job-logs"
    failed = logs / "test-unit-tests-2.log"
    assert failed.read_text().endswith("AssertionError: expected 3\n")
    assert summary["failed_jobs"][0]["log"] == str(failed.resolve())
    passed = (logs / "build-build-1.log").read_text()
    skipped = (logs / "test-deploy-3.log").read_text()
    for text in (passed, skipped):
        assert text.startswith(PLACEHOLDER_MARKER + "\n")
        assert COMMAND in text
    assert "the job passed" in passed and "status success" in passed
    assert "was skipped" in skipped
    state = json.loads((tmp_path / "state.json").read_text())
    assert {k: v["log_state"] for k, v in state["logs"].items()} == {
        "1": "placeholder",
        "2": "downloaded",
        "3": "placeholder",
    }
    assert state["snapshot"]["status"] == "failed" and state["dump"]["etag"] == 'W/"tree-1"'
    for name in ("pipeline.json", "jobs.json", "summary.json", "summary.txt", "test-report.json", "README.txt"):
        assert (tmp_path / name).is_file(), name
    assert "placeholders; --logs=all downloads them" in (tmp_path / "summary.txt").read_text()
    assert not any("jobs/1/trace" in e or "jobs/3/trace" in e for e in fake.endpoints())

    fake.calls.clear()
    again = inspect(fake, clock, tmp_path)
    assert fake.endpoints() == ["graphql"]
    assert fake.calls[0].headers["If-None-Match"] == 'W/"tree-1"'
    assert again["failed_jobs"] == summary["failed_jobs"]


def test_logs_all_replaces_the_placeholders_and_keeps_the_logs_already_there(
    fake: FakeGlab, clock: Clock, tmp_path: Path
) -> None:
    inspect(fake, clock, tmp_path)
    fake.calls.clear()
    inspect(fake, clock, tmp_path, logs="all")
    assert (tmp_path / "job-logs" / "build-build-1.log").read_text() == "build ok\n"
    traces = [e for e in fake.endpoints() if "trace" in e]
    assert traces == ["projects/group%2Fproject/jobs/1/trace"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["logs"]["1"]["log_state"] == "downloaded"
    # A skipped job never ran: it keeps its placeholder.
    assert state["logs"]["3"]["log_state"] == "placeholder"


def test_a_changed_tree_refreshes_the_rest_files(fake: FakeGlab, clock: Clock, tmp_path: Path) -> None:
    inspect(fake, clock, tmp_path)
    retried = [*JOBS, rest_job(4, "unit tests", "running")]
    fake.routes[0] = Route(
        "PipelineTree",
        {"data": gql_pipeline(5001, "running", [gql_job(1, "build", "success"), gql_job(4, "unit tests", "running")])},
        etag='W/"tree-2"',
    )
    fake.routes[1] = Route("pipelines/5001/jobs", retried)
    fake.calls.clear()
    inspect(fake, clock, tmp_path)
    assert "projects/group%2Fproject/pipelines/5001/jobs?per_page=100&include_retried=true&page=1" in fake.endpoints()
    running = (tmp_path / "job-logs" / "test-unit-tests-4.log").read_text()
    assert running.startswith(PLACEHOLDER_MARKER) and "still running" in running


def test_a_job_only_the_tree_shows_gets_its_file_too(fake: FakeGlab, clock: Clock, tmp_path: Path) -> None:
    # GitLab's REST job list leaves out some jobs the tree shows, such as a manual job of a later stage.
    tree = gql_pipeline(5001, "failed", [*TREE_JOBS, gql_job(4, "release", "manual", stage="release")])
    fake.routes = [Route("PipelineTree", {"data": tree}, etag='W/"tree-2"'), *routes()[1:]]
    inspect(fake, clock, tmp_path)
    assert (tmp_path / "job-logs" / "release-release-4.log").read_text().startswith(PLACEHOLDER_MARKER)
    assert [j["id"] for j in json.loads((tmp_path / "jobs.json").read_text())] == [1, 2, 3]


def test_partial_context_flags_are_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        pipeline_main(["inspect", "--pipeline-id", "5001", "--project", PROJECT])
    assert exit_info.value.code == 2
    assert "--hostname" in capsys.readouterr().err


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert pipeline_main([]) == 0
    assert "inspect" in capsys.readouterr().out
