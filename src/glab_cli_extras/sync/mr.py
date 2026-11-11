"""Merge requests: the snapshot model, the GraphQL snapshot fetch, and the REST fetches of discussion threads and of
the head commit's external statuses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from glab_cli_extras.discussion.models import UserInfo
from glab_cli_extras.gitlab import queries
from glab_cli_extras.gitlab.client import ApiError, GitLabClient, GraphQLResult, NotFound, Unauthorized, encode_project
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.gitlab.values import gid_to_int, lower
from glab_cli_extras.serde import stable_hash
from glab_cli_extras.status.diff import external_statuses
from glab_cli_extras.store.threads import is_system_discussion

THREAD_LIST_THRESHOLD = 3
"""Above this many changed threads, one paginated list request is cheaper than one request per thread."""
MAX_THREAD_PAGES = 10
MAX_STATUS_PAGES = 5
"""The head commit's statuses include every job of every pipeline on it, 100 to a page."""
APPROVALS_LEFT_MISSING = "Field 'approvalsLeft' doesn't exist"
"""GitLab Free has no approval rules, so its schema has no `MergeRequest.approvalsLeft` and rejects the whole query."""
_hosts_without_approvals_left: set[str] = set()


@dataclass(frozen=True, slots=True)
class NoteState:
    id: int
    updated_at: str
    author: str | None
    author_name: str | None = None
    author_bot: bool = False


@dataclass(frozen=True, slots=True)
class ThreadState:
    id: str
    resolvable: bool
    resolved: bool
    notes: tuple[NoteState, ...]


@dataclass(frozen=True, slots=True)
class ExternalStatus:
    """A commit status on the head that no job of the head pipeline produced: a quality gate outside the CI, say."""

    name: str
    status: str
    description: str | None = None
    target_url: str | None = None
    allow_failure: bool = False


@dataclass(frozen=True, slots=True)
class MrSnapshot:
    iid: int
    project: str
    state: str
    title: str
    description_hash: str
    draft: bool
    merge_status: str | None
    head_sha: str | None
    source_branch: str | None
    target_branch: str | None
    web_url: str | None
    approved: bool | None
    approvals_left: int | None
    approved_by: tuple[str, ...] | None
    """None when GitLab did not return the approvals (permissions, tier): unknown, not "nobody approved"."""
    reviewers: tuple[str, ...]
    head_pipeline_id: int | None = None
    head_pipeline_status: str | None = None
    head_pipeline_project: str | None = None
    head_pipeline_web_url: str | None = None
    head_pipeline_sha: str | None = None
    """The commit the head pipeline ran on. GitLab keeps the old head pipeline until a push gets its own."""
    head_pipeline_failed_jobs: tuple[str, ...] = ()
    """The names of the head pipeline's failed jobs that may not fail; free in the same query."""
    threads: tuple[ThreadState, ...] | None = None
    """None when no subscriber wants comment events, so the query leaves discussions out."""
    assignees: tuple[str, ...] | None = None
    labels: tuple[str, ...] | None = None
    blocking_threads_resolved: bool | None = None
    """GitLab's `mergeableDiscussionsState`: no unresolved thread blocks the merge."""
    external_statuses: tuple[ExternalStatus, ...] | None = None
    """None when no subscriber wants external status events, so the head commit's statuses are not fetched.

    The four fields above default to None, "unknown", so a snapshot kept by an older version compares with nothing
    instead of reporting every label and assignee as new."""


def _discussion_id(gid: Any) -> str:
    """GraphQL `gid://gitlab/Discussion/<sha>` → the REST discussion id `<sha>`."""
    return str(gid).rsplit("/", 1)[-1]


def parse_threads(nodes: list[Any]) -> tuple[ThreadState, ...]:
    threads = []
    for node in nodes:
        notes = (node.get("notes") or {}).get("nodes") or []
        if notes and all(n.get("system") for n in notes):
            continue
        threads.append(
            ThreadState(
                id=_discussion_id(node["id"]),
                resolvable=bool(node.get("resolvable")),
                resolved=bool(node.get("resolved")),
                notes=tuple(
                    NoteState(
                        id=gid_to_int(n["id"]),
                        updated_at=str(n.get("updatedAt") or ""),
                        author=(n.get("author") or {}).get("username"),
                        author_name=(n.get("author") or {}).get("name"),
                        author_bot=bool((n.get("author") or {}).get("bot")),
                    )
                    for n in notes
                    if not n.get("system")
                ),
            )
        )
    return tuple(sorted(threads, key=lambda t: t.id))


