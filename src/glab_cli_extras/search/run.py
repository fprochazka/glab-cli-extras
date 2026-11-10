"""One glab-search-code run: search, look up the projects, download each matching file once, write the index files.

GitLab returns one hit per match, so a file that matches on several lines comes back several times. The hits are
grouped by project, path and ref before the download, so each file is downloaded once and its entry lists every start
line. Every request is a read and goes through the client's retry policy. A file that fails is logged and counted, and
the run goes on; only a failed search fails the run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

from glab_cli_extras.gitlab.client import ApiError, GitLabClient, encode_project
from glab_cli_extras.search.files import output_path
from glab_cli_extras.store.atomic import atomic_write_bytes, atomic_write_json
from glab_cli_extras.store.layout import ensure_readme

PER_PAGE = 100
MAX_PAGES = 100
MAX_HITS = PER_PAGE * MAX_PAGES
PROJECTS_PER_QUERY = 100
METADATA_FILE = "metadata.json"
HITS_FILE = "hits.json"
LOG_FILE = "download.log"

PROJECTS_QUERY = """
query($ids: [ID!], $first: Int) {
  projects(ids: $ids, first: $first) { nodes { id fullPath webUrl } }
}
"""

ADVANCED_SEARCH_HINT = (
    "a code search across a group or the whole instance needs Advanced search or Exact code search on this GitLab;"
    " search one project with --project"
)


class SearchError(Exception):
    """The search itself failed, so there is nothing to download."""


@dataclass(frozen=True, slots=True)
class SearchOptions:
    host: str
    query: str
    directory: Path
    project: str | None = None
    group: str | None = None
    workers: int = 10


@dataclass(frozen=True, slots=True)
class Project:
    path: str
    web_url: str


@dataclass(slots=True)
class FileHits:
    project_id: int
    path: str
    ref: str
    hits: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SearchSummary:
    directory: Path
    hits: int = 0
    files: int = 0
    projects: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    truncated: bool = False

    @property
    def metadata_file(self) -> Path:
        return self.directory / METADATA_FILE

    @property
    def hits_file(self) -> Path:
        return self.directory / HITS_FILE

    @property
    def log_file(self) -> Path:
        return self.directory / LOG_FILE


def search_endpoint(project: str | None, group: str | None) -> str:
    if project:
        return f"projects/{encode_project(project)}/search"
    if group:
        return f"groups/{encode_project(group)}/search"
    return "search"


class _Log:
    """download.log, appended from the event loop only, so lines never interleave."""

    def __init__(self, path: Path) -> None:
        self._file = path.open("a", encoding="utf-8")

    def write(self, message: str) -> None:
        stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        self._file.write(f"[{stamp}] {message}\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def group_hits(hits: Sequence[Any], log: _Log) -> tuple[list[FileHits], int]:
    """The hits grouped by file, in the order GitLab returned them, and the number of hits that name no file."""
    files: dict[tuple[int, str, str], FileHits] = {}
    unusable = 0
    for hit in hits:
        project_id = hit.get("project_id") if isinstance(hit, dict) else None
        # `filename` is the deprecated name of `path`; older GitLab versions return only it.
        path = (hit.get("path") or hit.get("filename")) if isinstance(hit, dict) else None
        if not isinstance(project_id, int) or not isinstance(path, str) or not path:
            unusable += 1
            log.write(f"SKIP: a hit without a project id or a path: {str(hit)[:200]}")
            continue
        ref = str(hit.get("ref") or "")
        key = (project_id, path, ref)
        if key not in files:
            files[key] = FileHits(project_id, path, ref)
        files[key].hits.append(hit)
    return list(files.values()), unusable


async def lookup_projects(client: GitLabClient, ids: Sequence[int], log: _Log) -> dict[int, Project]:
    """Each project's path and URL, 100 projects per GraphQL query. A project the query does not return, or every
    project when the query fails, is named `project-<id>` instead; its files are still downloaded."""
    found: dict[int, Project] = {}
    for start in range(0, len(ids), PROJECTS_PER_QUERY):
        chunk = ids[start : start + PROJECTS_PER_QUERY]
        variables = {"ids": [f"gid://gitlab/Project/{i}" for i in chunk], "first": len(chunk)}
        try:
            result = await client.graphql(PROJECTS_QUERY, variables, kind="projects")
        except ApiError as exc:
            log.write(f"FAIL: looking up projects {', '.join(map(str, chunk))}: {exc}")
            continue
        nodes = ((result.data or {}).get("projects") or {}).get("nodes") or []
        for node in nodes:
            gid = str(node.get("id", ""))
            suffix = gid.rsplit("/", 1)[-1]
            if suffix.isdigit() and node.get("fullPath"):
                found[int(suffix)] = Project(str(node["fullPath"]), str(node.get("webUrl") or ""))
    for project_id in ids:
        if project_id not in found:
            log.write(f"FAIL: no path for project {project_id}; its files go under project-{project_id}")
    return found


def blob_url(host: str, project: Project, ref: str, path: str) -> str:
    web_url = project.web_url or f"https://{host}/{project.path}"
    return f"{web_url}/-/blob/{quote(ref)}/{quote(path)}"


class _Progress:
    """`Progress:` lines on stderr: the first file, then about every 10 %, then the last."""

    def __init__(self, total: int, summary: SearchSummary, stream: TextIO) -> None:
        self.total = total
        self.summary = summary
        self.stream = stream
        self.done = 0
        self.next_step = 0

    def tick(self) -> None:
        self.done += 1
        percent = 100 * self.done / self.total
        if self.done == 1 or self.done == self.total or percent >= self.next_step:
            self.next_step = (int(percent) // 10 + 1) * 10
            s = self.summary
            print(
                f"Progress: {self.done}/{self.total} ({percent:.0f}%) | OK: {s.downloaded}, Skipped: {s.skipped},"
                f" Failed: {s.failed}",
                file=self.stream,
                flush=True,
            )


async def run_search(options: SearchOptions, client: GitLabClient, stderr: TextIO | None = None) -> SearchSummary:
    progress_stream = stderr if stderr is not None else sys.stderr
    directory = options.directory
    directory.mkdir(parents=True, exist_ok=True)
    ensure_readme(directory, "search")
    summary = SearchSummary(directory)
    log = _Log(summary.log_file)
    try:
        log.write(f"Search query: {options.query}")
        print("Searching...", file=progress_stream, flush=True)
        endpoint = search_endpoint(options.project, options.group)
        params = {"scope": "blobs", "search": options.query}
        try:
            hits = await client.get_pages(endpoint, params, kind="search", max_pages=MAX_PAGES)
        except ApiError as exc:
            log.write(f"FAIL: search: {exc}")
            if exc.status == 400 and not options.project:
                raise SearchError(f"{exc} ({ADVANCED_SEARCH_HINT})") from exc
            raise SearchError(str(exc)) from exc
        summary.hits = len(hits)
        summary.truncated = len(hits) >= MAX_HITS
        files, unusable = group_hits(hits, log)
        summary.skipped += unusable
        summary.files = len(files)
        project_ids = list(dict.fromkeys(f.project_id for f in files))
        summary.projects = len(project_ids)
        log.write(f"Found {summary.hits} hits in {summary.files} files of {summary.projects} projects")

        projects: dict[int, Project] = {}
        if project_ids:
            print(f"Looking up {len(project_ids)} projects...", file=progress_stream, flush=True)
            projects = await lookup_projects(client, project_ids, log)

        def project_of(project_id: int) -> Project:
            return projects.get(project_id) or Project(f"project-{project_id}", "")

        owners: dict[str, FileHits] = {}
        metadata: dict[int, dict[str, Any]] = {}
        progress = _Progress(len(files), summary, progress_stream)

        async def download(index: int, item: FileHits) -> None:
            project = project_of(item.project_id)
            name = f"{project.path}/{item.path}" + (f" @ {item.ref}" if item.ref else "")
            relative = output_path(project.path, item.path).as_posix()
            owner = owners.setdefault(relative, item)
            if owner is not item:
                summary.skipped += 1
                log.write(f"SKIP: {name}: maps to {relative}, which holds {owner.path} @ {owner.ref}")
                return
            params = {"ref": item.ref} if item.ref else {}
            path = f"projects/{item.project_id}/repository/files/{quote(item.path, safe='')}/raw"
            try:
                response = await client.get(path, params, kind="file")
                atomic_write_bytes(directory / relative, response.body)
            except (ApiError, OSError) as exc:
                summary.failed += 1
                log.write(f"FAIL: {name}: {exc}")
                return
            summary.downloaded += 1
            log.write(f"OK: {name} -> {relative}")
            metadata[index] = {
                "project_id": item.project_id,
                "project_path": project.path,
                "file_path": item.path,
                "ref": item.ref,
                "output_file": relative,
                "url": blob_url(options.host, project, item.ref, item.path),
                "hits": [hit.get("startline") for hit in item.hits],
            }

        if files:
            print(
                f"Downloading {len(files)} files with {options.workers} parallel workers...",
                file=progress_stream,
                flush=True,
            )
            tasks = [asyncio.ensure_future(download(index, item)) for index, item in enumerate(files)]
            for finished in asyncio.as_completed(tasks):
                await finished
                progress.tick()

        atomic_write_json(summary.metadata_file, [metadata[index] for index in sorted(metadata)])
        atomic_write_json(
            summary.hits_file,
            [
                {
                    "project_path": project_of(item.project_id).path,
                    "path": item.path,
                    "ref": item.ref,
                    "startline": hit.get("startline"),
                    "data": hit.get("data"),
                }
                for item in files
                for hit in item.hits
            ],
        )
        log.write(f"Done: OK {summary.downloaded}, SKIP {summary.skipped}, FAIL {summary.failed}")
    finally:
        log.close()
    return summary
