"""One markdown file per MR discussion thread, in the format glab-discussion uses.

A file is rewritten only when its rendered content changed, so its mtime says when the thread last changed. The
`.meta.json` next to the files maps each discussion id to its file name and content hash.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from glab_cli_extras.paths import sanitize_part
from glab_cli_extras.store.atomic import atomic_write_text

META_FILE = ".meta.json"


def _endpoint(old_line: Any, new_line: Any) -> str:
    parts = []
    if new_line is not None:
        parts.append(f"new:{new_line}")
    if old_line is not None:
        parts.append(f"old:{old_line}")
    return " / ".join(parts)


def _position_lines(position: Mapping[str, Any]) -> list[str]:
    lines = [f"File: {position.get('new_path') or position.get('old_path')}"]
    line_range = position.get("line_range")
    if isinstance(line_range, dict) and line_range.get("start") and line_range.get("end"):
        start, end = line_range["start"], line_range["end"]
        lines.append(
            f"Lines: {_endpoint(start.get('old_line'), start.get('new_line'))}"
            f" to {_endpoint(end.get('old_line'), end.get('new_line'))}"
        )
    else:
        line = _endpoint(position.get("old_line"), position.get("new_line"))
        if line:
            lines.append(f"Line: {line}")
    if position.get("head_sha"):
        lines.append(f"Commit: {position['head_sha']}")
    return lines


def is_system_discussion(discussion: Mapping[str, Any]) -> bool:
    notes = discussion.get("notes") or []
    return bool(notes) and all(n.get("system") for n in notes)


def render_discussion(discussion: Mapping[str, Any], mr_url: str) -> str:
    """Render a REST discussion object as text: a header, then one block per note."""
    notes: list[Mapping[str, Any]] = list(discussion.get("notes") or [])
    first = notes[0] if notes else {}
    lines = [f"Discussion: {discussion.get('id')}"]
    position = first.get("position")
    if first.get("type") == "DiffNote" and isinstance(position, dict):
        lines.append("Type: DiffNote")
        lines.extend(_position_lines(position))
    else:
        lines.append("Type: General")
    resolvable = [n for n in notes if n.get("resolvable")]
    if resolvable:
        lines.append(f"Resolved: {'yes' if all(n.get('resolved') for n in resolvable) else 'no'}")
    if first.get("id") is not None:
        lines.append(f"URL: {mr_url}#note_{first['id']}")
    lines.append("---")
    for note in notes:
        author = (note.get("author") or {}).get("username", "unknown")
        timestamp = str(note.get("created_at", "")).replace("T", " ").split(".")[0].removesuffix("Z")
        lines.append(f"[{timestamp}] @{author} (note:{note.get('id')}):")
        lines.append(str(note.get("body", "")))
        lines.append("")
    return "\n".join(lines)


def thread_filename(discussion: Mapping[str, Any]) -> str:
    """`<created, to the minute>-<author>-<id prefix>.md`, so a directory listing sorts threads by start time."""
    notes = discussion.get("notes") or [{}]
    first = notes[0]
    created = sanitize_part(str(first.get("created_at", ""))[:16].replace(":", "-")) or "unknown"
    author = sanitize_part(str((first.get("author") or {}).get("username", "unknown")))
    short_id = sanitize_part(str(discussion.get("id", ""))[:12])
    return f"{created}-{author}-{short_id}.md"


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ThreadStore:
    def __init__(self, directory: Path, mr_url: str) -> None:
        self.directory = directory
        self.mr_url = mr_url
        self._meta_path = directory / META_FILE
        self._meta: dict[str, dict[str, str]] = self._load_meta()

    def _load_meta(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save_meta(self) -> None:
        atomic_write_text(self._meta_path, json.dumps(self._meta, indent=2, sort_keys=True) + "\n")

    def path_for(self, discussion_id: str) -> Path | None:
        entry = self._meta.get(discussion_id)
        if not entry:
            return None
        path = self.directory / entry["filename"]
        return path if path.is_file() else None

    def sig(self, discussion_id: str) -> str | None:
        """The signature of the note versions the thread's file was rendered from, None when unknown."""
        entry = self._meta.get(discussion_id)
        return entry.get("sig") if entry else None

    def discussion_ids(self) -> set[str]:
        """The discussions that have a file. Draft keys (`draft-<id>`) are not discussions and never listed."""
        return {key for key in self._meta if not key.startswith("draft-")}

    def write(self, discussion: Mapping[str, Any], sig: str | None = None) -> tuple[Path, bool]:
        """Write the thread's file if its content changed. Return the path and whether it was written."""
        discussion_id = str(discussion["id"])
        content = render_discussion(discussion, self.mr_url)
        filename = thread_filename(discussion)
        path = self.directory / filename
        digest = _hash(content)
        old = self._meta.get(discussion_id)
        if old and old.get("hash") == digest and old.get("filename") == filename and path.is_file():
            if sig is not None and old.get("sig") != sig:
                old["sig"] = sig
                self._save_meta()
            return path, False
        atomic_write_text(path, content)
        if old and old.get("filename") and old["filename"] != filename:
            with contextlib.suppress(OSError):
                (self.directory / old["filename"]).unlink()
        self._meta[discussion_id] = {"filename": filename, "hash": digest}
        if sig is not None:
            self._meta[discussion_id]["sig"] = sig
        self._save_meta()
        return path, True

    def remove(self, discussion_id: str) -> None:
        entry = self._meta.pop(discussion_id, None)
        if entry is None:
            return
        with contextlib.suppress(OSError):
            (self.directory / entry["filename"]).unlink()
        self._save_meta()
