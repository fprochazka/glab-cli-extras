"""One merge request as the dashboard sees it, built from a GraphQL node of the dashboard query.

The dataclasses are frozen and use only the types `serde` handles, because `glab-watch my-mrs` keeps them in its
snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from glab_cli_extras.gitlab.values import lower


@dataclass(frozen=True, slots=True)
class Reviewer:
    username: str
    state: str | None
    """`mergeRequestInteraction.reviewState`: UNREVIEWED, REVIEW_STARTED, REVIEWED, REQUESTED_CHANGES, APPROVED,
    UNAPPROVED, or None when GitLab did not say."""
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class Lists:
    """The dashboard lists an MR is in: one where the user is author or assignee, one where the user is a reviewer."""

    author: str | None = None
    reviewer: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"author": self.author, "reviewer": self.reviewer}

    def ids(self) -> tuple[str, ...]:
        return tuple(i for i in (self.author, self.reviewer) if i is not None)


@dataclass(frozen=True, slots=True)
class DashboardMr:
    gid: str
    reference: str
    """`group/project!123`."""
    iid: int
    project: str
    title: str
    web_url: str
    author: str | None
    draft: bool
    state: str
    created_at: str
    updated_at: str
    merged_at: str | None
    source_branch: str | None
    target_branch: str | None
    sha: str | None
    detailed_merge_status: str | None
    """Lowercased, as the REST API spells it."""
    pipeline_status: str | None
    """The head pipeline's status, lowercased; None without a head pipeline."""
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    reviewers: tuple[Reviewer, ...]
    mine: bool = False
    """In the user's authored-or-assigned set."""
    review_requested: bool = False
    """In the set of MRs that request the user's review."""

    def reviewer_state(self, username: str) -> str | None:
        return next((r.state for r in self.reviewers if r.username == username), None)


def _usernames(value: Any) -> tuple[str, ...]:
    nodes = value.get("nodes") if isinstance(value, dict) else None
    return tuple(str(n["username"]) for n in nodes or [] if isinstance(n, dict) and n.get("username"))


def _reviewers(value: Any) -> tuple[Reviewer, ...]:
    nodes = value.get("nodes") if isinstance(value, dict) else None
    reviewers = []
    for node in nodes or []:
        if not isinstance(node, dict) or not node.get("username"):
            continue
        interaction = node.get("mergeRequestInteraction") or {}
        state = interaction.get("reviewState")
        updated_at = interaction.get("updatedAt")
        reviewers.append(
            Reviewer(str(node["username"]), str(state) if state else None, str(updated_at) if updated_at else None)
        )
    return tuple(reviewers)


def mr_from_node(node: dict[str, Any], *, mine: bool = False, review_requested: bool = False) -> DashboardMr:
    """A GraphQL `MergeRequest` node of the dashboard query as a `DashboardMr`."""
    reference = str(node.get("reference") or "")
    project = reference.rsplit("!", 1)[0]
    labels = node.get("labels")
    label_nodes = labels.get("nodes") if isinstance(labels, dict) else None
    pipeline = node.get("headPipeline")
    status = node.get("detailedMergeStatus")
    return DashboardMr(
        gid=str(node.get("id") or ""),
        reference=reference,
        iid=int(node.get("iid") or 0),
        project=project,
        title=str(node.get("title") or ""),
        web_url=str(node.get("webUrl") or ""),
        author=str((node.get("author") or {}).get("username") or "") or None,
        draft=bool(node.get("draft")),
        state=lower(node.get("state")) or "opened",
        created_at=str(node.get("createdAt") or ""),
        updated_at=str(node.get("updatedAt") or ""),
        merged_at=str(node["mergedAt"]) if node.get("mergedAt") else None,
        source_branch=node.get("sourceBranch"),
        target_branch=node.get("targetBranch"),
        sha=node.get("diffHeadSha"),
        detailed_merge_status=lower(status) if status else None,
        pipeline_status=lower(pipeline.get("status"))
        if isinstance(pipeline, dict) and pipeline.get("status")
        else None,
        labels=tuple(sorted(str(n.get("title")) for n in label_nodes or [] if isinstance(n, dict))),
        assignees=tuple(sorted(_usernames(node.get("assignees")))),
        reviewers=tuple(sorted(_reviewers(node.get("reviewers")), key=lambda r: r.username)),
        mine=mine,
        review_requested=review_requested,
    )


def rest_shape(mr: DashboardMr) -> dict[str, Any]:
    """The MR with the REST list's keys, for what the `mrs` watch already computes from them."""
    return {
        "iid": mr.iid,
        "title": mr.title,
        "state": mr.state,
        "author": {"username": mr.author},
        "source_branch": mr.source_branch,
        "target_branch": mr.target_branch,
        "draft": mr.draft,
        "web_url": mr.web_url,
        "updated_at": mr.updated_at,
        "created_at": mr.created_at,
        "sha": mr.sha,
        "detailed_merge_status": mr.detailed_merge_status,
        "labels": list(mr.labels),
        "assignees": [{"username": u} for u in mr.assignees],
        "reviewers": [{"username": r.username} for r in mr.reviewers],
    }
