"""The dashboard's data in as few GraphQL requests as it takes.

The dashboard runs one query per list. This runs one request for both supersets of open MRs (authored or assigned,
and review requested), 50 per side and page, which keeps the query complexity near 150 of GitLab's 250. A side with
more pages gets follow-up requests for that side only, at most `MAX_PAGES` per side. The same request reads the
user's dashboard preferences and whether the Duo bot exists. The Merged tab costs a request of its own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from glab_cli_extras.dashboard.lists import DUO_BOT
from glab_cli_extras.dashboard.model import DashboardMr, mr_from_node
from glab_cli_extras.gitlab.client import ApiError, GitLabClient

PAGE_SIZE = 50
MERGED_PAGE_SIZE = 100
MAX_PAGES = 5
MERGED_WINDOW = timedelta(days=14)
GID_RE = re.compile(r"gid://gitlab/MergeRequest/\d+")
"""What `fetch_states` puts into its query text: nothing else from a response gets there."""

MR_FIELDS = """
  id iid reference(full: true) title webUrl draft state createdAt updatedAt mergedAt sourceBranch targetBranch
  diffHeadSha detailedMergeStatus
  author { username } assignees { nodes { username } } labels { nodes { title } } headPipeline { status }
  reviewers { nodes { username mergeRequestInteraction { reviewState updatedAt } } }