def parse_mr(node: Any, project: str, threads: tuple[ThreadState, ...] | None) -> MrSnapshot:
    if not node:
        raise NotFound(f"merge request not found in {project}", 404)
    pipeline = node.get("headPipeline") or None
    approved_by = node.get("approvedBy")
    approvers = approved_by.get("nodes") if isinstance(approved_by, dict) else None
    if isinstance(approvers, list):
        approvers = [n for n in approvers if isinstance(n, dict) and "username" in n]
    else:
        approvers = None
    return MrSnapshot(
        iid=int(node["iid"]),
        project=project,
        state=lower(node.get("state")),
        title=str(node.get("title") or ""),
        description_hash=stable_hash(str(node.get("description") or "")),
        draft=bool(node.get("draft")),
        merge_status=lower(node.get("detailedMergeStatus")) or None,
        head_sha=node.get("diffHeadSha"),
        source_branch=node.get("sourceBranch"),
        target_branch=node.get("targetBranch"),
        web_url=node.get("webUrl"),
        approved=node.get("approved") if approvers is not None else None,
        approvals_left=node.get("approvalsLeft"),
        approved_by=tuple(sorted(n["username"] for n in approvers)) if approvers is not None else None,
        reviewers=tuple(sorted(n["username"] for n in (node.get("reviewers") or {}).get("nodes") or [])),
        head_pipeline_id=gid_to_int(pipeline["id"]) if pipeline else None,
        head_pipeline_status=lower(pipeline.get("status")) if pipeline else None,
        head_pipeline_project=((pipeline.get("project") or {}).get("fullPath") or project) if pipeline else None,
        head_pipeline_web_url=_web_url(node.get("webUrl"), pipeline.get("path")) if pipeline else None,
        head_pipeline_sha=pipeline.get("sha") if pipeline else None,
        head_pipeline_failed_jobs=tuple(
            sorted(
                str(j.get("name"))
                for j in ((pipeline.get("jobs") or {}).get("nodes") or [])
                if isinstance(j, dict) and not j.get("allowFailure")
            )
        )
        if pipeline
        else (),
        threads=threads,
        assignees=_usernames(node.get("assignees")),
        labels=_names(node.get("labels"), "title"),
        blocking_threads_resolved=_bool_or_none(node.get("mergeableDiscussionsState")),
    )


def _names(connection: Any, key: str) -> tuple[str, ...] | None:
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    if not isinstance(nodes, list):
        return None
    return tuple(sorted(str(n[key]) for n in nodes if isinstance(n, dict) and n.get(key) is not None))


def _usernames(connection: Any) -> tuple[str, ...] | None:
    return _names(connection, "username")


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _web_url(mr_url: Any, path: Any) -> str | None:
    """The pipeline's URL from its path, on the host of the MR's URL."""
    if not isinstance(mr_url, str) or not isinstance(path, str) or "://" not in mr_url:
        return None
    scheme, rest = mr_url.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}{path}"


def snapshot_users(snapshot: MrSnapshot) -> dict[str, UserInfo]:
    """The note authors the snapshot knows, by username, with GitLab's bot flag and display name. A thread file
    needs both, and taking them from the snapshot saves a user lookup per author."""
    users: dict[str, UserInfo] = {}
    for thread in snapshot.threads or ():
        for note in thread.notes:
            if note.author:
                users[note.author] = UserInfo(0, note.author, note.author_name or note.author, note.author_bot)
    return users


def thread_sig(thread: ThreadState) -> str:
    """The version of a thread's published notes. It changes on a new note, an edit, a deletion, a resolve and an
    unresolve, because each of these moves a note's `updatedAt` or the note list."""
    return stable_hash((thread.resolved, [(n.id, n.updated_at) for n in thread.notes]))


async def fetch_snapshot(
    client: GitLabClient, ref: ResourceRef, *, with_threads: bool, with_pipeline: bool
) -> MrSnapshot:
    """One GraphQL snapshot of the MR, with its threads' note versions when asked, following thread pages."""
    variables: dict[str, str | int | bool] = {
        "project": ref.project,
        "iid": str(ref.id),
        "withThreads": with_threads,
        "withPipeline": with_pipeline,
    }
    result = await _query_snapshot(client, variables)
    node = ((result.data or {}).get("project") or {}).get("mergeRequest")
    if not node:
        raise NotFound(f"merge request !{ref.id} not found in {ref.project}", 404)
    threads: tuple[ThreadState, ...] | None = None
    if with_threads:
        connection = node.get("discussions") or {}
        nodes = list(connection.get("nodes") or [])
        pages = 1
        while (connection.get("pageInfo") or {}).get("hasNextPage") and pages < MAX_THREAD_PAGES:
            variables["threadsAfter"] = str(connection["pageInfo"]["endCursor"])
            more = await _query_snapshot(client, variables)
            connection = ((((more.data or {}).get("project") or {}).get("mergeRequest") or {}).get("discussions")) or {}
            nodes.extend(connection.get("nodes") or [])
            pages += 1
        threads = parse_threads(nodes)
    return parse_mr(node, ref.project, threads)


