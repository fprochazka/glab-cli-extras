"""Watched resources: the `ResourceRef` type and parsing of the command-line forms.

Repository names are resolved by glab itself (`glab repo view [repo] -F json`), so `group/project`,
`host/group/project` and an omitted repo (the cwd's git remotes) mean exactly what they mean to glab.
URLs carry their host and project, so they need no request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from glab_cli_extras.gitlab.glab import ProcessResult, run_glab_sync, stderr_message

ResourceKind = Literal["mr", "pipeline", "mrs", "branch"]
RESOURCE_KINDS: tuple[ResourceKind, ...] = ("mr", "pipeline", "mrs", "branch")

_URL_RE = re.compile(r"^https?://(?P<host>[^/]+)/(?P<project>.+?)/-/(?P<what>merge_requests|pipelines)/(?P<id>\d+)")
_ID_RE = re.compile(r"^(?:(?P<repo>.*)!)?(?P<id>\d+)$")


class RefError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceRef:
    kind: ResourceKind
    host: str
    project: str
    id: int | None = None
    """MR iid for `mr`, pipeline id for `pipeline`."""
    branch: str | None = None

    def __post_init__(self) -> None:
        if self.kind in ("mr", "pipeline") and self.id is None:
            raise RefError(f"a {self.kind} reference needs an id")
        if self.kind == "branch" and not self.branch:
            raise RefError("a branch reference needs a branch name")

    @property
    def key(self) -> str:
        """A stable identity, also used as the `resource` field of events: `mr gitlab.com/group/project!123`."""
        return f"{self.kind} {self.short}"

    @property
    def short(self) -> str:
        """The reference without the kind: `gitlab.com/group/project!123`."""
        base = f"{self.host}/{self.project}"
        if self.kind in ("mr", "pipeline"):
            return f"{base}!{self.id}"
        if self.kind == "branch":
            return f"{base}:{self.branch}"
        return base

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"kind": self.kind, "host": self.host, "project": self.project}
        if self.id is not None:
            wire["id"] = self.id
        if self.branch is not None:
            wire["branch"] = self.branch
        return wire

    @classmethod
    def from_wire(cls, data: Any) -> ResourceRef:
        if not isinstance(data, dict):
            raise RefError("resource must be an object")
        kind = data.get("kind")
        if kind not in RESOURCE_KINDS:
            raise RefError(f"unknown resource kind: {kind!r}")
        host, project = data.get("host"), data.get("project")
        if not isinstance(host, str) or not host or not isinstance(project, str) or not project:
            raise RefError("resource needs host and project")
        raw_id, branch = data.get("id"), data.get("branch")
        if raw_id is not None and not isinstance(raw_id, int):
            raise RefError("resource id must be an integer")
        if branch is not None and not isinstance(branch, str):
            raise RefError("resource branch must be a string")
        return cls(cast(ResourceKind, kind), host, project, raw_id, branch)


@dataclass(frozen=True, slots=True)
class Repo:
    host: str
    project: str


SyncRunner = Callable[[Sequence[str]], ProcessResult]


def _default_runner(args: Sequence[str]) -> ProcessResult:
    return run_glab_sync(args, timeout=30)


def resolve_repo(spec: str | None, runner: SyncRunner = _default_runner) -> Repo:
    """Resolve a repo name the way glab does, through `glab repo view [spec] -F json`. Costs one API request."""
    args = ["repo", "view"] + ([spec] if spec else []) + ["-F", "json"]
    result = runner(args)
    try:
        payload = json.loads(result.stdout or b"null")
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or "error" in payload or "web_url" not in payload:
        detail = ""
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            detail = str(payload["error"].get("message", ""))
        what = f"repository {spec!r}" if spec else "the repository of the current directory"
        raise RefError(f"glab could not resolve {what}: {detail or stderr_message(result)}")
    host = urlsplit(str(payload["web_url"])).netloc
    project = str(payload.get("path_with_namespace") or "")
    if not host or not project:
        raise RefError(f"glab returned no host or project path for {spec or 'the current directory'}")
    return Repo(host, project)


def parse_id_ref(kind: Literal["mr", "pipeline"], arg: str, runner: SyncRunner = _default_runner) -> ResourceRef:
    """Parse `<repo>!<id>`, `!<id>`, `<id>`, or a merge request or pipeline URL."""
    arg = arg.strip()
    url_match = _URL_RE.match(arg)
    if url_match:
        what = "merge_requests" if kind == "mr" else "pipelines"
        if url_match.group("what") != what:
            raise RefError(f"{arg} is not a {'merge request' if kind == 'mr' else 'pipeline'} URL")
        return ResourceRef(kind, url_match.group("host"), url_match.group("project"), int(url_match.group("id")))
    id_match = _ID_RE.match(arg)
    if not id_match:
        example = "group/project!123" if kind == "mr" else "group/project!4567"
        raise RefError(f"cannot read {arg!r}: expected {example}, !<id>, <id>, or a URL")
    repo = resolve_repo(id_match.group("repo") or None, runner)
    return ResourceRef(kind, repo.host, repo.project, int(id_match.group("id")))


def parse_repo_ref(
    kind: Literal["mrs", "branch"],
    repo_arg: str | None,
    branch: str | None = None,
    runner: SyncRunner = _default_runner,
) -> ResourceRef:
    """Parse the repo argument of `mrs` and `branch`. A project URL is accepted as well as a glab repo name."""
    if repo_arg and repo_arg.startswith(("http://", "https://")):
        parts = urlsplit(repo_arg)
        project = parts.path.strip("/").split("/-/")[0].removesuffix(".git")
        if not parts.netloc or not project:
            raise RefError(f"cannot read the project from {repo_arg}")
        return ResourceRef(kind, parts.netloc, project, branch=branch)
    repo = resolve_repo(repo_arg, runner)
    return ResourceRef(kind, repo.host, repo.project, branch=branch)


@dataclass(frozen=True, slots=True)
class BranchMr:
    host: str
    project: str
    iid: int
    web_url: str
    head_pipeline_id: int | None
    source_branch: str | None


_MR_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<host>[^/?#]+)/(?P<project>.+?)/-/merge_requests/(?P<iid>\d+)"
    r"(?:/[^?#]*)?(?:\?[^#]*)?(?:#.*)?$"
)
"""An MR URL as a browser shows it: also with a tab (`/diffs`, `/commits`, `/pipelines`, `/overview`), a query string
or an anchor such as `#note_<id>` after the MR's number."""


