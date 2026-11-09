from __future__ import annotations

import asyncio

import pytest

from conftest import Clock
from fakeglab import FakeGlab, Route, render
from glab_cli_extras.gitlab.client import (
    ApiError,
    GitLabClient,
    NotFound,
    RateLimited,
    TransportError,
    Unauthorized,
)
from glab_cli_extras.gitlab.glab import ProcessResult, build_api_args, parse_include_output
from glab_cli_extras.gitlab.retry import RetryPolicy

# The exact bytes glab 1.128 printed for a cached GraphQL 304: LF after the status line, CRLF after headers.
RECORDED_304 = (
    b"HTTP/2.0 304 Not Modified\n"
    b"Cache-Control: no-cache\r\n"
    b'Etag: W/"7b15c760a2f563108fa54aef5fd620d7"\r\n'
    b"X-Gitlab-From-Cache: true\r\n"
    b"\r\n"
)


def test_parse_recorded_304_output() -> None:
    response = parse_include_output(RECORDED_304)
    assert response is not None
    assert response.status == 304
    assert response.header("ETag") == 'W/"7b15c760a2f563108fa54aef5fd620d7"'
    assert response.body == b""


def test_parse_keeps_body_with_blank_lines() -> None:
    response = parse_include_output(render(200, "line one\n\nline three\n", {"Content-Type": "text/plain"}))
    assert response is not None
    assert response.text() == "line one\n\nline three\n"


def test_output_without_status_line_is_not_a_response() -> None:
    assert parse_include_output(b"") is None
    assert parse_include_output(b"          \n   ERROR  \n") is None


def test_build_api_args_sends_a_body_as_json_on_stdin_never_as_fields() -> None:
    args = build_api_args(
        "gitlab.example.com", "graphql", method="POST", headers={"If-None-Match": 'W/"x"'}, json_body=True
    )
    assert args[:4] == ["api", "--hostname", "gitlab.example.com", "-i"]
    assert args[4:8] == ["-X", "POST", "-H", 'If-None-Match: W/"x"']
    assert args[8:12] == ["-H", "Content-Type: application/json", "--input", "-"]
    assert "-f" not in args and "-F" not in args
    assert args[-1] == "graphql"


def test_graphql_variables_go_in_the_json_body_with_their_types(clock: Clock) -> None:
    fake = FakeGlab([Route("SetReviewers", {"data": {"x": {"errors": ["nope"]}}})])
    client = GitLabClient("gitlab.example.com", runner=fake, clock=clock)
    variables = {"usernames": ["@me", "true"], "flag": True, "first": 5}
    data = asyncio.run(client.graphql_mutation("mutation SetReviewers {}", variables))
    # A mutation's own errors are data for the caller, not an exception.
    assert data == {"x": {"errors": ["nope"]}}
    assert fake.calls[0].variables == variables
    assert fake.calls[0].method == "POST"


def test_write_bodies_are_json_and_a_202_body_is_returned(clock: Clock) -> None:
    fake = FakeGlab(
        [
            Route("merge_requests/7/notes", {"commands_changes": {}, "summary": ["Submitted the review."]}, status=202),
            Route("discussions/abc", None, status=204),
        ]
    )
    client = GitLabClient("gitlab.example.com", runner=fake, clock=clock)
    body = {"body": "@file-looking text\n\n<!-- plugin:command -->\n"}
    result = asyncio.run(client.request("POST", "projects/1/merge_requests/7/notes", json_body=body))
    assert result == {"commands_changes": {}, "summary": ["Submitted the review."]}
    assert fake.calls[0].body == body
    assert asyncio.run(client.request("PUT", "projects/1/merge_requests/7/discussions/abc", json_body={})) is None


def test_get_pages_follows_x_next_page(clock: Clock) -> None:
    fake = FakeGlab(
        [
            Route("items", [{"n": 1}], contains="&page=1", headers={"X-Next-Page": "2"}),
            Route("items", [{"n": 2}], contains="&page=2", headers={"X-Next-Page": ""}),
        ]
    )
    client = GitLabClient("gitlab.example.com", runner=fake, clock=clock)
    assert asyncio.run(client.get_pages("projects/1/items")) == [{"n": 1}, {"n": 2}]


def test_raw_control_characters_in_a_body_do_not_fail_the_response() -> None:
    response = parse_include_output(render(200, b'{"title": "a\tb\x01"}'))
    assert response is not None
    assert response.json() == {"title": "a\tb\x01"}


