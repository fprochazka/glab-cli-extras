from __future__ import annotations

from glab_cli_extras.pipeline.models import (
    Bridge,
    Job,
    Pipeline,
    parse_bridge,
    parse_bridges,
    parse_job,
    parse_jobs,
    parse_pipeline,
)


class TestParsePipeline:
    def test_parse_success_pipeline(self, sample_pipeline_success: dict) -> None:
        p = parse_pipeline(sample_pipeline_success)
        assert isinstance(p, Pipeline)
        assert p.id == 47
        assert p.iid == 12
        assert p.status == "success"
        assert p.source == "push"
        assert p.ref == "main"
        assert p.sha == "a91957a858320c0e17f3a0eca7cfacbff50ea29a"
        assert p.web_url.endswith("/pipelines/47")
        assert p.yaml_errors is None
        assert p.duration == 270
        assert p.queued_duration == 0.5
        assert p.name == "Build pipeline"
        assert p.user == {"id": 1, "username": "alice", "name": "Alice"}

    def test_parse_failed_pipeline(self, sample_pipeline_failed: dict) -> None:
        p = parse_pipeline(sample_pipeline_failed)
        assert p.status == "failed"
        assert p.yaml_errors is None
        assert p.source == "merge_request_event"
        assert p.name is None

    def test_parse_yaml_broken_pipeline(self, sample_pipeline_yaml_broken: dict) -> None:
        p = parse_pipeline(sample_pipeline_yaml_broken)
        assert p.status == "failed"
        assert p.yaml_errors and "unknown keys" in p.yaml_errors
        assert p.iid is None
        assert p.duration is None
        assert p.started_at is None
        assert p.queued_duration is None

    def test_parse_pipeline_missing_optional_fields(self) -> None:
        minimal = {
            "id": 7,
            "status": "success",
            "ref": "main",
            "sha": "abc",
            "web_url": "https://gitlab.example.com/foo/bar/-/pipelines/7",
        }
        p = parse_pipeline(minimal)
        assert p.id == 7
        assert p.iid is None
        assert p.source is None
        assert p.yaml_errors is None
        assert p.created_at is None
        assert p.updated_at is None
        assert p.started_at is None
        assert p.finished_at is None
        assert p.duration is None
        assert p.queued_duration is None
        assert p.name is None
        assert p.user is None


class TestParseJob:
    def test_parse_success_job(self, sample_job_success: dict) -> None:
        j = parse_job(sample_job_success)
        assert isinstance(j, Job)
        assert j.id == 1001
        assert j.name == "build"
        assert j.stage == "build"
        assert j.status == "success"
        assert j.failure_reason is None
        assert j.allow_failure is False
        assert j.duration == 42.5
        assert j.started_at == "2026-05-16T09:00:30.000Z"
        assert j.finished_at == "2026-05-16T09:01:12.500Z"

    def test_parse_failed_script(self, sample_job_failed_script: dict) -> None:
        j = parse_job(sample_job_failed_script)
        assert j.status == "failed"
        assert j.failure_reason == "script_failure"
        assert j.stage == "test"

    def test_parse_failed_runner(self, sample_job_failed_runner: dict) -> None:
        j = parse_job(sample_job_failed_runner)
        assert j.failure_reason == "runner_system_failure"

    def test_parse_failed_missing_dep(self, sample_job_failed_missing_dep: dict) -> None:
        j = parse_job(sample_job_failed_missing_dep)
        assert j.failure_reason == "missing_dependency_failure"
        assert j.duration is None
        assert j.started_at is None
        assert j.finished_at is None

    def test_parse_job_with_archive_artifact(self, sample_job_with_archive: dict) -> None:
        j = parse_job(sample_job_with_archive)
        assert j.has_archive_artifact is True
        assert j.artifacts_expire_at == "2026-06-15T09:00:00.000Z"

    def test_parse_job_with_only_junit_has_no_archive(self, sample_job_with_only_junit: dict) -> None:
        j = parse_job(sample_job_with_only_junit)
        assert j.has_archive_artifact is False
        assert j.artifacts_expire_at == "2026-06-15T09:00:00.000Z"

    def test_parse_job_without_artifacts(self, sample_job_success: dict) -> None:
        j = parse_job(sample_job_success)
        assert j.has_archive_artifact is False
        assert j.artifacts_expire_at is None

    def test_parse_job_missing_optional_fields(self) -> None:
        minimal = {
            "id": 1,
            "name": "x",
            "stage": "build",
            "status": "manual",
            "web_url": "https://gitlab.example.com/foo/bar/-/jobs/1",
        }
        j = parse_job(minimal)
        assert j.failure_reason is None
        assert j.allow_failure is False
        assert j.duration is None
        assert j.created_at is None


class TestParseBridge:
    def test_parse_success_bridge(self, sample_bridge_success: dict) -> None:
        b = parse_bridge(sample_bridge_success)
        assert isinstance(b, Bridge)
        assert b.id == 2001
        assert b.name == "trigger:downstream-ok"
        assert b.status == "success"
        assert b.failure_reason is None
        assert b.downstream_pipeline is not None
        assert b.downstream_pipeline["id"] == 9001
        assert b.downstream_pipeline["status"] == "success"

    def test_parse_failed_bridge(self, sample_bridge_failed: dict) -> None:
        b = parse_bridge(sample_bridge_failed)
        assert b.status == "failed"
        assert b.failure_reason == "downstream_pipeline_creation_failed"
        assert b.downstream_pipeline is not None
        assert b.downstream_pipeline["status"] == "failed"

    def test_parse_bridge_missing_optional_fields(self) -> None:
        minimal = {
            "id": 3,
            "name": "trig",
            "stage": "deploy",
            "status": "created",
        }
        b = parse_bridge(minimal)
        assert b.failure_reason is None
        assert b.downstream_pipeline is None


class TestParseLists:
    def test_parse_jobs_preserves_order(
        self,
        sample_job_success: dict,
        sample_job_failed_script: dict,
        sample_job_failed_runner: dict,
    ) -> None:
        jobs = parse_jobs([sample_job_success, sample_job_failed_script, sample_job_failed_runner])
        assert [j.id for j in jobs] == [1001, 1002, 1003]
        assert all(isinstance(j, Job) for j in jobs)

    def test_parse_jobs_empty(self) -> None:
        assert parse_jobs([]) == []

    def test_parse_bridges_preserves_order(self, sample_bridge_success: dict, sample_bridge_failed: dict) -> None:
        bridges = parse_bridges([sample_bridge_failed, sample_bridge_success])
        assert [b.id for b in bridges] == [2002, 2001]

    def test_parse_bridges_empty(self) -> None:
        assert parse_bridges([]) == []
