"""Merge requests: the snapshot model, the GraphQL snapshot fetch, and the REST fetch of discussion threads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from glab_cli_extras.discussion.models import UserInfo
from glab_cli_extras.gitlab import queries
from glab_cli_extras.gitlab.client import GitLabClient, NotFound, encode_project
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.gitlab.values import gid_to_int, lower
from glab_cli_extras.serde import stable_hash
from glab_cli_extras.store.threads import is_system_discussion

THREAD_LIST_THRESHOLD = 3
"""Above this many changed threads, one paginated list request is cheaper than one request per thread."""
MAX_THREAD_PAGES = 10


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
    approved_by: tuple[str, ...]
    reviewers: tuple[str, ...]
    head_pipeline_id: int | None = None
    head_pipeline_status: str | None = None
    head_pipeline_project: str | None = None
    threads: tuple[ThreadState, ...] | None = None
    """None when no subscriber wants comment events, so the query leaves discussions out."""


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
        approved=node.get("approved"),
        approvals_left=node.get("approvalsLeft"),
        approved_by=tuple(sorted(n["username"] for n in (node.get("approvedBy") or {}).get("nodes") or [])),
        reviewers=tuple(sorted(n["username"] for n in (node.get("reviewers") or {}).get("nodes") or [])),
        head_pipeline_id=gid_to_int(pipeline["id"]) if pipeline else None,
        head_pipeline_status=lower(pipeline.get("status")) if pipeline else None,
        head_pipeline_project=((pipeline.get("project") or {}).get("fullPath") or project) if pipeline else None,
        threads=threads,
    )


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
    result = await client.graphql(queries.load("mr"), variables, kind="mr")
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
            more = await client.graphql(queries.load("mr"), variables, kind="mr")
            connection = ((((more.data or {}).get("project") or {}).get("mergeRequest") or {}).get("discussions")) or {}
            nodes.extend(connection.get("nodes") or [])
            pages += 1
        threads = parse_threads(nodes)
    return parse_mr(node, ref.project, threads)


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