def test_graphql_etag_304_is_not_modified_and_cheap(clock: Clock) -> None:
    fake = FakeGlab([Route("PipelineTree", {"data": {"x": 1}}, etag='W/"e1"')])
    client = GitLabClient("gitlab.example.com", runner=fake, clock=clock)
    first = asyncio.run(client.graphql("query PipelineTree {}", {}, etag_resource="pipelines/id/5"))
    assert not first.not_modified and first.data == {"x": 1} and first.etag == 'W/"e1"'
    tokens_before = client.budget.tokens
    second = asyncio.run(
        client.graphql("query PipelineTree {}", {}, etag_resource="pipelines/id/5", if_none_match=first.etag)
    )
    assert second.not_modified and second.data is None
    assert fake.calls[1].headers["X-GITLAB-GRAPHQL-RESOURCE-ETAG"] == "/api/graphql:pipelines/id/5"
    assert fake.calls[1].headers["If-None-Match"] == 'W/"e1"'
    assert client.stats.not_modified == 1
    assert tokens_before - client.budget.tokens == pytest.approx(0.1, abs=0.01)


def test_rate_limit_headers_feed_the_budget(clock: Clock) -> None:
    fake = FakeGlab([Route("version", {"version": "19.5.3-ee"})])
    client = GitLabClient("gitlab.example.com", runner=fake, clock=clock)
    assert asyncio.run(client.version()) == (19, 5)
    assert client.budget.remaining == 14388
    assert client.budget.limit_per_minute == 480
    assert asyncio.run(client.version()) == (19, 5)
    assert len(fake.calls) == 1


def test_error_statuses_map_to_exceptions(clock: Clock) -> None:
    fake = FakeGlab(
        [
            Route("missing", {"message": "404 Not found"}, status=404),
            Route("forbidden", {"message": "403 Forbidden"}, status=403),
            Route("limited", {"message": "Retry later"}, status=429, headers={"Retry-After": "42"}),
            Route("broken", {"message": "500 Internal Server Error"}, status=500),
            Route("bad-graphql", {"errors": [{"message": "Field 'nope' doesn't exist"}]}),
        ]
    )
    client = GitLabClient("gitlab.example.com", runner=fake, clock=clock)
    with pytest.raises(NotFound):
        asyncio.run(client.get_json("projects/missing"))
    with pytest.raises(Unauthorized, match="glab auth status"):
        asyncio.run(client.get_json("projects/forbidden"))
    with pytest.raises(RateLimited) as limited:
        asyncio.run(client.get_json("projects/limited"))
    assert limited.value.retry_after == 42
    assert client.budget.blocked_until == clock.now + 42
    with pytest.raises(ApiError) as broken:
        asyncio.run(client.get_json("projects/broken"))
    assert broken.value.status == 500
    with pytest.raises(ApiError, match="doesn't exist"):
        asyncio.run(client.graphql("query bad-graphql", {}))
    with pytest.raises(TransportError, match="no fake route"):
        asyncio.run(client.get_json("projects/unknown"))


def test_reads_are_retried_and_writes_never(clock: Clock) -> None:
    fake = FakeGlab([Route("flaky", {"message": "502 Bad Gateway"}, status=502)])
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    client = GitLabClient("gitlab.example.com", runner=fake, clock=clock, retry_reads=RetryPolicy(sleep=sleep))
    with pytest.raises(ApiError):
        asyncio.run(client.get_json("projects/flaky"))
    assert len(fake.calls) == 5 and len(slept) == 4
    with pytest.raises(ApiError):
        asyncio.run(client.graphql("query flaky {}", {}))
    assert len(fake.calls) == 10
    for method in ("POST", "PUT", "DELETE"):
        with pytest.raises(ApiError):
            asyncio.run(client.request(method, "projects/flaky", json_body={"body": "x"}))
    with pytest.raises(ApiError):
        asyncio.run(client.graphql_mutation("mutation flaky {}", {}))
    assert len(fake.calls) == 14


def test_not_found_is_never_retried(clock: Clock) -> None:
    fake = FakeGlab([Route("gone", {"message": "404 Not found"}, status=404)])
    client = GitLabClient("gitlab.example.com", runner=fake, clock=clock, retry_reads=RetryPolicy())
    with pytest.raises(NotFound):
        asyncio.run(client.get_json("projects/gone"))
    assert len(fake.calls) == 1


def test_timeout_is_a_transport_error(clock: Clock) -> None:
    async def slow(args: object, timeout: float, stdin: bytes | None = None) -> ProcessResult:
        raise TimeoutError

    client = GitLabClient("gitlab.example.com", runner=slow, clock=clock, timeout=3)
    with pytest.raises(TransportError, match="3 s"):
        asyncio.run(client.get_json("version"))
