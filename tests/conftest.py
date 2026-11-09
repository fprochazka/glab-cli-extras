from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakeglab import FakeGlab  # noqa: E402
from glab_cli_extras.gitlab.client import GitLabClient  # noqa: E402
from glab_cli_extras.resources.base import PollContext  # noqa: E402
from shapes import HOST  # noqa: E402


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
def fake() -> FakeGlab:
    return FakeGlab()


@pytest.fixture
def ctx(fake: FakeGlab, clock: Clock, tmp_path: Path) -> PollContext:
    return PollContext(GitLabClient(HOST, runner=fake, clock=clock), clock)


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may touch the real daemon, base dir or Claude session."""
    monkeypatch.setenv("GLAB_CLI_EXTRAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GLAB_CLI_EXTRAS_BASE_DIR", str(tmp_path / "base"))
    for name in ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    yield
