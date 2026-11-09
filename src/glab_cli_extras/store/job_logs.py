"""Job log files in a pipeline dir, and their index in the pipeline's `state.json`.

The index (`logs` in `state.json`) records for each job what its file holds: `log_state` is `downloaded` for the full
log or `tail` for its last 500 KB. `complete` says the job had ended when its log was fetched, so the file will not
change any more and is never fetched again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from glab_cli_extras.events import utc_now_iso
from glab_cli_extras.store.atomic import atomic_write_bytes
from glab_cli_extras.store.layout import JOB_LOGS_DIR, job_log_name
from glab_cli_extras.sync.job_logs import FINAL_JOB_STATUSES, TAIL_BYTES, LogContent

LOG_DOWNLOADED = "downloaded"
LOG_TAIL = "tail"


def log_file(directory: Path, stage: str | None, name: str, job_id: int) -> Path:
    return directory / JOB_LOGS_DIR / job_log_name(stage, name, job_id)


def log_entry(state: dict[str, Any], job_id: int) -> dict[str, Any] | None:
    entry = (state.get("logs") or {}).get(str(job_id))
    return entry if isinstance(entry, dict) else None


def has_log(directory: Path, state: dict[str, Any], job_id: int, *, full: bool) -> Path | None:
    """The file of a complete log already in the dir, when it satisfies the request: a full log always does, a tail
    only when `full` is False."""
    entry = log_entry(state, job_id)
    if entry is None or not entry.get("complete"):
        return None
    if entry.get("log_state") not in (LOG_DOWNLOADED, LOG_TAIL):
        return None
    if full and entry.get("log_state") != LOG_DOWNLOADED:
        return None
    path = directory / str(entry.get("file", ""))
    return path if path.is_file() and path.parent == directory / JOB_LOGS_DIR else None


def write_log(
    directory: Path,
    state: dict[str, Any],
    *,
    job_id: int,
    stage: str | None,
    name: str,
    status: str,
    content: LogContent,
) -> Path:
    """Write a fetched log and record it in `state["logs"]`. The caller holds the dir's lock and saves the state."""
    path = log_file(directory, stage, name, job_id)
    header = f"[glab-cli-extras: last {TAIL_BYTES} of {content.size} bytes]\n".encode() if content.truncated else b""
    atomic_write_bytes(path, header + content.body)
    state.setdefault("logs", {})[str(job_id)] = {
        "file": f"{JOB_LOGS_DIR}/{path.name}",
        "name": name,
        "stage": stage,
        "job_status": status,
        "log_state": LOG_TAIL if content.truncated else LOG_DOWNLOADED,
        "complete": status in FINAL_JOB_STATUSES,
        "truncated": content.truncated,
        "bytes": len(content.body),
        "fetched_at": utc_now_iso(),
    }
    return path
