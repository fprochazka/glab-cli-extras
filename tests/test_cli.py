from __future__ import annotations

import importlib

import pytest
from typer.testing import CliRunner

from glab_cli_extras import __version__
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
        (["mr", MR_URL, "--events=quiet"], "'quiet' is reported only with --quiet-after"),
        (["mr", MR_URL, "--events=pipeline_stuck"], "'pipeline_stuck' is reported only with --stuck-after"),
        (["mr", MR_URL, "--quiet-after=2m", "--exclude-events=status"], "'quiet' is filtered out"),
        (["mr", MR_URL, "--stuck-after=30m", "--events=status"], "'pipeline_stuck' is filtered out"),
        (["mr", MR_URL, "--quiet-after=soon"], "--quiet-after: cannot read the duration"),
        (["mr", MR_URL, "--quiet-after=0s"], "longer than zero"),
        (["mrs", "group/project", "--idle-after=later"], "--idle-after: cannot read the duration"),
        (["pipeline", PIPELINE_URL, "--stuck-after=0"], "longer than zero"),
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
    assert runner.invoke(app, ["--version"]).output.strip() == f"glab-watch {__version__}"


@pytest.mark.parametrize("command", ["pipeline", "discussion"])
def test_glab_pipeline_and_glab_discussion_name_the_package_in_their_version(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plugin's session check tells these apart from the deprecated standalone packages by this line."""
    module = importlib.import_module(f"glab_cli_extras.cli.{command}")
    with pytest.raises(SystemExit) as raised:
        module.main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == f"glab-cli-extras {__version__}\n"
