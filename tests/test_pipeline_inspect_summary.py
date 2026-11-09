from __future__ import annotations

from pathlib import Path

from glab_cli_extras.pipeline.models import parse_bridge, parse_job, parse_pipeline
from glab_cli_extras.pipeline.summary import (
    SummaryInputs,
    build_summary_dict,
    decide_extras,
    format_summary,
)


def _fmt(summary_inputs):
    return format_summary(build_summary_dict(summary_inputs))


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


def test_green_summary_base_only(sample_pipeline_success, sample_job_success):
    p = parse_pipeline(sample_pipeline_success)
    jobs = [parse_job(sample_job_success)]
    out = _fmt(_make(p, jobs))
    assert "PIPELINE 47" in out
    assert "Files Created:" in out
    assert "All files in:" in out
    assert "Failed Jobs" not in out
    assert "YAML Errors" not in out
    assert "Failed Downstream" not in out
    assert "Test Failures" not in out


def test_yaml_broken_summary(sample_pipeline_yaml_broken):
    p = parse_pipeline(sample_pipeline_yaml_broken)
    out = _fmt(_make(p, has_lint_file=True))
    assert "YAML Errors:" in out
    assert "  jobs:build config contains unknown keys: scriptz" in out
    assert "→ lint.json, merged.yml" in out


def test_script_failure_no_yaml_hint(sample_pipeline_failed, sample_job_failed_script):
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_script)
    out = _fmt(_make(p, [j]))
    assert "Failed Jobs (1):" in out
    assert "rspec:unit" in out
    assert "reason=script_failure" in out
    assert "Log:" in out
    assert "likely YAML/needs issue" not in out


def test_missing_dep_has_yaml_hint(sample_pipeline_failed, sample_job_failed_missing_dep):
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_missing_dep)
    out = _fmt(_make(p, [j], has_lint_file=True))
    assert "Failed Jobs (1):" in out
    assert "reason=missing_dependency_failure" in out
    assert "→ likely YAML/needs issue, see lint.json" in out


def test_failed_bridge_summary(sample_pipeline_failed, sample_bridge_failed):
    p = parse_pipeline(sample_pipeline_failed)
    b = parse_bridge(sample_bridge_failed)
    output_dir = Path("/tmp/g")
    detail_path = output_dir / "downstream" / "trigger-downstream-bad-9002.json"
    out = _fmt(
        _make(
            p,
            bridges=[b],
            output_dir=output_dir,
            downstream_paths={b.id: detail_path},
        )
    )
    assert "Failed Downstream Pipelines (1):" in out
    assert "trigger:downstream-bad" in out
    assert "pipeline 9002" in out
    assert str(detail_path) in out


def test_test_report_counts(sample_pipeline_failed, sample_job_failed_script):
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_script)
    out = _fmt(
        _make(
            p,
            [j],
            has_test_report_file=True,
            test_report={"total": {"failed": 12, "count": 1843}},
        )
    )
    assert "Test Failures (12 failed / 1843 total):" in out


def test_test_report_without_totals_fallback(sample_pipeline_failed, sample_job_failed_script):
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_script)
    out = _fmt(_make(p, [j], has_test_report_file=True, test_report={}))
    assert "Test failures present — see test-report.json" in out


def test_mixed_summary_all_sections_in_order(
    sample_pipeline_failed,
    sample_job_failed_script,
    sample_job_failed_missing_dep,
    sample_bridge_failed,
):
    p = parse_pipeline(sample_pipeline_failed)
    jobs = [
        parse_job(sample_job_failed_script),
        parse_job(sample_job_failed_missing_dep),
    ]
    b = parse_bridge(sample_bridge_failed)
    out = _fmt(
        _make(
            p,
            jobs,
            [b],
            has_lint_file=True,
            has_test_report_file=True,
            test_report={"total": {"failed": 1, "count": 10}},
            downstream_paths={b.id: Path("/tmp/g/downstream/x.json")},
        )
    )
    # All sections present
    assert "Failed Jobs (2):" in out
    assert "Failed Downstream Pipelines (1):" in out
    assert "Test Failures (1 failed / 10 total):" in out
    # Ordering: failed jobs before downstream before test failures
    idx_jobs = out.index("Failed Jobs")
    idx_down = out.index("Failed Downstream")
    idx_test = out.index("Test Failures")
    assert idx_jobs < idx_down < idx_test


def test_bridges_listed_in_files_created(sample_pipeline_success, sample_bridge_success):
    p = parse_pipeline(sample_pipeline_success)
    b = parse_bridge(sample_bridge_success)
    out = _fmt(_make(p, bridges=[b]))
    assert "Bridges:" in out
    assert "1 bridges" in out
