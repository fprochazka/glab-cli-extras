"""Blocking GitLab calls for the write commands of glab-discussion and the extras of glab-pipeline.

These commands are short sequences of single calls, so a blocking interface keeps their code readable. It is a thin
entry into the same transport as `GitLabClient`: `glab api -i`, the status read from the status line, request bodies
as JSON on stdin. The same retry rule applies: only GETs and GraphQL queries are retried, never a write.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from glab_cli_extras.gitlab.glab import (
    DEFAULT_TIMEOUT,
    HttpResponse,
    ProcessResult,
    build_api_args,
    error_message,
    json_bytes,
    parse_include_output,
    run_glab_sync,
    stderr_message,
)

SyncGlabRunner = Callable[[Sequence[str], float, "bytes | None"], ProcessResult]

ATTEMPTS = 5
BASE_DELAY = 1.0


def _default_runner(args: Sequence[str], timeout: float, stdin: bytes | None) -> ProcessResult:
    return run_glab_sync(args, timeout, stdin)


RUNNER: SyncGlabRunner = _default_runner
"""Replaced by the tests with a fake glab."""
SLEEP: Callable[[float], None] = time.sleep


class GlabApiError(Exception):
    def __init__(self, message: str, stderr: str = "", returncode: int = 1, status: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode
        self.status = status

    @property
    def http_status(self) -> int | None:
        """The HTTP status of the failed request, or None when glab got no response."""
        return self.status


def _call(args: list[str], stdin: bytes | None) -> HttpResponse:
    result = RUNNER(args, DEFAULT_TIMEOUT, stdin)
    response = parse_include_output(result.stdout)
    if response is None:
        message = stderr_message(result)
        raise GlabApiError(f"glab api failed: {message}", stderr=message, returncode=result.returncode)
    if response.status >= 400:
        message = error_message(response)
        raise GlabApiError(
            f"glab api failed: {message} (HTTP {response.status})",
            stderr=f"{message} (HTTP {response.status})",
            returncode=result.returncode or 1,
            status=response.status,
        )
    return response


def _retryable(exc: GlabApiError) -> bool:
    return exc.status is None or exc.status == 429 or exc.status >= 500


def _with_retries(args: list[str], stdin: bytes | None, retry: bool) -> HttpResponse:
    delay = BASE_DELAY
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _call(args, stdin)
        except GlabApiError as exc:
            if not retry or not _retryable(exc) or attempt == ATTEMPTS:
                raise
        SLEEP(delay + random.uniform(0, delay / 2))
        delay *= 2
    raise AssertionError("unreachable")


def _parse(response: HttpResponse, endpoint: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GlabApiError(f"GitLab returned invalid JSON for {endpoint}: {exc}", status=response.status) from exc


def glab_api(
    endpoint: str,
    *,
    hostname: str,
    method: str | None = None,
    query: Mapping[str, str] | None = None,
    json_body: Any = None,
    paginate: bool = False,
) -> Any:
    """Call the REST API and return the parsed body, or None for an empty one.

    `query` goes into the URL. A body is sent as JSON on stdin. Only a GET is retried. With `paginate`, the pages are
    followed through `X-Next-Page` and their items returned as one list.
    """
    verb = (method or "GET").upper()
    stdin = json_bytes(json_body) if json_body is not None else None
    if query:
        endpoint = f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(dict(query))}"
    if not paginate:
        args = build_api_args(hostname, endpoint, method=verb, json_body=stdin is not None)
        return _parse(_with_retries(args, stdin, retry=verb == "GET"), endpoint)
    items: list[Any] = []
    page = 1
    while True:
        paged = f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode({'per_page': 100, 'page': page})}"
        response = _with_retries(build_api_args(hostname, paged, method="GET"), None, retry=True)
        batch = _parse(response, endpoint)
        if isinstance(batch, list):
            items.extend(batch)
        else:
            items.append(batch)
        next_page = (response.header("x-next-page") or "").strip()
        if not next_page.isdigit():
            return items
        page = int(next_page)


def _graphql(query: str, variables: Mapping[str, Any], hostname: str, retry: bool) -> dict[str, Any]:
    args = build_api_args(hostname, "graphql", method="POST", json_body=True)
    response = _with_retries(args, json_bytes({"query": query, "variables": dict(variables)}), retry)
    payload = _parse(response, "graphql")
    if not isinstance(payload, dict):
        raise GlabApiError("GitLab returned an unexpected GraphQL response", status=response.status)
    errors = payload.get("errors")
    if errors:
        messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
        raise GlabApiError(f"glab api failed: GraphQL error: {messages}", stderr=messages, status=response.status)
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def glab_graphql_query(query: str, variables: Mapping[str, Any], *, hostname: str) -> dict[str, Any]:
    """Run a GraphQL query and return its `data`. Retried like a GET. Top-level `errors` raise."""
    return _graphql(query, variables, hostname, retry=True)


def glab_graphql_mutation(query: str, variables: Mapping[str, Any], *, hostname: str) -> dict[str, Any]:
    """Run a GraphQL mutation once and return its `data`. Top-level `errors` raise; the mutation's own `errors`
    field stays in `data` for the caller to judge."""
    return _graphql(query, variables, hostname, retry=False)
