"""Where a matching file goes in a search run's dir: `files/<project path>/<file path>`.

The tree keeps the repository paths, so `rg` over `files/` prints a path that is also the file's path in its project.
Each component is made safe on its own. A path from GitLab is data, not a trusted path: `..`, `.`, an empty component
and a leading `/` never leave the run's dir. Characters Windows rejects become `_` on every platform, so a run's dir
looks the same everywhere. A component above the 255 bytes most filesystems allow is shortened and gets a hash of its
full name, so two long names that share a prefix stay apart.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

FILES_DIR = "files"
MAX_NAME_BYTES = 255
HASH_LENGTH = 12
MAX_EXTENSION_BYTES = 16

_UNSAFE = re.compile(r'[\x00-\x1f<>:"\\|?*]')


def shorten(name: str) -> str:
    """`name` if it fits in 255 bytes; else its start, `__`, 12 hex digits of its SHA-1, and its extension."""
    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_NAME_BYTES:
        return name
    digest = hashlib.sha1(encoded).hexdigest()[:HASH_LENGTH]
    dot = name.rfind(".")
    extension = name[dot:] if dot > 0 else ""
    if len(extension.encode("utf-8")) > MAX_EXTENSION_BYTES:
        extension = ""
    budget = MAX_NAME_BYTES - len(extension.encode("utf-8")) - len(digest) - 2
    # Cutting the bytes may split a multi-byte character; drop its remains.
    start = encoded[:budget].decode("utf-8", errors="ignore")
    return f"{start}__{digest}{extension}"


def safe_component(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name)
    if cleaned in (".", ".."):
        cleaned = cleaned.replace(".", "_")
    return shorten(cleaned)


def output_path(project_path: str, file_path: str) -> PurePosixPath:
    """The file's path relative to the run's dir."""
    parts = [safe_component(part) for part in [*project_path.split("/"), *file_path.split("/")] if part]
    return PurePosixPath(FILES_DIR, *parts)
