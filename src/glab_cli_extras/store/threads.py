"""One text file per MR discussion thread, in glab-discussion's format, and the `.meta.json` index next to them.

A file is rewritten only when its rendered content changed, so its mtime says when the thread last changed. The index
maps each key (a discussion id, or `draft-<id>` for a draft that starts a thread) to its file name, its content hash,
`sig`, the version of the published notes it was rendered from, and `drafts_sig`, the version of the user's draft
replies in it. A watch compares `sig` only and renders no drafts; `glab-discussion read` compares both.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from glab_cli_extras.discussion.formatter import format_discussion
from glab_cli_extras.discussion.models import Discussion, DraftNote, UserInfo, parse_discussion
from glab_cli_extras.store.atomic import atomic_write_text

META_FILE = ".meta.json"


def is_system_discussion(discussion: Mapping[str, Any]) -> bool:
    notes = discussion.get("notes") or []
    return bool(notes) and all(n.get("system") for n in notes)


def filename_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", value)


def display_name(user: UserInfo) -> str:
    """The name for file names: the display name for service accounts, whose usernames are random, else the username."""
    stripped = user.username.replace("-", "").replace("_", "")
    if "serviceaccount" in stripped:
        return user.name
    return user.username


def thread_filename(discussion: Discussion, users: Mapping[str, UserInfo]) -> str:
    """`<created, to the minute>-<author, bot- prefix for bots>-<id prefix>.txt`, so a listing sorts by start time."""
    first = discussion.first_note
    created = filename_part(first.created_at[:16])
    user = users.get(first.author_username)
    name = filename_part(display_name(user) if user else first.author_username)
    if user and user.is_bot:
        name = f"bot-{name}"
    return f"{created}-{name}-{filename_part(discussion.id[:12])}.txt"


def parse_with_users(raw: Mapping[str, Any], users: Mapping[str, UserInfo]) -> Discussion:
    discussion = parse_discussion(dict(raw))
    for note in discussion.notes:
        user = users.get(note.author_username)
        note.is_bot = bool(user and user.is_bot)
    return discussion


def render_thread(
    raw: Mapping[str, Any], mr_url: str, users: Mapping[str, UserInfo], reply_drafts: Sequence[DraftNote] = ()
) -> tuple[str, str]:
    """The file name and the content of a REST discussion's thread file."""
    discussion = parse_with_users(raw, users)
    return thread_filename(discussion, users), format_discussion(discussion, mr_url, reply_drafts)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


WriteStatus = Literal["new", "updated", "unchanged"]


@dataclass(frozen=True, slots=True)
class WriteResult:
    path: Path
    status: WriteStatus
    replaced: str | None
    """The old file name, when the thread was renamed (its first author's display name changed, for example)."""


class ThreadStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._meta_path = directory / META_FILE
        self.meta: dict[str, dict[str, str]] = self._load_meta()

    def _load_meta(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def save(self) -> None:
        atomic_write_text(self._meta_path, json.dumps(self.meta, indent=2, sort_keys=True) + "\n")

    def safe_file(self, filename: str) -> Path | None:
        """The path of a file named in `.meta.json`, or None when the name could point outside the dir."""
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            return None
        path = self.directory / filename
        if path.resolve().parent != self.directory.resolve():
            return None
        return path

    def path_for(self, key: str) -> Path | None:
        entry = self.meta.get(key)
        path = self.safe_file(entry.get("filename", "")) if entry else None
        return path if path is not None and path.is_file() else None

    def sig(self, key: str) -> str | None:
        entry = self.meta.get(key)
        return entry.get("sig") if entry else None

    def drafts_sig(self, key: str) -> str | None:
        entry = self.meta.get(key)
        return entry.get("drafts_sig") if entry else None

    def discussion_ids(self) -> set[str]:
        """The discussions that have a file. Draft keys (`draft-<id>`) are not discussions and are never listed."""
        return {key for key in self.meta if not key.startswith("draft-")}

    def write(
        self, key: str, filename: str, content: str, *, sig: str | None = None, drafts_sig: str | None = None
    ) -> WriteResult:
        """Write the thread's file if its content or name changed, and record it. Call `save()` afterwards."""
        self.directory.mkdir(parents=True, exist_ok=True)
        digest = content_hash(content)
        old = self.meta.get(key)
        entry = {"filename": filename, "hash": digest}
        if sig is not None:
            entry["sig"] = sig
        elif old and "sig" in old:
            entry["sig"] = old["sig"]
        if drafts_sig is not None:
            entry["drafts_sig"] = drafts_sig
        elif old and "drafts_sig" in old:
            entry["drafts_sig"] = old["drafts_sig"]
        path = self.directory / filename
        if old and old.get("hash") == digest and old.get("filename") == filename and path.is_file():
            self.meta[key] = entry
            return WriteResult(path, "unchanged", None)
        atomic_write_text(path, content)
        replaced = None
        if old and old.get("filename") and old["filename"] != filename:
            replaced = old["filename"]
            stale = self.safe_file(replaced)
            if stale is not None and replaced not in {e.get("filename") for k, e in self.meta.items() if k != key}:
                with contextlib.suppress(OSError):
                    stale.unlink()
        self.meta[key] = entry
        return WriteResult(path, "new" if old is None else "updated", replaced)

    def remove(self, key: str) -> str | None:
        """Forget a thread and delete its file. Return the file name. Call `save()` afterwards."""
        entry = self.meta.pop(key, None)
        if entry is None:
            return None
        filename = entry.get("filename", "")
        path = self.safe_file(filename)
        if path is not None and filename not in {e.get("filename") for e in self.meta.values()}:
            with contextlib.suppress(OSError):
                path.unlink()
        return filename
