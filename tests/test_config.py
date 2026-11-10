"""The settings: where they are read from, how the sources combine, and how strictly the files are checked."""

from __future__ import annotations

from pathlib import Path

import pytest

from glab_cli_extras import config
from glab_cli_extras.config import QUICK_ACTIONS, ConfigError, load_settings


def _system(tmp_path: Path) -> Path:
    path = config.system_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _user(tmp_path: Path) -> Path:
    path = tmp_path / "config" / "glab-cli-extras" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_no_source_means_the_defaults() -> None:
    settings = load_settings()
    assert settings.get(QUICK_ACTIONS) == "allow"
    assert settings.source(QUICK_ACTIONS) is None
    assert not settings.quick_actions_rejected


def test_the_user_file_is_under_xdg_config_home(tmp_path: Path) -> None:
    assert config.user_config_file() == _user(tmp_path)


def test_a_relative_xdg_config_home_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert config.user_config_file() == Path.home() / ".config" / "glab-cli-extras" / "config.toml"


@pytest.mark.parametrize("where", ["system", "user", "explicit"])
def test_each_file_can_set_a_restriction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    if where == "system":
        path = _system(tmp_path)
    elif where == "user":
        path = _user(tmp_path)
    else:
        path = tmp_path / "agent.toml"
        monkeypatch.setenv("GLAB_CLI_EXTRAS_CONFIG", str(path))
    path.write_text('[discussion]\nquick_actions = "reject"\n')
    settings = load_settings()
    assert settings.quick_actions_rejected
    assert settings.describe(QUICK_ACTIONS) == f'discussion.quick_actions = "reject" (from {path})'


def test_the_environment_variable_sets_a_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS", "reject")
    settings = load_settings()
    assert settings.quick_actions_rejected
    assert settings.source(QUICK_ACTIONS) == "GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS"


def test_no_source_loosens_a_restriction_another_one_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _system(tmp_path).write_text('[discussion]\nquick_actions = "reject"\n')
    _user(tmp_path).write_text('[discussion]\nquick_actions = "allow"\n')
    explicit = tmp_path / "agent.toml"
    explicit.write_text('[discussion]\nquick_actions = "allow"\n')
    monkeypatch.setenv("GLAB_CLI_EXTRAS_CONFIG", str(explicit))
    monkeypatch.setenv("GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS", "allow")
    settings = load_settings()
    assert settings.quick_actions_rejected
    assert settings.source(QUICK_ACTIONS) == str(config.system_config_file())


def test_an_empty_environment_variable_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS", "")
    assert not load_settings().quick_actions_rejected


def test_an_empty_file_sets_nothing(tmp_path: Path) -> None:
    _user(tmp_path).write_text("")
    assert not load_settings().quick_actions_rejected


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            '[discussion]\nquick_actions = "deny"\n',
            'discussion.quick_actions is \'deny\'; set it to "allow" or "reject"',
        ),
        ("[discussion]\nquick_actions = true\n", "discussion.quick_actions is True"),
        ('[discussion]\nquick_action = "reject"\n', "unknown setting discussion.quick_action; [discussion] knows"),
        ('[discusion]\nquick_actions = "reject"\n', "unknown setting 'discusion'"),
        ('quick_actions = "reject"\n', "unknown setting 'quick_actions'"),
        ("[discussion\n", "is not valid TOML"),
    ],
)
def test_a_file_with_an_unknown_table_key_or_value_is_an_error(tmp_path: Path, content: str, message: str) -> None:
    path = _user(tmp_path)
    path.write_text(content)
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert str(path) in str(exc_info.value) and message in str(exc_info.value)


def test_an_unknown_value_in_the_environment_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS", "no")
    with pytest.raises(ConfigError, match="GLAB_CLI_EXTRAS_DISCUSSION_QUICK_ACTIONS: discussion.quick_actions is 'no'"):
        load_settings()


def test_an_explicit_file_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLAB_CLI_EXTRAS_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ConfigError, match="GLAB_CLI_EXTRAS_CONFIG names .*missing.toml, which is not a file"):
        load_settings()


def test_the_drafts_setting_combines_like_every_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from glab_cli_extras.config import DRAFTS

    _user(tmp_path).write_text('[discussion]\ndrafts = "enabled"\nquick_actions = "allow"\n')
    assert not load_settings().drafts_disabled
    monkeypatch.setenv("GLAB_CLI_EXTRAS_DISCUSSION_DRAFTS", "disabled")
    settings = load_settings()
    assert settings.drafts_disabled and not settings.quick_actions_rejected
    assert settings.source(DRAFTS) == "GLAB_CLI_EXTRAS_DISCUSSION_DRAFTS"
