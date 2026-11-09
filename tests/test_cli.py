from __future__ import annotations

import pytest
from typer.testing import CliRunner

from glab_cli_extras.cli.watch import app, parse_duration

runner = CliRunner()
PIPELINE_URL = "https://gitlab.example.com/group/project/-/pipelines/1"
MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/42"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["mr", MR_URL, "--events=stauts"], "unknown event kind or group 'stauts'"),
        (["mr", MR_URL, "--exclude-events=lifecycle"], "cannot be excluded"),
        (
            ["pipeline", "https://gitlab.example.com/group/project/-/pipelines/1", "--events=comments"],
            "no events for a pipeline watch",
        ),
        (["mr", MR_URL, "--until=mr_opened"], "never produces 'mr_opened'"),
        (["mr", MR_URL, "--events=status", "--until=pipeline_finished"], "filtered out"),
        (["mr", MR_URL, "--until=job_finished"], "never produces 'job_finished'"),
        (
            ["pipeline", "https://gitlab.example.com/group/project/-/pipelines/1", "--logs=some"],
            "--logs must be one of",
        ),
        (["mr", MR_URL, "--events-stream=email"], "--events-stream must be one of"),
        (["mr", MR_URL, "--message-priority=urgent"], "--message-priority must be one of"),
        (["mr", MR_URL, "--timeout=soon"], "cannot read the duration"),
        (["branch", "a", "b", "c"], "expected [REPO] BRANCH"),
    ],
)
def test_bad_options_fail_before_anything_starts(args: list[str], message: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert message in result.output


@pytest.mark.parametrize(("text", "seconds"), [("90", 90), ("90s", 90), ("30m", 1800), ("1h30m", 5400), ("2h", 7200)])
def test_durations(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


def test_daemon_status_without_a_daemon() -> None:
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == 0
    assert "not running" in result.output


def test_version() -> None:
    assert runner.invoke(app, ["--version"]).output.strip() == "glab-watch 0.2.0"
