from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakeglab import FakeGlab  # noqa: E402
from glab_cli_extras import config  # noqa: E402
from glab_cli_extras.discussion.models import MrContext  # noqa: E402
from glab_cli_extras.gitlab.client import GitLabClient  # noqa: E402
from glab_cli_extras.resources.base import PollContext  # noqa: E402
from shapes import HOST  # noqa: E402

pytest_plugins = ["pipeline_fixtures"]


class Clock:
    def __init__(self, now: float = 1_794_220_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def mr_context() -> MrContext:
    """The MR of the ported glab-discussion command tests, which patch the API calls of each command."""
    return MrContext(
        "gitlab.example.com", 42, "group/project", 7, "https://gitlab.example.com/group/project/-/merge_requests/7"
    )


@pytest.fixture
def fake() -> FakeGlab:
    return FakeGlab()


@pytest.fixture
def ctx(fake: FakeGlab, clock: Clock, tmp_path: Path) -> PollContext:
    return PollContext(GitLabClient(HOST, runner=fake, clock=clock), clock)


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may touch the real daemon, base dir, cache dir or Claude session."""
    monkeypatch.setenv("GLAB_CLI_EXTRAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GLAB_CLI_EXTRAS_BASE_DIR", str(tmp_path / "base"))
    monkeypatch.setenv("GLAB_CLI_EXTRAS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    for name in ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    # No test may read the machine's config files or settings variables.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setattr(config, "system_config_file", lambda: tmp_path / "etc" / config.APP / config.CONFIG_FILE)
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    for setting in config.SETTINGS:
        monkeypatch.delenv(setting.env_var, raising=False)
    yield


from discussion_samples import (  # noqa: E402, F401  the fixtures of the tests ported from glab-discussion
    diff_discussion,
    general_discussion,
    sample_diff_note,
    sample_note,
    sample_position,
    sample_system_note,
    system_discussion,
)
