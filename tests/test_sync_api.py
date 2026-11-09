from __future__ import annotations

import pytest

from fakeglab import FakeGlab, Route
from glab_cli_extras.gitlab import sync_api
from glab_cli_extras.gitlab.sync_api import GlabApiError, glab_api, glab_graphql_mutation, glab_graphql_query

HOST = "gitlab.example.com"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGlab:
    fake = FakeGlab()
    monkeypatch.setattr(sync_api, "RUNNER", fake.sync)
    monkeypatch.setattr(sync_api, "SLEEP", lambda seconds: None)
    return fake


def test_json_body_on_stdin_and_status_from_the_status_line(fake: FakeGlab) -> None:
    fake.routes = [
        Route("discussions", {"id": "d1", "notes": [{"id": 5}]}, status=201),
        Route("missing", {"message": "404 Discussion Not Found"}, status=404),
    ]
    position = {"position_type": "text", "new_path": "a.py", "new_line": 3}
    result = glab_api(
        "projects/1/discussions", hostname=HOST, method="POST", json_body={"body": "x", "position": position}
    )
    assert result["id"] == "d1"
    assert fake.calls[0].body == {"body": "x", "position": position}
    assert "--input" in fake.calls[0].args and "-f" not in fake.calls[0].args
    with pytest.raises(GlabApiError) as missing:
        glab_api("projects/1/missing", hostname=HOST)
    assert missing.value.http_status == 404
    assert "Discussion Not Found" in str(missing.value)


def test_writes_are_never_retried_reads_are(fake: FakeGlab) -> None:
    fake.routes = [Route("flaky", {"message": "500"}, status=500)]
    with pytest.raises(GlabApiError):
        glab_api("projects/flaky", hostname=HOST, method="POST", json_body={"note": "x"})
    assert len(fake.calls) == 1
    with pytest.raises(GlabApiError):
        glab_graphql_mutation("mutation flaky {}", {"usernames": ["me"]}, hostname=HOST)
    assert len(fake.calls) == 2
    with pytest.raises(GlabApiError):
        glab_api("projects/flaky", hostname=HOST)
    assert len(fake.calls) == 7
    with pytest.raises(GlabApiError):
        glab_graphql_query("query flaky {}", {}, hostname=HOST)
    assert len(fake.calls) == 12


def test_mutation_errors_are_data_and_top_level_errors_raise(fake: FakeGlab) -> None:
    fake.routes = [
        Route("Destroy", {"data": {"m": {"errors": ["User has not requested changes for this merge request"]}}}),
        Route("Broken", {"errors": [{"message": "Field 'x' doesn't exist"}]}),
    ]
    data = glab_graphql_mutation("mutation Destroy {}", {}, hostname=HOST)
    assert data["m"]["errors"] == ["User has not requested changes for this merge request"]
    with pytest.raises(GlabApiError, match="doesn't exist"):
        glab_graphql_mutation("mutation Broken {}", {}, hostname=HOST)


def test_paginate_follows_x_next_page(fake: FakeGlab) -> None:
    fake.routes = [
        Route("draft_notes", [{"id": 1}], contains="&page=1", headers={"X-Next-Page": "2"}),
        Route("draft_notes", [{"id": 2}], contains="&page=2"),
    ]
    assert glab_api("projects/1/draft_notes", hostname=HOST, paginate=True) == [{"id": 1}, {"id": 2}]


def test_empty_body_is_none(fake: FakeGlab) -> None:
    fake.routes = [Route("draft_notes/4", None, status=204)]
    assert glab_api("projects/1/draft_notes/4", hostname=HOST, method="DELETE") is None
