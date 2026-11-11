"""Filesystem locations: the base dir every command writes its files under, and the daemon state dir.

Each location can be overridden by an environment variable, which the tests use to stay inside tmp dirs.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_blackbox import ActiveSession

BASE_DIR_ENV = "GLAB_CLI_EXTRAS_BASE_DIR"
STATE_DIR_ENV = "GLAB_CLI_EXTRAS_STATE_DIR"
CACHE_DIR_ENV = "GLAB_CLI_EXTRAS_CACHE_DIR"
APP = "glab-cli-extras"


def sanitize_part(value: str) -> str:
    """Make one path component safe: no separators, no traversal, no characters Windows rejects."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip(".")
    cleaned = cleaned.replace("..", "-")
    return cleaned or "unknown"


def project_dir(root: Path, host: str, project: str) -> Path:
    """`<root>/<host>/<group>/<sub>/<project>`, each component sanitized."""
    path = root / sanitize_part(host)
    for part in project.split("/"):
        path = path / sanitize_part(part)
    return path


def _uid_suffix() -> str:
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if getuid is not None else (os.environ.get("USERNAME") or "user")


def find_session() -> ActiveSession | None:
    """The Claude Code session this process runs under, or None."""
    from cc_blackbox import ClaudeBlackboxError, current_session

    try:
        return current_session()
    except (ClaudeBlackboxError, OSError):
        return None


def base_dir(override: Path | None = None, session: ActiveSession | None = None, *, detect: bool = True) -> Path:
    """The dir every command writes its resource dirs under.

    `override` wins, then `GLAB_CLI_EXTRAS_BASE_DIR`. Under a Claude Code session it is `<scratchpad>/glab-cli-extras`,
    which Claude Code removes after a few months. Otherwise `<temp dir>/glab-cli-extras-<uid>`, private to the user,
    which the OS cleans. Nothing is kept forever. With `detect`, the session is looked up when none is given.
    """
    if override is not None:
        return override
    env = os.environ.get(BASE_DIR_ENV)
    if env:
        return Path(env)
    if session is None and detect:
        session = find_session()
    if session is not None and session.scratchpad_dir is not None:
        return session.scratchpad_dir / APP
    return ensure_private_dir(Path(tempfile.gettempdir()) / f"{APP}-{_uid_suffix()}")


def daemon_state_dir() -> Path:
    """The per-user dir holding the daemon's lock, socket (or port file) and log."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP / "run"
    if sys.platform == "darwin":
        tmpdir = os.environ.get("TMPDIR")
        if tmpdir:
            return Path(tmpdir) / APP
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and Path(runtime).is_dir():
            return Path(runtime) / APP
    return Path(tempfile.gettempdir()) / f"{APP}-{_uid_suffix()}"


def cache_dir() -> Path:
    """The per-user dir of what the commands remember about each GitLab host between runs.

    `GLAB_CLI_EXTRAS_CACHE_DIR` wins. Else `$XDG_CACHE_HOME/glab-cli-extras` (default `~/.cache`, macOS included, as
    for the config file), on Windows `%LOCALAPPDATA%\\glab-cli-extras\\cache`.
    """
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    # The XDG spec says to ignore a relative path.
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".cache") / APP


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path
