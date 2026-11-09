"""Job log files in a pipeline dir, and their index in the pipeline's `state.json`.

Every job of the pipeline has a file at its usual path. The index (`logs` in `state.json`) records what each file
holds: `log_state` is `downloaded` for the full log, `tail` for its last 500 KB, or `placeholder` for a short note that
says why the log is not there and gives the command that downloads it. `complete` says the job had ended when its log
was fetched, so the file will not change any more and is never fetched again. A placeholder counts as "not fetched":
`--logs=all`, or the job failing later, replaces it with the real log.
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
LOG_PLACEHOLDER = "placeholder"
PLACEHOLDER_MARKER = "[glab-cli-extras: log not downloaded]"
"""The first line of every placeholder file, so code and people can tell it from a real log."""

QUEUED_JOB_STATUSES = frozenset({"created", "pending", "waiting_for_resource", "preparing", "scheduled"})


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


def placeholder_reason(status: str, logs: str, *, coming: bool = False) -> str:
    """Why a job's log is not in its file. `coming`: the log is wanted and on its way."""
    if coming:
        return "the job just ended and its log is being downloaded; this file is replaced when it arrives"
    if status == "running":
        return "the job is still running; its log is complete once it ends"
    if status in QUEUED_JOB_STATUSES:
        return f"the job has not started yet (status {status}), so it has no log"
    if status == "skipped":
        return "the job was skipped, so it has no log"
    if status == "manual":
        return "the job is a manual job that nobody started, so it has no log"
    if logs == "none":
        return "the log mode is none, so no log was downloaded"
    if status == "success":
        return "the job passed, and only the logs of failed jobs are downloaded (--logs=failed)"
    return f"the job ended with status {status}, and only the logs of failed jobs are downloaded (--logs=failed)"


def write_placeholder(
    directory: Path,
    state: dict[str, Any],
    *,
    job_id: int,
    stage: str | None,
    name: str,
    status: str,
    logs: str,
    command: str,
    coming: bool = False,
) -> Path | None:
    """Put a placeholder where the job's log would be, unless a real log is there. Return its path when written.

    The caller holds the dir's lock and saves the state. An unchanged placeholder is not rewritten.
    """
    entry = log_entry(state, job_id)
    if entry is not None and entry.get("log_state") in (LOG_DOWNLOADED, LOG_TAIL):
        return None
    reason = placeholder_reason(status, logs, coming=coming)
    unchanged = entry is not None and entry.get("job_status") == status and entry.get("reason") == reason
    if unchanged and entry is not None and (directory / str(entry.get("file", ""))).is_file():
        return None
    path = log_file(directory, stage, name, job_id)
    text = (
        f"{PLACEHOLDER_MARKER}\n"
        f"Job: {name} (id {job_id}), stage {stage or '-'}, status {status}\n"
        f"Why: {reason}\n"
        f"To download the logs of every job of this pipeline, run:\n"
        f"  {command}\n"
    )
    atomic_write_bytes(path, text.encode("utf-8"))
    state.setdefault("logs", {})[str(job_id)] = {
        "file": f"{JOB_LOGS_DIR}/{path.name}",
        "name": name,
        "stage": stage,
        "job_status": status,
        "log_state": LOG_PLACEHOLDER,
        "complete": False,
        "reason": reason,
    }
    return path
