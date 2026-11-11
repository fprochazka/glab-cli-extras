"""GitLab REST and GraphQL access for one host, on top of `glab api -i`.

Every request charges the host budget and feeds its rate-limit headers back into it. A 429 blocks the whole host
for its Retry-After. Errors are mapped to a small exception hierarchy the pollers and the daemon understand.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote, urlencode

from glab_cli_extras.gitlab.budget import NOT_MODIFIED_COST, HostBudget
from glab_cli_extras.gitlab.capabilities import PROBE_QUERY, Capabilities, CapabilityStore, parse_probe
from glab_cli_extras.gitlab.glab import (
    DEFAULT_TIMEOUT,
    GlabRunner,
    HttpResponse,
    build_api_args,
    error_message,
    json_bytes,
    parse_include_output,
    run_glab,
    stderr_message,
)

if TYPE_CHECKING:
    from glab_cli_extras.gitlab.retry import RetryPolicy

MAX_CONCURRENT_REQUESTS = 4
T = TypeVar("T")
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


class GitLabClient:
    def __init__(
        self,
        host: str,
        *,
        runner: GlabRunner = run_glab,
        budget: HostBudget | None = None,
        clock: Callable[[], float] = time.time,
        timeout: float = DEFAULT_TIMEOUT,
        max_concurrency: int = MAX_CONCURRENT_REQUESTS,
        retry_reads: RetryPolicy | None = None,
        capabilities: CapabilityStore | None = None,
    ) -> None:
        """`retry_reads` retries GETs and GraphQL queries, never a write: a write that timed out may still have been
        applied by GitLab, and sending it again would post a comment or a review twice.

        `capabilities` keeps what the host's schema offers; by default in memory only, which is what the daemon needs.
        """
        self.host = host
        self.capability_store = capabilities if capabilities is not None else CapabilityStore()
        self.budget = budget if budget is not None else HostBudget()
        self.stats = RequestStats()
        self._runner = runner
        self._clock = clock
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._probe_lock = asyncio.Lock()
        self._retry = retry_reads
        self._version: tuple[int, int] | None = None
        self._version_checked = False

    async def _read(self, call: Callable[[], Awaitable[T]]) -> T:
        if self._retry is None:
            return await call()
        return await self._retry.run(call)

    async def _request(self, args: list[str], kind: str, stdin: bytes | None = None) -> HttpResponse:
        async with self._semaphore:
            self.stats.requests += 1
            self.stats.by_kind[kind] += 1
            try:
                result = await self._runner(args, self._timeout, stdin)
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
            raise NotFound(error_message(response), 404)
        if response.status in (401, 403):
            raise Unauthorized(
                f"{error_message(response)} (HTTP {response.status}); check `glab auth status --hostname {self.host}`",
                response.status,
            )
        if response.status >= 400:
            raise ApiError(error_message(response), response.status)
        self.budget.on_success()
        return response

    async def get(
        self, path: str, params: Mapping[str, str | int] | None = None, *, kind: str = "rest"
    ) -> HttpResponse:
        endpoint = _with_params(path, params)
        return await self._read(lambda: self._request(build_api_args(self.host, endpoint), kind))

    async def get_json(self, path: str, params: Mapping[str, str | int] | None = None, *, kind: str = "rest") -> Any:
        response = await self.get(path, params, kind=kind)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"GitLab returned invalid JSON for {path}: {exc}", response.status) from exc

    async def get_pages(
        self, path: str, params: Mapping[str, str | int] | None = None, *, kind: str = "rest", max_pages: int = 100
    ) -> list[Any]:
        """Every item of a paginated list, following GitLab's `X-Next-Page` header."""
        items: list[Any] = []
        page = 1
        for _ in range(max_pages):
            response = await self.get(path, {"per_page": 100, **(params or {}), "page": page}, kind=kind)
            try:
                batch = response.json()
            except ValueError as exc:
                raise ApiError(f"GitLab returned invalid JSON for {path}: {exc}", response.status) from exc
            if not isinstance(batch, list):
                raise ApiError(f"GitLab returned no list for {path}", response.status)
            items.extend(batch)
            next_page = (response.header("x-next-page") or "").strip()
            if not next_page.isdigit():
                break
            page = int(next_page)
        return items

    async def request(self, method: str, path: str, *, json_body: Any = None, kind: str = "write") -> Any:
        """A REST call with an optional JSON body. Only a GET is retried. Returns the parsed body, None when empty."""
        args = build_api_args(self.host, path, method=method, json_body=json_body is not None)
        stdin = json_bytes(json_body) if json_body is not None else None
        if method.upper() == "GET":
            response = await self._read(lambda: self._request(args, kind, stdin))
        else:
            response = await self._request(args, kind, stdin)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"GitLab returned invalid JSON for {path}: {exc}", response.status) from exc

    async def graphql_mutation(self, query: str, variables: Mapping[str, Any], *, kind: str = "mutation") -> Any:
        """Run a GraphQL mutation once, never retried. Top-level errors raise; the mutation's own `errors` field is
        part of the returned `data`, for the caller to judge."""
        args = build_api_args(self.host, "graphql", method="POST", json_body=True)
        response = await self._request(args, kind, json_bytes({"query": query, "variables": dict(variables)}))
        return _graphql_data(response)

    async def graphql(
        self,
        query: str,
        variables: Mapping[str, Any],
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
        args = build_api_args(self.host, "graphql", method="POST", headers=headers, json_body=True)
        body = json_bytes({"query": query, "variables": dict(variables)})
        response = await self._read(lambda: self._request(args, kind + ("-etag" if etag_resource else ""), body))
        etag = response.header("etag")
        if response.status == 304:
            return GraphQLResult(True, None, etag or if_none_match)
        return GraphQLResult(False, _graphql_data(response), etag)

    async def capabilities(self) -> Capabilities | None:
        """What this host's schema offers, from the store or from one probe. None when the probe got no answer.

        The probe is never retried: on a failure the caller asks for every field and handles the rejection.
        """
        # The daemon's first polls of several MRs on one host run at once; one probe answers them all.
        async with self._probe_lock:
            return await self._load_or_probe()

    async def _load_or_probe(self) -> Capabilities | None:
        now = self._clock()
        store = self.capability_store
        found = store.load(self.host, now)
        if found is not None or not store.may_probe(self.host, now):
            return found
        args = build_api_args(self.host, "graphql", method="POST", json_body=True)
        try:
            response = await self._request(args, "capabilities", json_bytes({"query": PROBE_QUERY, "variables": {}}))
            data = _graphql_data(response)
        except ApiError as exc:
            if type(exc) is not ApiError or exc.status is None or exc.status >= 500:
                # No answer: a network failure, a 5xx, a 429, a 401 or 403. Try again later, never write it down.
                store.probe_failed(self.host, now)
                return None
            # An answer: GitLab older than 15.6 has no `metadata.enterprise`. Every field is assumed to exist.
            data = None
        found = parse_probe(data, now)
        store.save(self.host, found)
        return found

    def record_missing_mr_field(self, name: str) -> None:
        """GitLab rejected `MergeRequest.<name>`: leave it out of the next queries until the capabilities expire."""
        now = self._clock()
        current = self.capability_store.load(self.host, now) or Capabilities(now)
        self.capability_store.save(self.host, current.without_mr_field(name))

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


def _with_params(path: str, params: Mapping[str, str | int] | None) -> str:
    if not params:
        return path
    return path + ("&" if "?" in path else "?") + urlencode(list(params.items()), safe="[]")


def _graphql_data(response: HttpResponse) -> Any:
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
    return payload.get("data")
