from __future__ import annotations

from pathlib import Path

from glab_cli_extras.pipeline.models import parse_bridge, parse_job, parse_pipeline
from glab_cli_extras.pipeline.summary import (
    SummaryInputs,
    build_summary_dict,
    decide_extras,
)


def _make(
    pipeline,
    jobs=None,
    bridges=None,
    *,
    output_dir=Path("/tmp/glab-pipeline-test"),
    has_lint_file=False,
    has_test_report_file=False,
    test_report=None,
    downstream_paths=None,
    job_log_paths=None,
):
    jobs = jobs or []
    bridges = bridges or []
    plan = decide_extras(pipeline, jobs, bridges)
    return SummaryInputs(
        pipeline=pipeline,
        jobs=jobs,
        bridges=bridges,
        plan=plan,
        output_dir=output_dir,
        job_log_paths=job_log_paths or {j.id: output_dir / "job-logs" / f"{j.stage}-{j.name}-{j.id}.log" for j in jobs},
        downstream_paths=downstream_paths or {},
        has_lint_file=has_lint_file,
        has_test_report_file=has_test_report_file,
        test_report=test_report,
    )


def test_green_pipeline_minimal_shape(sample_pipeline_success, sample_job_success):
    p = parse_pipeline(sample_pipeline_success)
    jobs = [parse_job(sample_job_success)]
    d = build_summary_dict(_make(p, jobs))

    assert d["failed_jobs"] == []
    assert d["yaml_errors"] is None
    assert d["failed_downstream"] == []
    assert d["test_failures"] is None

    files = d["files"]
    assert "pipeline" in files
    assert "jobs" in files
    assert files["jobs_count"] == 1
    # optional files keys are absent
    for k in ("bridges", "lint", "merged_yaml", "test_report"):
        assert k not in files

    # pipeline section
    assert d["pipeline"]["id"] == 47
    assert d["pipeline"]["status"] == "success"
    assert d["pipeline"]["sha_short"] == "a91957a"


def test_yaml_broken_sets_yaml_errors_and_lint(sample_pipeline_yaml_broken, tmp_path):
    p = parse_pipeline(sample_pipeline_yaml_broken)
    # write a real merged.yml so we can verify it's picked up
    (tmp_path / "merged.yml").write_text("hello: world\n")
    d = build_summary_dict(_make(p, has_lint_file=True, output_dir=tmp_path))

    assert d["yaml_errors"] == ("jobs:build config contains unknown keys: scriptz")
    assert "lint" in d["files"]
    assert "merged_yaml" in d["files"]
    assert d["files"]["merged_yaml"].endswith("merged.yml")


def test_script_failure_no_yaml_hint(sample_pipeline_failed, sample_job_failed_script):
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_script)
    d = build_summary_dict(_make(p, [j]))

    assert len(d["failed_jobs"]) == 1
    entry = d["failed_jobs"][0]
    assert entry["id"] == j.id
    assert entry["name"] == "rspec:unit"
    assert entry["stage"] == "test"
    assert entry["failure_reason"] == "script_failure"
    assert entry["yaml_hint"] is False
    assert entry["log"] is not None
    assert Path(entry["log"]).is_absolute()


def test_missing_dep_has_yaml_hint(sample_pipeline_failed, sample_job_failed_missing_dep):
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_missing_dep)
    d = build_summary_dict(_make(p, [j], has_lint_file=True))

    assert len(d["failed_jobs"]) == 1
    assert d["failed_jobs"][0]["yaml_hint"] is True
    assert "lint" in d["files"]


def test_failed_downstream_with_detail_path(sample_pipeline_failed, sample_bridge_failed):
    p = parse_pipeline(sample_pipeline_failed)
    b = parse_bridge(sample_bridge_failed)
    output_dir = Path("/tmp/g")
    detail = output_dir / "downstream" / "trigger-downstream-bad-9002.json"
    d = build_summary_dict(
        _make(
            p,
            bridges=[b],
            output_dir=output_dir,
            downstream_paths={b.id: detail},
        )
    )

    assert len(d["failed_downstream"]) == 1
    entry = d["failed_downstream"][0]
    assert entry["bridge_id"] == b.id
    assert entry["bridge_name"] == "trigger:downstream-bad"
    assert entry["downstream_pipeline_id"] == 9002
    assert entry["downstream_status"] == "failed"
    assert entry["detail"] == str(detail.resolve())
    assert Path(entry["detail"]).is_absolute()


def test_failed_downstream_without_detail_is_null(sample_pipeline_failed, sample_bridge_failed):
    p = parse_pipeline(sample_pipeline_failed)
    b = parse_bridge(sample_bridge_failed)
    d = build_summary_dict(_make(p, bridges=[b]))

    assert len(d["failed_downstream"]) == 1
    assert d["failed_downstream"][0]["detail"] is None


def test_test_report_present(sample_pipeline_failed, sample_job_failed_script):
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_script)
    d = build_summary_dict(
        _make(
            p,
            [j],
            has_test_report_file=True,
            test_report={"total": {"failed": 12, "count": 1843}},
        )
    )
    assert d["test_failures"] == {"failed": 12, "total": 1843}
    assert "test_report" in d["files"]


def test_all_paths_absolute(sample_pipeline_success, sample_job_success):
    p = parse_pipeline(sample_pipeline_success)
    jobs = [parse_job(sample_job_success)]
    d = build_summary_dict(_make(p, jobs))

    assert Path(d["output_dir"]).is_absolute()
    assert Path(d["files"]["pipeline"]).is_absolute()
    assert Path(d["files"]["jobs"]).is_absolute()
