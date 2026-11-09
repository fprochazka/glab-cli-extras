"""Filesystem locations: the shared artifact cache, the daemon state dir, and the default changelog dir.

Each location can be overridden by an environment variable, which the tests use to stay inside tmp dirs.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

CACHE_DIR_ENV = "GLAB_CLI_EXTRAS_CACHE_DIR"
STATE_DIR_ENV = "GLAB_CLI_EXTRAS_STATE_DIR"
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


def user_cache_dir() -> Path:
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / APP


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


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def default_changelog_root() -> Path:
    return Path(tempfile.gettempdir()) / f"glab-watch-{_uid_suffix()}"
