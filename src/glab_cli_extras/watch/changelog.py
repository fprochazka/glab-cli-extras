"""The client's per-resource changelog: `events.jsonl` (append-only) and `state.json` (the last snapshot).

The stored snapshot lets a watcher resubscribe after a daemon restart without a fake "something changed": the new
daemon diffs against it, and the client drops the new `watch_started` when the snapshot hash did not move.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glab_cli_extras.events import Event, utc_now_iso
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.paths import project_dir, sanitize_part


def resource_dir_name(ref: ResourceRef) -> str:
    if ref.kind in ("mr", "pipeline"):
        return f"{ref.kind}-{ref.id}"
    if ref.kind == "branch":
        return f"branch-{sanitize_part(ref.branch or '')}"
    return ref.kind


@dataclass(frozen=True)
class StoredState:
    snapshot_hash: str | None
    snapshot: Any


class Changelog:
    def __init__(self, root: Path, ref: ResourceRef) -> None:
        self.ref = ref
        self.dir = project_dir(root, ref.host, ref.project) / resource_dir_name(ref)
        self.events_path = self.dir / "events.jsonl"
        self.state_path = self.dir / "state.json"

    def append(self, events: Iterable[Event]) -> None:
        lines = [json.dumps(e.to_json(), ensure_ascii=False, separators=(",", ":")) + "\n" for e in events]
        if not lines:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.writelines(lines)

    def last_seq(self) -> int:
        """The `seq` of the last event in events.jsonl, 0 when there is none."""
        try:
            with open(self.events_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 256 * 1024))
                tail = fh.read()
        except OSError:
            return 0
        for line in reversed(tail.splitlines()):
            try:
                return int(json.loads(line)["seq"])
            except (ValueError, KeyError, TypeError):
                continue
        return 0

    def load_state(self) -> StoredState | None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("resource") != self.ref.key:
            return None
        return StoredState(raw.get("snapshot_hash"), raw.get("snapshot"))

    def save_state(self, snapshot_hash: str, snapshot: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "resource": self.ref.key,
            "snapshot_hash": snapshot_hash,
            "snapshot": snapshot,
            "written_at": utc_now_iso(),
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)
