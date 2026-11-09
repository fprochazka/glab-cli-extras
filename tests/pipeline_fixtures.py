from __future__ import annotations

import pytest


@pytest.fixture
def sample_pipeline_success() -> dict:
    return {
        "id": 47,
        "iid": 12,
        "project_id": 1,
        "status": "success",
        "source": "push",
        "ref": "main",
        "sha": "a91957a858320c0e17f3a0eca7cfacbff50ea29a",
        "web_url": "https://gitlab.example.com/foo/bar/-/pipelines/47",
        "yaml_errors": None,
        "created_at": "2026-05-16T09:00:00.000Z",
        "updated_at": "2026-05-16T09:05:00.000Z",
        "started_at": "2026-05-16T09:00:30.000Z",
        "finished_at": "2026-05-16T09:05:00.000Z",
        "duration": 270,
        "queued_duration": 0.5,
        "name": "Build pipeline",
        "user": {"id": 1, "username": "alice", "name": "Alice"},
        # extra unknown field — parser must ignore
        "coverage": "98.5",
    }


@pytest.fixture
def sample_pipeline_failed() -> dict:
    return {
        "id": 48,
        "iid": 13,
        "project_id": 1,
        "status": "failed",
        "source": "merge_request_event",
        "ref": "feature/x",
        "sha": "bb91957a858320c0e17f3a0eca7cfacbff50ea29",
        "web_url": "https://gitlab.example.com/foo/bar/-/pipelines/48",
        "yaml_errors": None,
        "created_at": "2026-05-16T10:00:00.000Z",
        "updated_at": "2026-05-16T10:04:12.000Z",
        "started_at": "2026-05-16T10:00:30.000Z",
        "finished_at": "2026-05-16T10:04:12.000Z",
        "duration": 252,
        "queued_duration": 0.7,
        "name": None,
        "user": {"id": 2, "username": "bob", "name": "Bob"},
    }


@pytest.fixture
def sample_pipeline_yaml_broken() -> dict:
    return {
        "id": 49,
        "iid": None,
        "project_id": 1,
        "status": "failed",
        "source": "push",
        "ref": "broken-yaml",
        "sha": "ccc1957a858320c0e17f3a0eca7cfacbff50ea29",
        "web_url": "https://gitlab.example.com/foo/bar/-/pipelines/49",
        "yaml_errors": "jobs:build config contains unknown keys: scriptz",
        "created_at": "2026-05-16T11:00:00.000Z",
        "updated_at": "2026-05-16T11:00:01.000Z",
        "started_at": None,
        "finished_at": "2026-05-16T11:00:01.000Z",
        "duration": None,
        "queued_duration": None,
        "name": None,
        "user": {"id": 2, "username": "bob", "name": "Bob"},
    }


@pytest.fixture
def sample_job_success() -> dict:
    return {
        "id": 1001,
        "name": "build",
        "stage": "build",
        "status": "success",
        "failure_reason": None,
        "allow_failure": False,
        "web_url": "https://gitlab.example.com/foo/bar/-/jobs/1001",
        "duration": 42.5,
        "started_at": "2026-05-16T09:00:30.000Z",
        "finished_at": "2026-05-16T09:01:12.500Z",
        "created_at": "2026-05-16T09:00:00.000Z",
        # extras the parser must ignore
        "ref": "main",
        "tag": False,
    }


@pytest.fixture
def sample_job_failed_script() -> dict:
    return {
        "id": 1002,
        "name": "rspec:unit",
        "stage": "test",
        "status": "failed",
        "failure_reason": "script_failure",
        "allow_failure": False,
        "web_url": "https://gitlab.example.com/foo/bar/-/jobs/1002",
        "duration": 88.1,
        "started_at": "2026-05-16T09:01:30.000Z",
        "finished_at": "2026-05-16T09:02:58.100Z",
        "created_at": "2026-05-16T09:00:00.000Z",
    }


@pytest.fixture
def sample_job_failed_runner() -> dict:
    return {
        "id": 1003,
        "name": "deploy",
        "stage": "deploy",
        "status": "failed",
        "failure_reason": "runner_system_failure",
        "allow_failure": False,
        "web_url": "https://gitlab.example.com/foo/bar/-/jobs/1003",
        "duration": 5.0,
        "started_at": "2026-05-16T09:03:00.000Z",
        "finished_at": "2026-05-16T09:03:05.000Z",
        "created_at": "2026-05-16T09:00:00.000Z",
    }


