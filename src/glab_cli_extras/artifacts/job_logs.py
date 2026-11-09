"""Job logs, fetched once after a job ends.

On GitLab 19.0 and later the trace endpoint takes `byte_offset` and `byte_limit` (at most 500 KB), so a large log is
read as its last 500 KB only. The size comes from the job's trace artifact. Older versions get the full trace.
A log already in the shared cache is reused, so two sessions watching the same pipeline download it once.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from glab_cli_extras.gitlab.client import GitLabClient, encode_project
from glab_cli_extras.paths import project_dir, sanitize_part

LogsMode = Literal["failed", "all", "none"]
LOGS_MODES: tuple[LogsMode, ...] = ("failed", "all", "none")
TAIL_BYTES = 500 * 1024
"""GitLab's `Gitlab::Ci::Trace::Stream::LIMIT_SIZE`, the largest `byte_limit` it accepts."""
BYTE_OFFSET_MIN_VERSION = (19, 0)


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


def _trace_size(job: Any) -> int | None:
    if not isinstance(job, dict):
        return None
    for artifact in job.get("artifacts") or []:
        if (
            isinstance(artifact, dict)
            and artifact.get("file_type") == "trace"
            and isinstance(artifact.get("size"), int)
        ):
            return int(artifact["size"])
    return None


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
    encoded = encode_project(project)
    version = await client.version()
    tail_supported = version is not None and version >= BYTE_OFFSET_MIN_VERSION
    size = known_size
    if size is None and tail_supported:
        size = _trace_size(await client.get_json(f"projects/{encoded}/jobs/{job_id}", kind="job"))
    params: dict[str, str | int] = {}
    truncated = False
    if tail_supported and size is not None and size > TAIL_BYTES:
        params = {"byte_offset": size - TAIL_BYTES, "byte_limit": TAIL_BYTES}
        truncated = True
    response = await client.get(f"projects/{encoded}/jobs/{job_id}/trace", params, kind="trace")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    header = f"[glab-watch: last {TAIL_BYTES} of {size} bytes]\n".encode() if truncated else b""
    tmp.write_bytes(header + response.body)
    os.replace(tmp, path)
    return FetchedLog(path, truncated, size, False)