def parse_mr_url(url: str) -> tuple[str, str, int]:
    """`https://<host>/<project>/-/merge_requests/<iid>`, with any tab, query or anchor → (host, project, iid).
    Raises RefError otherwise."""
    match = _MR_URL_RE.match(url.strip())
    if not match:
        raise RefError(f"Invalid GitLab MR URL: {url}")
    return match.group("host"), match.group("project"), int(match.group("iid"))


def normalize_mr_url(url: str) -> str:
    """The plain URL of the MR an MR URL points into: no tab, query or anchor. Raises RefError like `parse_mr_url`.

    Links built from it, such as a thread's `<mr url>#note_<id>`, then get exactly one anchor."""
    match = _MR_URL_RE.match(url.strip())
    if not match:
        raise RefError(f"Invalid GitLab MR URL: {url}")
    scheme, host, project, iid = match.group("scheme", "host", "project", "iid")
    return f"{scheme}://{host}/{project}/-/merge_requests/{iid}"


_PIPELINE_URL_RE = re.compile(r"^https?://(?P<host>[^/]+)/(?P<project>.+?)/-/pipelines/(?P<id>\d+)/?$")


def parse_pipeline_url(url: str) -> tuple[str, str, int]:
    """`https://<host>/<project>/-/pipelines/<id>` → (host, project, id). Raises RefError otherwise."""
    match = _PIPELINE_URL_RE.match(url.strip())
    if not match:
        raise RefError(f"Invalid GitLab pipeline URL: {url}")
    return match.group("host"), match.group("project"), int(match.group("id"))


def current_branch_mr(runner: SyncRunner = _default_runner) -> BranchMr:
    """The merge request of the current git branch, as `glab mr view -F json` finds it."""
    result = runner(["mr", "view", "-F", "json"])
    try:
        payload = json.loads(result.stdout or b"null")
    except ValueError:
        payload = None
    if result.returncode != 0 or not isinstance(payload, dict) or "web_url" not in payload:
        raise RefError(f"glab mr view failed: {stderr_message(result)}")
    host, project, iid = parse_mr_url(str(payload["web_url"]))
    head = payload.get("head_pipeline")
    head_id = head.get("id") if isinstance(head, dict) else None
    return BranchMr(
        host,
        project,
        iid,
        str(payload["web_url"]),
        int(head_id) if isinstance(head_id, int) else None,
        payload.get("source_branch"),
    )
