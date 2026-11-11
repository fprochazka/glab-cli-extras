"""GitLab Free has no `MergeRequest.approvalsLeft`: the MR snapshot is asked again without it."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras.gitlab.client import ApiError, GitLabClient
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.sync import mr as mr_sync
from shapes import HOST, PROJECT, gql_mr

REF = ResourceRef("mr", HOST, PROJECT, 42)
MISSING_MESSAGE = "Field 'approvalsLeft' doesn't exist on type 'MergeRequest' (Did you mean `approved`?)"
MISSING = {"errors": [{"message": MISSING_MESSAGE}]}


@pytest.fixture(autouse=True)
def forget_hosts() -> Iterator[None]:
    mr_sync._hosts_without_approvals_left.clear()
    yield
    mr_sync._hosts_without_approvals_left.clear()


def free_tier_mr() -> dict[str, object]:
    data = gql_mr()
    del data["project"]["mergeRequest"]["approvalsLeft"]
    return {"data": data}


def snapshot_queries(fake: FakeGlab) -> list[str]:
    return [str(c.body["query"]) for c in fake.calls if c.endpoint == "graphql"]


def test_free_tier_snapshot_is_asked_again_without_approvals_left_and_remembered(clock: Clock) -> None:
    fake = FakeGlab(
        [
            Route("MergeRequestSnapshot", MISSING, contains="approvalsLeft"),
            Route("MergeRequestSnapshot", free_tier_mr()),
        ]
    )
    client = GitLabClient(HOST, runner=fake, clock=clock)

    snap = asyncio.run(mr_sync.fetch_snapshot(client, REF, with_threads=False, with_pipeline=False))
    assert snap.iid == 42
    assert snap.approvals_left is None
    assert ["approvalsLeft" in q for q in snapshot_queries(fake)] == [True, False]

    fake.calls.clear()
    asyncio.run(mr_sync.fetch_snapshot(client, REF, with_threads=False, with_pipeline=False))
    assert ["approvalsLeft" in q for q in snapshot_queries(fake)] == [False]


def test_other_graphql_errors_are_not_swallowed(clock: Clock) -> None:
    fake = FakeGlab([Route("MergeRequestSnapshot", {"errors": [{"message": "Field 'title' doesn't exist"}]})])
    client = GitLabClient(HOST, runner=fake, clock=clock)

    with pytest.raises(ApiError, match="'title'"):
        asyncio.run(mr_sync.fetch_snapshot(client, REF, with_threads=False, with_pipeline=False))
    assert len(snapshot_queries(fake)) == 1
