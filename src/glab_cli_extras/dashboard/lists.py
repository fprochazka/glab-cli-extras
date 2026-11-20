"""The dashboard's lists and the rules that put an MR into them, without I/O.

GitLab defines the lists in `MergeRequestsHelper#merge_request_dashboard_data` (action-based) and
`#merge_request_dashboard_role_based_data` (role-based), and fetches each with its own query. Here the two supersets,
the user's open MRs as author or assignee and those that request the user's review, are fetched once and classified
with the same rules:

- The author side looks at the other reviewers' states, ignoring GitLab's Duo bot, as `ignoredReviewerUsername` does.
- The reviewer side looks at the user's own review state.

Each side partitions its set, so an open MR is in at most one list per side. The list ids are GitLab's own `id:`s.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from glab_cli_extras.dashboard.model import DashboardMr, Lists

Side = Literal["author", "reviewer"]
SIDES: tuple[Side, ...] = ("author", "reviewer")
ACTION_BASED = "action_based"
ROLE_BASED = "role_based"
LAYOUT_OPTIONS = ("auto", "action", "role")
"""`--layout`: `auto` follows the user's dashboard preference."""
DUO_BOT = "GitLabDuo"
"""The username of GitLab's Duo code review bot (`Users::Internal#duo_code_review_bot`), which the author-side lists
ignore as a reviewer. An instance where the name was taken gives the bot a suffixed name, which is not ignored."""


@dataclass(frozen=True, slots=True)
class ListDef:
    id: str
    side: Side
    title: str
    merged: bool = False


MERGED_LISTS = (
    ListDef("merged_recently_reviews", "reviewer", "Reviews merged in the last 2 weeks", merged=True),
    ListDef("merged_recently_assigned", "author", "Your merge requests merged in the last 2 weeks", merged=True),
)
ACTION_LISTS = (
    ListDef("returned_to_you", "author", "Returned to you"),
    ListDef("reviews_requested", "reviewer", "Review requested"),
    ListDef("assigned_to_you", "author", "Your merge requests"),
    ListDef("waiting_for_assignee", "reviewer", "Waiting for author or assignee"),
    ListDef("waiting_for_approvals", "author", "Waiting for approvals"),
    ListDef("approved_by_you", "reviewer", "Approved by you"),
    ListDef("approved_by_others", "author", "Approved by others"),
    *MERGED_LISTS,
)
ROLE_LISTS = (
    ListDef("reviews", "reviewer", "Review requested"),
    ListDef("assigned", "author", "Your merge requests"),
    ListDef("reviews_inactive", "reviewer", "Reviewed"),
    *MERGED_LISTS,
)
LISTS_BY_LAYOUT = {ACTION_BASED: ACTION_LISTS, ROLE_BASED: ROLE_LISTS}
SIDE_OF: dict[str, Side] = {d.id: d.side for d in (*ACTION_LISTS, *ROLE_LISTS)}
MERGED_LIST_BY_SIDE: dict[Side, str] = {"author": "merged_recently_assigned", "reviewer": "merged_recently_reviews"}

_ACTIVE = frozenset({"UNREVIEWED", "UNAPPROVED", "REVIEW_STARTED"})
_FEEDBACK = frozenset({"REVIEWED", "REQUESTED_CHANGES"})


def effective_layout(option: str | None, preference: str | None) -> str:
    """The layout in use: `action`/`role` pin it, otherwise the preference (`ACTION_BASED`/`ROLE_BASED`) decides."""
    if option == "action":
        return ACTION_BASED
    if option == "role":
        return ROLE_BASED
    return ROLE_BASED if (preference or "").lower() == ROLE_BASED else ACTION_BASED


def _author_list(mr: DashboardMr, layout: str, show_drafts: bool) -> str | None:
    if not mr.mine:
        return None
    if layout == ROLE_BASED:
        return None if mr.draft and not show_drafts else "assigned"
    states = [r.state for r in mr.reviewers if r.username != DUO_BOT]
    if not states:
        # No reviewer besides the Duo bot (`reviewerWildcard: NONE` or `onlyReviewerUsername`).
        return None if mr.draft and not show_drafts else "assigned_to_you"
    if any(s in _FEEDBACK for s in states):
        return "returned_to_you"
    if any(s in _ACTIVE for s in states):
        return "waiting_for_approvals"
    if all(s == "APPROVED" for s in states):
        return "approved_by_others"
    return None


