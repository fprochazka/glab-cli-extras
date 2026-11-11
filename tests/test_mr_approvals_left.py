"""GitLab CE has no `MergeRequest.approvalsLeft`: the MR snapshot leaves it out there.

The host's edition comes from a probe kept for a day, on disk for the commands and in memory for the daemon. GitLab's
rejection of the field is the fallback when the probe got no answer or the kept answer is wrong.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import Clock
from fakeglab import FakeGlab, Route
from glab_cli_extras import paths
from glab_cli_extras.daemon.engine import Engine
from glab_cli_extras.gitlab import capabilities as caps
from glab_cli_extras.gitlab.capabilities import Capabilities, CapabilityStore, DiskCapabilityStore
from glab_cli_extras.gitlab.client import ApiError, GitLabClient
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.gitlab.retry import standalone_client
from glab_cli_extras.sync import mr as mr_sync
from shapes import HOST, PROJECT, gql_mr

REF = ResourceRef("mr", HOST, PROJECT, 42)
MISSING_MESSAGE = "Field 'approvalsLeft' doesn't exist on type 'MergeRequest' (Did you mean `approved`?)"
MISSING = {"errors": [{"message": MISSING_MESSAGE}]}


def probe(enterprise: bool | None, version: str = "17.5.0") -> Route:
    return Route("GitLabCapabilities", {"data": {"metadata": {"version": version, "enterprise": enterprise}}})


def free_tier_mr() -> dict[str, object]:
    data = gql_mr()
    del data["project"]["mergeRequest"]["approvalsLeft"]
    return {"data": data}


CE_ROUTES = [
    Route("MergeRequestSnapshot", MISSING, contains="approvalsLeft"),
    Route("MergeRequestSnapshot", free_tier_mr()),
]
EE_ROUTES = [Route("MergeRequestSnapshot", {"data": gql_mr()})]


def requests(fake: FakeGlab) -> list[str]:
    """Each GraphQL request: `probe`, `with` (approvalsLeft) or `without`."""
    result = []
    for call in fake.calls:
        query = str(call.body["query"])
        if "GitLabCapabilities" in query:
            result.append("probe")
        else:
            result.append("with" if "approvalsLeft" in query else "without")
    return result


def fetch(client: GitLabClient) -> mr_sync.MrSnapshot:
    return asyncio.run(mr_sync.fetch_snapshot(client, REF, with_threads=False, with_pipeline=False))


def disk_client(fake: FakeGlab, clock: Clock, root: Path) -> GitLabClient:
    return GitLabClient(HOST, runner=fake, clock=clock, capabilities=DiskCapabilityStore(root))


def test_probe_is_parsed(clock: Clock) -> None:
    client = GitLabClient(HOST, runner=FakeGlab([probe(False, "16.11.2")]), clock=clock)

    found = asyncio.run(client.capabilities())
    assert found == Capabilities(clock.now, "16.11.2", False)
    assert not found.has_mr_field("approvalsLeft")
    assert found.has_mr_field("title")


def test_ce_host_is_asked_without_approvals_left_on_the_first_try(clock: Clock) -> None:
    fake = FakeGlab([probe(False), *CE_ROUTES])

    snap = fetch(GitLabClient(HOST, runner=fake, clock=clock))
    assert snap.iid == 42
    assert snap.approvals_left is None
    assert requests(fake) == ["probe", "without"]


def test_ee_host_is_asked_with_approvals_left(clock: Clock) -> None:
    fake = FakeGlab([probe(True), *EE_ROUTES])

    assert fetch(GitLabClient(HOST, runner=fake, clock=clock)).approvals_left == 1
    assert requests(fake) == ["probe", "with"]


def test_concurrent_snapshots_share_one_probe(clock: Clock) -> None:
    fake = FakeGlab([probe(False), *CE_ROUTES])
    client = GitLabClient(HOST, runner=fake, clock=clock)

    async def both() -> None:
        snapshot = mr_sync.fetch_snapshot(client, REF, with_threads=False, with_pipeline=False)
        await asyncio.gather(snapshot, mr_sync.fetch_snapshot(client, REF, with_threads=False, with_pipeline=False))

    asyncio.run(both())
    assert requests(fake) == ["probe", "without", "without"]


def test_cached_capabilities_spare_the_probe_and_the_failing_request(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([probe(False), *CE_ROUTES])
    fetch(disk_client(fake, clock, tmp_path))
    assert json.loads((tmp_path / HOST / "capabilities.json").read_text())["enterprise"] is False

    fake.calls.clear()
    fetch(disk_client(fake, clock, tmp_path))
    assert requests(fake) == ["without"]


def test_capabilities_expire_after_a_day(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([probe(False), *CE_ROUTES])
    fetch(disk_client(fake, clock, tmp_path))

    clock.advance(caps.TTL - 1)
    fake.calls.clear()
    fetch(disk_client(fake, clock, tmp_path))
    assert requests(fake) == ["without"]

    clock.advance(1)
    fake.calls.clear()
    fetch(disk_client(fake, clock, tmp_path))
    assert requests(fake) == ["probe", "without"]


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe", b'{"format": 99}', b'{"format": 1, "checked_at": "x"}'])
def test_unreadable_cache_file_reads_as_missing(clock: Clock, tmp_path: Path, content: bytes) -> None:
    path = tmp_path / HOST / "capabilities.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    fake = FakeGlab([probe(False), *CE_ROUTES])

    fetch(disk_client(fake, clock, tmp_path))
    assert requests(fake) == ["probe", "without"]
    assert json.loads(path.read_text())["enterprise"] is False


def test_wrong_cached_capabilities_are_corrected_by_the_rejection(clock: Clock, tmp_path: Path) -> None:
    DiskCapabilityStore(tmp_path).save(HOST, Capabilities(clock.now, "17.5.0", True))
    fake = FakeGlab(CE_ROUTES)

    assert fetch(disk_client(fake, clock, tmp_path)).approvals_left is None
    assert requests(fake) == ["with", "without"]
    saved = json.loads((tmp_path / HOST / "capabilities.json").read_text())
    assert saved["missing_mr_fields"] == ["approvalsLeft"]
    assert saved["checked_at"] == clock.now

    fake.calls.clear()
    fetch(disk_client(fake, clock, tmp_path))
    assert requests(fake) == ["without"]


@pytest.mark.parametrize("status", [502, 429, 401])
def test_probe_without_answer_falls_back_to_the_rejection_and_is_not_written(
    clock: Clock, tmp_path: Path, status: int
) -> None:
    fake = FakeGlab([Route("GitLabCapabilities", status=status), *CE_ROUTES])
    client = disk_client(fake, clock, tmp_path)

    assert fetch(client).approvals_left is None
    assert requests(fake) == ["probe", "with", "without"]
    # The field GitLab rejected is an answer, kept for the next run; the failed probe is not.
    saved = json.loads((tmp_path / HOST / "capabilities.json").read_text())
    assert (saved["enterprise"], saved["missing_mr_fields"]) == (None, ["approvalsLeft"])

    fake.calls.clear()
    fetch(client)
    assert requests(fake) == ["without"]


def test_failed_probe_is_retried_after_an_hour_in_a_long_running_process(clock: Clock) -> None:
    failure = Route("GitLabCapabilities", status=503)
    fake = FakeGlab([failure, *EE_ROUTES])
    client = GitLabClient(HOST, runner=fake, clock=clock)

    fetch(client)
    fetch(client)
    assert requests(fake) == ["probe", "with", "with"]

    clock.advance(caps.PROBE_RETRY)
    fake.routes.remove(failure)
    fake.routes.insert(0, probe(True))
    fake.calls.clear()
    fetch(client)
    assert requests(fake) == ["probe", "with"]


def test_probe_rejected_by_an_old_gitlab_is_kept_as_unknown(clock: Clock, tmp_path: Path) -> None:
    old = Route("GitLabCapabilities", {"errors": [{"message": "Field 'enterprise' doesn't exist on type 'Metadata'"}]})
    fake = FakeGlab([old, *EE_ROUTES])

    fetch(disk_client(fake, clock, tmp_path))
    fetch(disk_client(fake, clock, tmp_path))
    assert requests(fake) == ["probe", "with", "with"]


def test_daemon_keeps_capabilities_in_memory_and_writes_no_file(clock: Clock, tmp_path: Path) -> None:
    fake = FakeGlab([probe(False), *CE_ROUTES])
    engine = Engine(runner=fake, clock=clock)

    fetch(engine.client_for(HOST))
    fetch(engine.client_for(HOST))
    assert requests(fake) == ["probe", "without", "without"]
    assert type(engine.client_for(HOST).capability_store) is CapabilityStore
    assert not paths.cache_dir().exists()
    assert not list(tmp_path.rglob("capabilities.json"))


def test_commands_keep_capabilities_in_the_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = standalone_client(HOST).capability_store
    assert isinstance(store, DiskCapabilityStore)
    assert store.path(HOST) == tmp_path / "cache" / HOST / "capabilities.json"

    monkeypatch.delenv(paths.CACHE_DIR_ENV)
    assert paths.cache_dir() == tmp_path / "xdg-cache" / paths.APP


def test_other_graphql_errors_are_not_swallowed(clock: Clock) -> None:
    fake = FakeGlab(
        [probe(True), Route("MergeRequestSnapshot", {"errors": [{"message": "Field 'title' doesn't exist"}]})]
    )
    client = GitLabClient(HOST, runner=fake, clock=clock)

    with pytest.raises(ApiError, match="'title'"):
        fetch(client)
    assert requests(fake) == ["probe", "with"]