@pytest.fixture
def sample_job_failed_missing_dep() -> dict:
    return {
        "id": 1004,
        "name": "package",
        "stage": "package",
        "status": "failed",
        "failure_reason": "missing_dependency_failure",
        "allow_failure": False,
        "web_url": "https://gitlab.example.com/foo/bar/-/jobs/1004",
        "duration": None,
        "started_at": None,
        "finished_at": None,
        "created_at": "2026-05-16T09:00:00.000Z",
    }


@pytest.fixture
def sample_job_with_archive() -> dict:
    return {
        "id": 1005,
        "name": "build:assets",
        "stage": "build",
        "status": "success",
        "failure_reason": None,
        "allow_failure": False,
        "web_url": "https://gitlab.example.com/foo/bar/-/jobs/1005",
        "duration": 30.0,
        "started_at": "2026-05-16T09:00:30.000Z",
        "finished_at": "2026-05-16T09:01:00.000Z",
        "created_at": "2026-05-16T09:00:00.000Z",
        "artifacts_expire_at": "2026-06-15T09:00:00.000Z",
        "artifacts_file": {"filename": "artifacts.zip", "size": 4096},
        "artifacts": [
            {"file_type": "archive", "size": 4096, "filename": "artifacts.zip", "file_format": "zip"},
            {"file_type": "metadata", "size": 128, "filename": "metadata.gz", "file_format": "gzip"},
        ],
    }


@pytest.fixture
def sample_job_with_only_junit() -> dict:
    return {
        "id": 1006,
        "name": "rspec:report",
        "stage": "test",
        "status": "success",
        "failure_reason": None,
        "allow_failure": False,
        "web_url": "https://gitlab.example.com/foo/bar/-/jobs/1006",
        "duration": 10.0,
        "started_at": "2026-05-16T09:02:00.000Z",
        "finished_at": "2026-05-16T09:02:10.000Z",
        "created_at": "2026-05-16T09:00:00.000Z",
        "artifacts_expire_at": "2026-06-15T09:00:00.000Z",
        "artifacts": [
            {"file_type": "junit", "size": 256, "filename": "junit.xml.gz", "file_format": "gzip"},
        ],
    }


@pytest.fixture
def sample_bridge_success() -> dict:
    return {
        "id": 2001,
        "name": "trigger:downstream-ok",
        "stage": "deploy",
        "status": "success",
        "failure_reason": None,
        "downstream_pipeline": {
            "id": 9001,
            "status": "success",
            "web_url": "https://gitlab.example.com/foo/downstream/-/pipelines/9001",
            "ref": "main",
            "sha": "deadbeefcafebabe",
        },
    }


@pytest.fixture
def sample_bridge_failed() -> dict:
    return {
        "id": 2002,
        "name": "trigger:downstream-bad",
        "stage": "deploy",
        "status": "failed",
        "failure_reason": "downstream_pipeline_creation_failed",
        "downstream_pipeline": {
            "id": 9002,
            "status": "failed",
            "web_url": "https://gitlab.example.com/foo/downstream/-/pipelines/9002",
            "ref": "main",
            "sha": "f00dbabef00dbabe",
        },
    }


@pytest.fixture
def sample_mr_with_head_pipeline() -> dict:
    return {
        "id": 5000,
        "iid": 42,
        "project_id": 1,
        "title": "Add new feature",
        "web_url": "https://gitlab.example.com/foo/bar/-/merge_requests/42",
        "source_branch": "feature/x",
        "target_branch": "main",
        "sha": "bb91957a858320c0e17f3a0eca7cfacbff50ea29",
        "head_pipeline": {
            "id": 48,
            "iid": 13,
            "project_id": 1,
            "status": "failed",
            "ref": "feature/x",
            "sha": "bb91957a858320c0e17f3a0eca7cfacbff50ea29",
            "web_url": "https://gitlab.example.com/foo/bar/-/pipelines/48",
        },
    }
