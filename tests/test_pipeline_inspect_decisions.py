from __future__ import annotations

from glab_cli_extras.pipeline.models import Bridge, Job, parse_bridge, parse_job, parse_pipeline
from glab_cli_extras.pipeline.summary import decide_extras, is_test_job


def _job(**overrides) -> Job:
    base = {
        "id": 1,
        "name": "build",
        "stage": "build",
        "status": "success",
        "failure_reason": None,
        "allow_failure": False,
        "web_url": "https://gitlab.example.com/x/y/-/jobs/1",
        "duration": 1.0,
        "started_at": None,
        "finished_at": None,
        "created_at": None,
    }
    base.update(overrides)
    return parse_job(base)


def _bridge(**overrides) -> Bridge:
    base = {
        "id": 1,
        "name": "trigger:x",
        "stage": "deploy",
        "status": "success",
        "failure_reason": None,
        "downstream_pipeline": None,
    }
    base.update(overrides)
    return parse_bridge(base)


def test_green_pipeline_no_extras(sample_pipeline_success, sample_job_success):
    p = parse_pipeline(sample_pipeline_success)
    jobs = [parse_job(sample_job_success)]
    plan = decide_extras(p, jobs, [])
    assert not plan.need_lint
    assert not plan.need_test_report
    assert not plan.need_downstream
    assert plan.yaml_hint_jobs == ()
    assert plan.failed_test_jobs == ()
    assert plan.failed_bridges == ()


def test_yaml_errors_triggers_lint(sample_pipeline_yaml_broken):
    p = parse_pipeline(sample_pipeline_yaml_broken)
    plan = decide_extras(p, [], [])
    assert plan.need_lint


def test_zero_jobs_triggers_lint(sample_pipeline_failed):
    p = parse_pipeline(sample_pipeline_failed)
    plan = decide_extras(p, [], [])
    assert plan.need_lint


def test_script_failure_does_not_trigger_lint(sample_pipeline_failed, sample_job_failed_script):
    p = parse_pipeline(sample_pipeline_failed)
    jobs = [parse_job(sample_job_failed_script)]
    plan = decide_extras(p, jobs, [])
    assert not plan.need_lint
    assert plan.yaml_hint_jobs == ()


def test_missing_dependency_triggers_lint_and_hint(sample_pipeline_failed, sample_job_failed_missing_dep):
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_missing_dep)
    plan = decide_extras(p, [j], [])
    assert plan.need_lint
    assert plan.yaml_hint_jobs == (j.id,)


def test_failed_test_stage_job_triggers_report(sample_pipeline_failed, sample_job_failed_script):
    # sample_job_failed_script has stage=test
    p = parse_pipeline(sample_pipeline_failed)
    j = parse_job(sample_job_failed_script)
    plan = decide_extras(p, [j], [])
    assert plan.need_test_report
    assert plan.failed_test_jobs == (j.id,)


def test_failed_job_name_pytest_in_qa_stage_triggers_report(sample_pipeline_failed):
    p = parse_pipeline(sample_pipeline_failed)
    j = _job(id=99, name="pytest-unit", stage="qa", status="failed", failure_reason="script_failure")
    plan = decide_extras(p, [j], [])
    assert plan.need_test_report
    assert plan.failed_test_jobs == (99,)


def test_failed_bridge_triggers_downstream(sample_pipeline_failed, sample_bridge_failed):
    p = parse_pipeline(sample_pipeline_failed)
    b = parse_bridge(sample_bridge_failed)
    plan = decide_extras(p, [], [b])
    assert plan.need_downstream
    assert plan.failed_bridges == (b.id,)


def test_manual_bridge_not_a_failure(sample_pipeline_failed):
    p = parse_pipeline(sample_pipeline_failed)
    b = _bridge(id=77, status="manual")
    plan = decide_extras(p, [], [b])
    assert not plan.need_downstream
    assert plan.failed_bridges == ()


def test_skipped_bridge_not_a_failure(sample_pipeline_failed):
    p = parse_pipeline(sample_pipeline_failed)
    b = _bridge(id=78, status="skipped")
    plan = decide_extras(p, [], [b])
    assert not plan.need_downstream


def test_force_lint_forces_lint(sample_pipeline_success, sample_job_success):
    p = parse_pipeline(sample_pipeline_success)
    plan = decide_extras(p, [parse_job(sample_job_success)], [], force_lint=True)
    assert plan.need_lint
    assert not plan.need_test_report
    assert not plan.need_downstream


def test_force_test_report_forces_test_report(sample_pipeline_success, sample_job_success):
    p = parse_pipeline(sample_pipeline_success)
    plan = decide_extras(p, [parse_job(sample_job_success)], [], force_test_report=True)
    assert not plan.need_lint
    assert plan.need_test_report
    assert not plan.need_downstream


def test_force_downstream_forces_downstream_no_bridges(sample_pipeline_success, sample_job_success):
    p = parse_pipeline(sample_pipeline_success)
    plan = decide_extras(p, [parse_job(sample_job_success)], [], force_downstream=True)
    assert not plan.need_lint
    assert not plan.need_test_report
    assert plan.need_downstream
    assert plan.downstream_bridges == ()


def test_force_downstream_expands_to_all_bridges_with_downstream(sample_pipeline_success, sample_bridge_success):
    p = parse_pipeline(sample_pipeline_success)
    b = parse_bridge(sample_bridge_success)
    plan = decide_extras(p, [], [b], force_downstream=True)
    assert plan.need_downstream
    # state-heuristic failed_bridges stays as-is (b is success → empty)
    assert plan.failed_bridges == ()
    # but downstream_bridges contains every bridge with a downstream_pipeline
    assert plan.downstream_bridges == (b.id,)


def test_downstream_bridges_matches_failed_when_not_forced(sample_pipeline_failed, sample_bridge_failed):
    p = parse_pipeline(sample_pipeline_failed)
    b = parse_bridge(sample_bridge_failed)
    plan = decide_extras(p, [], [b])
    assert plan.failed_bridges == (b.id,)
    assert plan.downstream_bridges == (b.id,)


def test_is_test_job_by_stage():
    assert is_test_job(_job(stage="test", name="foo"))
    assert is_test_job(_job(stage="spec-runner", name="foo"))
    assert is_test_job(_job(stage="qa", name="foo"))
    assert not is_test_job(_job(stage="build", name="compile"))


def test_is_test_job_by_name():
    assert is_test_job(_job(stage="build", name="pytest-unit"))
    assert is_test_job(_job(stage="build", name="rspec:unit"))
    assert is_test_job(_job(stage="build", name="vitest"))
    assert not is_test_job(_job(stage="build", name="package"))
