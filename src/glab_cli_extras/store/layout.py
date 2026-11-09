"""Where each resource's files go under a base dir.

The resource dirs mirror GitLab's URLs: `<base>/<host>/<group>/<project>/-/pipelines/<id>/` and
`.../-/merge_requests/<iid>/`. A URL maps to its dir by eye, and the `-` segment cannot collide with a group or project
path, because GitLab paths cannot be `-`. Every file about one resource lives in its dir: the canonical `state.json`,
the watcher's changelog, the job logs or thread files, and a `README.txt` that names them.
"""

from __future__ import annotations

import re
from pathlib import Path

from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.paths import project_dir, sanitize_part
from glab_cli_extras.store.atomic import atomic_write_text

STATE_FILE = "state.json"
README_FILE = "README.txt"
LOCK_FILE = ".lock"
JOB_LOGS_DIR = "job-logs"
THREADS_DIR = "threads"


def pipeline_dir(base: Path, host: str, project: str, pipeline_id: int) -> Path:
    return project_dir(base, host, project) / "-" / "pipelines" / str(pipeline_id)


def mr_dir(base: Path, host: str, project: str, iid: int) -> Path:
    return project_dir(base, host, project) / "-" / "merge_requests" / str(iid)


def resource_dir(base: Path, ref: ResourceRef) -> Path:
    if ref.kind == "pipeline":
        assert ref.id is not None
        return pipeline_dir(base, ref.host, ref.project, ref.id)
    if ref.kind == "mr":
        assert ref.id is not None
        return mr_dir(base, ref.host, ref.project, ref.id)
    if ref.kind == "branch":
        return project_dir(base, ref.host, ref.project) / "-" / "branches" / sanitize_part(ref.branch or "")
    return project_dir(base, ref.host, ref.project) / "-" / "merge_requests"


def filename_part(value: str) -> str:
    """glab-pipeline's sanitizer for job log names: every character but a letter or a digit becomes `-`."""
    return re.sub(r"[^a-zA-Z0-9]", "-", value)


def job_log_name(stage: str | None, name: str, job_id: int) -> str:
    """`<stage>-<name>-<job id>.log`, the name glab-pipeline has always used."""
    return f"{filename_part(stage or '')}-{filename_part(name)}-{job_id}.log"


_README = {
    "pipeline": """What glab-cli-extras knows about one GitLab pipeline. A file is replaced as a whole when it changes.

state.json     the pipeline's latest observed state: status, jobs, downstream pipelines, which job logs are here
job-logs/      the logs fetched so far, one file per job: <stage>-<name>-<job id>.log
events.jsonl   every event glab-watch reported for this pipeline, one JSON object per line
baseline.json  the last state the watcher saw, used after a daemon restart
""",
    "mr": """What glab-cli-extras knows about one GitLab merge request. A file is replaced as a whole when it changes.

state.json     the MR's latest state: title, draft, merge status, approvals, reviewers, head pipeline, threads
threads/       one file per discussion thread; the "Discussion:" line holds the full discussion id
               .meta.json maps each discussion id to its file name
events.jsonl   every event glab-watch reported for this MR, one JSON object per line
baseline.json  the last state the watcher saw, used after a daemon restart

The head pipeline's files are in ../../pipelines/<pipeline id>/.
""",
    "mrs": """events.jsonl   every event glab-watch reported for this project's merge requests, one JSON object per line
baseline.json  the last state the watcher saw, used after a daemon restart

Each merge request has its own dir next to these files: <iid>/.
""",
    "branch": """events.jsonl   every event glab-watch reported for this branch, one JSON object per line
baseline.json  the last state the watcher saw, used after a daemon restart

The branch's pipelines have their own dirs in ../../pipelines/<pipeline id>/.
""",
}


def ensure_readme(directory: Path, kind: str) -> None:
    """Write the dir's README.txt once, so an agent that lands in the dir learns what is in it."""
    path = directory / README_FILE
    text = _README.get(kind)
    if text is not None and not path.exists():
        atomic_write_text(path, text)
