"""Job traces.

On GitLab 19.0 and later the trace endpoint takes `byte_offset` and `byte_limit` (at most 500 KB), so a large log can be
read as its last 500 KB only. The size comes from the job's trace artifact. Older versions get the full trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from glab_cli_extras.gitlab.client import GitLabClient, encode_project

LogsMode = Literal["failed", "all", "none"]
LOGS_MODES: tuple[LogsMode, ...] = ("failed", "all", "none")
FINAL_JOB_STATUSES = frozenset({"success", "failed", "canceled", "skipped"})

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
class LogContent:
    body: bytes
    truncated: bool
    size: int | None


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


async def fetch_log_content(
    client: GitLabClient, project: str, job_id: int, known_size: int | None, *, tail: bool = True
) -> LogContent:
    """The job's log. With `tail`, only its last 500 KB when it is larger and GitLab can serve a tail."""
    encoded = encode_project(project)
    if not tail:
        response = await client.get(f"projects/{encoded}/jobs/{job_id}/trace", kind="trace")
        return LogContent(response.body, False, len(response.body))
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
    return LogContent(response.body, truncated, size)