"""

OPEN_QUERY = (
    """query MyMergeRequests($first: Int!, $authoredAfter: String, $reviewingAfter: String,
  $withAuthored: Boolean!, $withReviewing: Boolean!, $withUser: Boolean!) {
  duoBot: user(username: \""""
    + DUO_BOT
    + """\") @include(if: $withUser) { username }
  currentUser {
    username
    userPreferences @include(if: $withUser) {
      listType: mergeRequestDashboardListType showDrafts: mergeRequestDashboardShowDrafts
    }
    authored: authoredMergeRequests(includeAssigned: true, state: opened, first: $first, after: $authoredAfter,
      sort: UPDATED_DESC) @include(if: $withAuthored) { pageInfo { hasNextPage endCursor } nodes { ...M } }
    reviewing: reviewRequestedMergeRequests(state: opened, first: $first, after: $reviewingAfter,
      sort: UPDATED_DESC) @include(if: $withReviewing) { pageInfo { hasNextPage endCursor } nodes { ...M } }
  }
}
fragment M on MergeRequest {"""
    + MR_FIELDS
    + "}\n"
)

MERGED_QUERY = (
    """query MyMergedMergeRequests($first: Int!, $mergedAfter: Time!, $authoredAfter: String,
  $reviewingAfter: String, $withAuthored: Boolean!, $withReviewing: Boolean!) {
  currentUser {
    authored: authoredMergeRequests(includeAssigned: true, state: merged, mergedAfter: $mergedAfter, first: $first,
      after: $authoredAfter, sort: MERGED_AT_DESC) @include(if: $withAuthored) {
      pageInfo { hasNextPage endCursor } nodes { ...M }
    }
    reviewing: reviewRequestedMergeRequests(state: merged, mergedAfter: $mergedAfter, first: $first,
      after: $reviewingAfter, sort: MERGED_AT_DESC) @include(if: $withReviewing) {
      pageInfo { hasNextPage endCursor } nodes { ...M }
    }
  }
}
fragment M on MergeRequest {"""
    + MR_FIELDS
    + "}\n"
)


@dataclass
class SidePages:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    cursor: str | None = None
    more: bool = True


@dataclass
class DashboardRead:
    username: str
    list_type: str | None
    """`ACTION_BASED` or `ROLE_BASED`, the user's dashboard preference."""
    show_drafts: bool
    duo_bot: bool
    """Whether the Duo bot's username exists on the host."""
    mrs: list[DashboardMr]
    """The open MRs of both sets, each once, `mine` and `review_requested` telling which sets it is in."""
    requests: int = 0
    truncated: bool = False
    """A side had more than `MAX_PAGES` pages; the rest is missing."""


def _side(data: Any, name: str) -> tuple[list[dict[str, Any]], bool, str | None]:
    conn = data.get(name) if isinstance(data, dict) else None
    if not isinstance(conn, dict):
        return [], False, None
    info = conn.get("pageInfo") or {}
    nodes = [n for n in conn.get("nodes") or [] if isinstance(n, dict)]
    return nodes, bool(info.get("hasNextPage")), info.get("endCursor")


async def _paged(
    client: GitLabClient, query: str, variables: dict[str, Any], *, first_page_extra: dict[str, Any] | None = None
) -> tuple[Any, dict[str, SidePages], int, bool]:
    """Run `query` until both sides are read or hit `MAX_PAGES`. Returns the first response's `currentUser` and the
    top-level data, the nodes per side, the request count and whether a side was cut off."""
    sides = {"authored": SidePages(), "reviewing": SidePages()}
    first_data: Any = None
    requests = 0
    truncated = False
    for page in range(MAX_PAGES):
        wanted = {name: s.more for name, s in sides.items()}
        if not any(wanted.values()):
            break
        page_vars = {
            **variables,
            "authoredAfter": sides["authored"].cursor,
            "reviewingAfter": sides["reviewing"].cursor,
            "withAuthored": wanted["authored"],
            "withReviewing": wanted["reviewing"],
        }
        for name, value in (first_page_extra or {}).items():
            # What only the first page asks for, such as the preferences, is switched off on the follow-ups.
            page_vars[name] = value if page == 0 else False
        result = await client.graphql(query, page_vars, kind="dashboard")
        requests += 1
        data = result.data if isinstance(result.data, dict) else {}
        if page == 0:
            first_data = data
        user = data.get("currentUser")
        if not isinstance(user, dict):
            raise ApiError("GitLab returned no current user; check `glab auth status`")
        for name, side in sides.items():
            if not wanted[name]:
                continue
            nodes, more, cursor = _side(user, name)
            side.nodes.extend(nodes)
            side.more, side.cursor = more and bool(cursor), cursor
            if side.more and page == MAX_PAGES - 1:
                truncated = True
    return first_data, sides, requests, truncated


def merge_sets(authored: Sequence[dict[str, Any]], reviewing: Sequence[dict[str, Any]]) -> list[DashboardMr]:
    """Both sets as one list of MRs, an MR in both sets once with both flags."""
    by_ref: dict[str, DashboardMr] = {}
    for node in authored:
        mr = mr_from_node(node, mine=True)
        by_ref[mr.reference] = mr
    for node in reviewing:
        mr = mr_from_node(node, review_requested=True)
        known = by_ref.get(mr.reference)
        by_ref[mr.reference] = replace(known, review_requested=True) if known is not None else mr
    return list(by_ref.values())


async def fetch_open(client: GitLabClient) -> DashboardRead:
    """The user's open MRs of both sets, the dashboard preferences, and whether the Duo bot exists."""
    data, sides, requests, truncated = await _paged(
        client, OPEN_QUERY, {"first": PAGE_SIZE}, first_page_extra={"withUser": True}
    )
    user = data.get("currentUser") or {}
    prefs = user.get("userPreferences") or {}
    show_drafts = prefs.get("showDrafts")
    return DashboardRead(
        username=str(user.get("username") or ""),
        list_type=str(prefs["listType"]) if prefs.get("listType") else None,
        show_drafts=True if show_drafts is None else bool(show_drafts),
        duo_bot=isinstance(data.get("duoBot"), dict),
        mrs=merge_sets(sides["authored"].nodes, sides["reviewing"].nodes),
        requests=requests,
        truncated=truncated,
    )


def merged_after(now: datetime) -> str:
    return (now - MERGED_WINDOW).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def fetch_merged(client: GitLabClient, now: datetime) -> tuple[list[DashboardMr], int, bool]:
    """The Merged tab: the MRs of both sets merged in the last two weeks, the request count, and whether a side was
    cut off."""
    _, sides, requests, truncated = await _paged(
        client, MERGED_QUERY, {"first": MERGED_PAGE_SIZE, "mergedAfter": merged_after(now)}
    )
    return merge_sets(sides["authored"].nodes, sides["reviewing"].nodes), requests, truncated


async def fetch_states(client: GitLabClient, gids: Sequence[str]) -> dict[str, tuple[str, str | None]]:
    """`{gid: (state, merged_at)}` of these MRs, in one request. An MR GitLab does not return (deleted, or no longer
    visible) is left out."""
    if not gids:
        return {}
    valid = [gid for gid in gids if GID_RE.fullmatch(gid)]
    if not valid:
        return {}
    fields = " ".join(f'm{i}: mergeRequest(id: "{gid}") {{ id state mergedAt }}' for i, gid in enumerate(valid))
    result = await client.graphql(f"query MyMergeRequestStates {{ {fields} }}", {}, kind="dashboard-states")
    data = result.data if isinstance(result.data, dict) else {}
    states: dict[str, tuple[str, str | None]] = {}
    for node in data.values():
        if isinstance(node, dict) and node.get("id"):
            merged = node.get("mergedAt")
            states[str(node["id"])] = (str(node.get("state") or "").lower(), str(merged) if merged else None)
    return states
