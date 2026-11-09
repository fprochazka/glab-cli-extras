"""Job logs, fetched once after a job ends.

On GitLab 19.0 and later the trace endpoint takes `byte_offset` and `byte_limit` (at most 500 KB), so a large log is
read as its last 500 KB only. The size comes from the job's trace artifact. Older versions get the full trace.
A log already in the shared cache is reused, so two sessions watching the same pipeline download it once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.paths import project_dir, sanitize_part
from glab_cli_extras.store.atomic import atomic_write_bytes
from glab_cli_extras.sync.job_logs import TAIL_BYTES, fetch_log_content

LogsMode = Literal["failed", "all", "none"]
LOGS_MODES: tuple[LogsMode, ...] = ("failed", "all", "none")


def merge_logs_modes(modes: list[LogsMode]) -> LogsMode:
    if "all" in modes:
        return "all"
    if "failed" in modes:
        return "failed"
    return "none"


def wants_log(mode: LogsMode, status: str) -> bool:
    if mode == "all":
        return status in ("success", "failed", "canceled")
    if mode == "failed":
        return status == "failed"
    return False


@dataclass(frozen=True, slots=True)
class FetchedLog:
    path: Path
    truncated: bool
    size: int | None
    reused: bool


def log_path(cache_root: Path, host: str, project: str, job_id: int, job_name: str) -> Path:
    return project_dir(cache_root, host, project) / "jobs" / f"{job_id}-{sanitize_part(job_name)[:80]}.log"


async def fetch_job_log(
    client: GitLabClient,
    cache_root: Path,
    project: str,
    job_id: int,
    job_name: str,
    known_size: int | None,
) -> FetchedLog:
    """Download the job's log (or its tail) into the shared cache, unless it is already there."""
    path = log_path(cache_root, client.host, project, job_id, job_name)
    if path.is_file():
        return FetchedLog(path, path.read_bytes().startswith(b"[glab-watch: last "), known_size, True)
    content = await fetch_log_content(client, project, job_id, known_size)
    header = f"[glab-watch: last {TAIL_BYTES} of {content.size} bytes]\n".encode() if content.truncated else b""
    atomic_write_bytes(path, header + content.body)
    return FetchedLog(path, content.truncated, content.size, False)