def _reviewer_list(mr: DashboardMr, me: str, layout: str) -> str | None:
    if not mr.review_requested:
        return None
    state = mr.reviewer_state(me)
    if layout == ROLE_BASED:
        if state in _ACTIVE:
            return "reviews"
        if state in _FEEDBACK or state == "APPROVED":
            return "reviews_inactive"
        return None
    if state in _ACTIVE:
        return "reviews_requested"
    if state in _FEEDBACK:
        return "waiting_for_assignee"
    if state == "APPROVED":
        return "approved_by_you"
    return None


def classify(mr: DashboardMr, me: str, layout: str, show_drafts: bool) -> Lists:
    """The open-tab lists `mr` is in for user `me`."""
    return Lists(_author_list(mr, layout, show_drafts), _reviewer_list(mr, me, layout))


def merged_lists(mr: DashboardMr) -> Lists:
    """The Merged-tab lists of a merged MR."""
    return Lists(
        MERGED_LIST_BY_SIDE["author"] if mr.mine else None,
        MERGED_LIST_BY_SIDE["reviewer"] if mr.review_requested else None,
    )


class ListFilterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ListFilter:
    """`--list` and `--side`: which of the dashboard's lists to show or report. Empty means all of them."""

    lists: frozenset[str] = frozenset()
    side: Side | None = None

    @property
    def active(self) -> bool:
        return bool(self.lists) or self.side is not None

    def matches(self, list_id: str | None) -> bool:
        """Whether a list is selected. A list id names its side, so this needs no layout."""
        if list_id is None:
            return False
        if self.lists and list_id not in self.lists:
            return False
        return self.side is None or SIDE_OF.get(list_id) == self.side

    def selected(self, layout: str, *, include_merged: bool) -> tuple[ListDef, ...]:
        """The lists of `layout` to show, in the dashboard's order. Without `--list`, the Merged tab only when
        `include_merged`. Raises `ListFilterError` when `--list` names a list of the other layout or nothing is left."""
        defs = LISTS_BY_LAYOUT[layout]
        known = {d.id for d in defs}
        foreign = sorted(self.lists - known)
        if foreign:
            raise ListFilterError(
                f"--list: {', '.join(foreign)} not in the {layout} layout, whose lists are "
                f"{', '.join(d.id for d in defs)}"
            )
        chosen = tuple(d for d in defs if self.matches(d.id) and (bool(self.lists) or include_merged or not d.merged))
        if not chosen:
            raise ListFilterError(f"--list and --side={self.side} select no list")
        return chosen

    def describe(self) -> str:
        parts = []
        if self.lists:
            parts.append(f"lists {','.join(sorted(self.lists))}")
        if self.side is not None:
            parts.append(f"side {self.side}")
        return "; ".join(parts) or "all"

    def options(self) -> list[str]:
        """The filter as command-line options, for a restart command."""
        opts = [f"--list={','.join(sorted(self.lists))}"] if self.lists else []
        return opts + ([f"--side={self.side}"] if self.side is not None else [])


def parse_list_filter(values: Iterable[str] | None, side: str | None) -> ListFilter:
    """`--list` values (comma-separated, repeatable) and `--side`. Raises `ListFilterError` for an unknown id."""
    ids = frozenset(v.strip() for value in values or () for v in value.split(",") if v.strip())
    unknown = sorted(ids - set(SIDE_OF))
    if unknown:
        raise ListFilterError(f"--list: unknown list {', '.join(unknown)}; the lists are {', '.join(SIDE_OF)}")
    if side is not None and side not in SIDES:
        raise ListFilterError(f"--side must be one of {', '.join(SIDES)}")
    chosen: Side | None = "author" if side == "author" else "reviewer" if side == "reviewer" else None
    if chosen is not None and ids and not any(SIDE_OF[i] == chosen for i in ids):
        raise ListFilterError(f"--list and --side={chosen} select no list")
    return ListFilter(ids, chosen)


def list_title(list_id: str, layout: str) -> str:
    return next((d.title for d in LISTS_BY_LAYOUT.get(layout, ()) if d.id == list_id), list_id)


def open_list_ids(layout: str) -> Sequence[str]:
    return [d.id for d in LISTS_BY_LAYOUT[layout] if not d.merged]
