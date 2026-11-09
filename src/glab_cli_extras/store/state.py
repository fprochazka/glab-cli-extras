"""A resource dir's `state.json` and its lock.

`state.json` is the latest state any writer observed, plus the index of the files in the dir. Its sections are owned
by different writers (a watch client, a standalone command of the same session), so each writer reads the file, updates
its own sections and writes it back while it holds the dir's lock. The snapshot never goes backwards: a write whose
`observed_at` is older than the stored one keeps the stored snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from glab_cli_extras.store.atomic import atomic_write_json
from glab_cli_extras.store.layout import LOCK_FILE, STATE_FILE
from glab_cli_extras.store.locks import FileLock

SCHEMA = 1
LOCK_TIMEOUT = 120.0


@contextmanager
def locked(directory: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Hold the dir's lock. Raises `LockTimeout` when another process holds it longer than `timeout`."""
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(directory / LOCK_FILE)
    lock.acquire(timeout)
    try:
        yield
    finally:
        lock.release()


def load_state(directory: Path, resource: str) -> dict[str, Any]:
    """The stored state, or a new one when the file is missing, unreadable, or about another resource."""
    try:
        raw = json.loads((directory / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict) or raw.get("resource") != resource:
        return {"schema": SCHEMA, "resource": resource}
    return raw


def save_state(directory: Path, state: dict[str, Any]) -> None:
    atomic_write_json(directory / STATE_FILE, state)


def put_snapshot(state: dict[str, Any], snapshot: Any, observed_at: str, **extra: Any) -> bool:
    """Store a snapshot unless the stored one was observed later. Return whether it was stored."""
    stored = state.get("observed_at")
    if isinstance(stored, str) and stored > observed_at and "snapshot" in state:
        return False
    state["observed_at"] = observed_at
    state["snapshot"] = snapshot
    state.update(extra)
    return True
