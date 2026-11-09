"""GitLab REST and GraphQL access for one host, on top of `glab api -i`.

Every request charges the host budget and feeds its rate-limit headers back into it. A 429 blocks the whole host
for its Retry-After. Errors are mapped to a small exception hierarchy the pollers and the daemon understand.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from glab_cli_extras.gitlab.budget import NOT_MODIFIED_COST, HostBudget
from glab_cli_extras.gitlab.glab import (
    DEFAULT_TIMEOUT,
    GlabRunner,
    HttpResponse,
    build_api_args,
    parse_include_output,
    run_glab,
    stderr_message,
)

MAX_CONCURRENT_REQUESTS = 4
ETAG_RESOURCE_HEADER = "X-GITLAB-GRAPHQL-RESOURCE-ETAG"


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFound(ApiError):
    pass


class Unauthorized(ApiError):
    """401 or 403. glab owns authentication, so the fix is `glab auth login`, never a retry here."""


class RateLimited(ApiError):
    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message, 429)
        self.retry_after = retry_after


class TransportError(ApiError):
    """No HTTP response: network failure, timeout, or glab failed before sending the request."""


@dataclass(frozen=True, slots=True)
class GraphQLResult:
    not_modified: bool
    data: Any
    etag: str | None


@dataclass
class RequestStats:
    requests: int = 0
    not_modified: int = 0
    statuses: Counter[int] = field(default_factory=Counter)
    transport_errors: int = 0
    by_kind: Counter[str] = field(default_factory=Counter)

    def describe(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "not_modified": self.not_modified,
            "statuses": {str(k): v for k, v in sorted(self.statuses.items())},
            "transport_errors": self.transport_errors,
            "by_kind": dict(sorted(self.by_kind.items())),
        }


def encode_project(project: str) -> str:
    return quote(project, safe="")


def _error_message(response: HttpResponse) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "error", "error_description"):
            if key in payload:
                return str(payload[key])
    return f"HTTP {response.status} {response.reason}".strip()


class GitLabClient:
    def __init__(
        self,
        host: str,
        *,
        runner: GlabRunner = run_glab,
        budget: HostBudget | None = None,
        clock: Callable[[], float] = time.time,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.budget = budget if budget is not None else HostBudget()
        self.stats = RequestStats()
        self._runner = runner
        self._clock = clock
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._version: tuple[int, int] | None = None
        self._version_checked = False

    async def _request(self, args: list[str], kind: str) -> HttpResponse:
        async with self._semaphore:
            self.stats.requests += 1
            self.stats.by_kind[kind] += 1
            try:
                result = await self._runner(args, self._timeout)
            except TimeoutError as exc:
                self.stats.transport_errors += 1
                self.budget.charge(1.0, self._clock())
                raise TransportError(f"glab did not finish within {self._timeout:.0f} s") from exc
        now = self._clock()
        response = parse_include_output(result.stdout)
        if response is None:
            self.stats.transport_errors += 1
            self.budget.charge(1.0, now)
            raise TransportError(stderr_message(result))
        self.stats.statuses[response.status] += 1
        self.budget.observe(response.headers, now)
        if response.status == 304:
            self.stats.not_modified += 1
            self.budget.charge(NOT_MODIFIED_COST, now)
            return response
        self.budget.charge(1.0, now)
        if response.status == 429:
            retry_after = response.header("retry-after")
            seconds = float(retry_after) if retry_after and retry_after.strip().isdigit() else None
            self.budget.on_rate_limited(seconds, now)
            raise RateLimited(f"GitLab rate limit hit on {self.host}", seconds)
        if response.status == 404:
            raise NotFound(_error_message(response), 404)
        if response.status in (401, 403):
            raise Unauthorized(
                f"{_error_message(response)} (HTTP {response.status}); check `glab auth status --hostname {self.host}`",
                response.status,
            )
        if response.status >= 400:
            raise ApiError(_error_message(response), response.status)
        self.budget.on_success()
        return response

    async def get(
        self, path: str, params: Mapping[str, str | int] | None = None, *, kind: str = "rest"
    ) -> HttpResponse:
        endpoint = path
        if params:
            endpoint += ("&" if "?" in path else "?") + urlencode(list(params.items()), safe="[]")
        return await self._request(build_api_args(self.host, endpoint), kind)

    async def get_json(self, path: str, params: Mapping[str, str | int] | None = None, *, kind: str = "rest") -> Any:
        response = await self.get(path, params, kind=kind)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"GitLab returned invalid JSON for {path}: {exc}", response.status) from exc

    async def graphql(
        self,
        query: str,
        variables: Mapping[str, str | int | bool],
        *,
        etag_resource: str | None = None,
        if_none_match: str | None = None,
        kind: str = "graphql",
    ) -> GraphQLResult:
        """Run a GraphQL query, optionally through GitLab's ETag cache.

        With `etag_resource` (for example `pipelines/id/123`) and `if_none_match`, an unchanged resource returns
        a 304 from GitLab's cache before the query runs, and the result has `not_modified=True` and no data.
        """
        headers: dict[str, str] = {}
        if etag_resource is not None:
            headers[ETAG_RESOURCE_HEADER] = f"/api/graphql:{etag_resource}"
            if if_none_match:
                headers["If-None-Match"] = if_none_match
        strings = {k: v for k, v in variables.items() if isinstance(v, str)}
        typed = {k: v for k, v in variables.items() if not isinstance(v, str)}
        args = build_api_args(
            self.host, "graphql", headers=headers, fields={"query": query, **strings}, typed_fields=typed
        )
        response = await self._request(args, kind + ("-etag" if etag_resource else ""))
        etag = response.header("etag")
        if response.status == 304:
            return GraphQLResult(True, None, etag or if_none_match)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError(f"GitLab returned invalid GraphQL JSON: {exc}", response.status) from exc
        if not isinstance(payload, dict):
            raise ApiError("GitLab returned an unexpected GraphQL response", response.status)
        errors = payload.get("errors")
        if errors:
            messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
            raise ApiError(f"GraphQL error: {messages}", response.status)
        return GraphQLResult(False, payload.get("data"), etag)

    async def version(self) -> tuple[int, int] | None:
        """The GitLab version (major, minor), fetched once per client. None when the server does not tell."""
        if self._version_checked:
            return self._version
        self._version_checked = True
        try:
            payload = await self.get_json("version", kind="version")
        except NotFound:
            return None
        raw = str(payload.get("version", "")) if isinstance(payload, dict) else ""
        parts = raw.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            self._version = (int(parts[0]), int(parts[1]))
        return self._version
