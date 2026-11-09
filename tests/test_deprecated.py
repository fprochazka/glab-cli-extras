"""The notice glab-pipeline and glab-discussion print when the deprecated standalone package is installed alongside."""

from __future__ import annotations

import importlib
import importlib.metadata
import io
from typing import Any

import pytest

from glab_cli_extras.cli import deprecated

LINES = {
    "glab-pipeline": "glab-pipeline: this package is deprecated; glab-cli-extras provides the same command."
    " Run: uv tool uninstall glab-pipeline && uv tool install glab-cli-extras\n",
    "glab-discussion": "glab-discussion: this package is deprecated; glab-cli-extras provides the same command."
    " Run: uv tool uninstall glab-discussion && uv tool install glab-cli-extras\n",
}


def installed(*names: str) -> Any:
    def distribution(name: str) -> object:
        if name in names:
            return object()
        raise importlib.metadata.PackageNotFoundError(name)

    return distribution


@pytest.mark.parametrize("command", sorted(LINES))
def test_the_notice_is_one_exact_line_on_stderr(command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "distribution", installed(command))
    err = io.StringIO()
    deprecated.warn_if_installed(command, err)
    assert err.getvalue() == LINES[command]


def test_no_notice_without_the_old_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "distribution", installed())
    err = io.StringIO()
    deprecated.warn_if_installed("glab-pipeline", err)
    assert err.getvalue() == ""


def test_the_lookup_failing_in_any_way_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(name: str) -> object:
        raise OSError("unreadable site-packages")

    monkeypatch.setattr(importlib.metadata, "distribution", broken)
    err = io.StringIO()
    deprecated.warn_if_installed("glab-discussion", err)
    assert err.getvalue() == ""


def test_glab_cli_extras_alone_has_no_old_package_installed() -> None:
    """The real lookup, in this environment, where neither old package is installed."""
    err = io.StringIO()
    for command in LINES:
        deprecated.warn_if_installed(command, err)
    assert err.getvalue() == ""


@pytest.mark.parametrize(("command", "module"), [("glab-pipeline", "pipeline"), ("glab-discussion", "discussion")])
def test_the_commands_print_it_before_running_and_leave_stdout_and_the_exit_code_alone(
    command: str, module: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main = importlib.import_module(f"glab_cli_extras.cli.{module}").main
    with pytest.raises(SystemExit) as without:
        main(["--version"])
    plain = capsys.readouterr()
    monkeypatch.setattr(importlib.metadata, "distribution", installed(command))
    with pytest.raises(SystemExit) as with_old:
        main(["--version"])
    noted = capsys.readouterr()
    assert with_old.value.code == without.value.code == 0
    assert noted.out == plain.out
    assert noted.err == LINES[command] + plain.err