async def _query_snapshot(client: GitLabClient, variables: dict[str, str | int | bool]) -> GraphQLResult:
    """The MR snapshot query. On a host whose schema has no `approvalsLeft` (GitLab Free), it is asked again without
    the field, and that host is asked without it from then on; `approvals_left` stays None there."""
    if client.host not in _hosts_without_approvals_left:
        try:
            return await client.graphql(queries.load("mr"), variables, kind="mr")
        except ApiError as exc:
            if APPROVALS_LEFT_MISSING not in str(exc):
                raise
            _hosts_without_approvals_left.add(client.host)
    return await client.graphql(_query_without_approvals_left(), variables, kind="mr")


def _query_without_approvals_left() -> str:
    query, removed = re.subn(r"^[ \t]*approvalsLeft[ \t]*\n", "", queries.load("mr"), flags=re.MULTILINE)
    if removed != 1:
        raise RuntimeError("mr.graphql must select approvalsLeft on a line of its own")
    return query


async def fetch_discussions(client: GitLabClient, ref: ResourceRef, ids: set[str] | None) -> dict[str, Any]:
    """The REST discussions with these ids, or every non-system discussion when `ids` is None.

    A few threads are fetched one by one; above `THREAD_LIST_THRESHOLD`, one paginated list is cheaper.
    """
    base = f"projects/{encode_project(ref.project)}/merge_requests/{ref.id}/discussions"
    if ids is None or len(ids) > THREAD_LIST_THRESHOLD:
        items = await client.get_pages(base, kind="discussions", max_pages=MAX_THREAD_PAGES)
        found = {str(d["id"]): d for d in items if isinstance(d, dict) and "id" in d and not is_system_discussion(d)}
        return found if ids is None else {k: v for k, v in found.items() if k in ids}
    result = {}
    for discussion_id in sorted(ids):
        try:
            discussion = await client.get_json(f"{base}/{discussion_id}", kind="discussion")
        except NotFound:
            # Deleted, or briefly gone after a force-push rewrote a diff note: the caller keeps what it has.
            continue
        if isinstance(discussion, dict):
            result[discussion_id] = discussion
    return result


def parse_external_statuses(statuses: list[Any], pipeline_id: int | None) -> tuple[ExternalStatus, ...]:
    """The statuses no job of the head pipeline produced, one per name, sorted by name.

    The same rule as `glab-mr-status`: a status of the head pipeline is not external, and neither is a status that
    carries the name of one of its jobs (an older pipeline's run of the same job). The job names come from the
    statuses themselves, so this costs no request for the pipeline's jobs. GitLab lists the newest status first.
    """
    job_names = {
        str(s.get("name"))
        for s in statuses
        if isinstance(s, dict) and pipeline_id is not None and s.get("pipeline_id") == pipeline_id
    }
    found: dict[str, ExternalStatus] = {}
    for s in external_statuses(statuses, pipeline_id, job_names):
        name = str(s.get("name"))
        if name in found:
            continue
        found[name] = ExternalStatus(
            name=name,
            status=lower(s.get("status")),
            description=s.get("description") or None,
            target_url=s.get("target_url") or None,
            allow_failure=bool(s.get("allow_failure")),
        )
    return tuple(found[name] for name in sorted(found))


async def fetch_external_statuses(
    client: GitLabClient, ref: ResourceRef, sha: str, pipeline_id: int | None
) -> tuple[ExternalStatus, ...]:
    """The external statuses of the head commit. One request per 100 statuses; a status list GitLab refuses or no
    longer has (a commit pruned after a force-push) reads as none."""
    path = f"projects/{encode_project(ref.project)}/repository/commits/{sha}/statuses"
    try:
        statuses = await client.get_pages(path, kind="statuses", max_pages=MAX_STATUS_PAGES)
    except (NotFound, Unauthorized):
        return ()
    return parse_external_statuses(statuses, pipeline_id)
