"""Retries for reads, for the commands that run without the daemon.

Only reads are ever retried: REST GETs and GraphQL queries. A write that failed with a timeout or a 5xx may still have
been applied by GitLab, and sending it again would post a comment, a draft or a review twice. 401, 403 and 404 are
answers, not failures, and are never retried. The daemon does not use this: its engine has its own backoff.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from glab_cli_extras.gitlab.client import ApiError, RateLimited, TransportError

T = TypeVar("T")

DEFAULT_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 1.0


def retry_wait(exc: BaseException, delay: float) -> float | None:
    """Seconds to wait before the next attempt, or None when `exc` must not be retried."""
    if isinstance(exc, RateLimited):
        return exc.retry_after if exc.retry_after is not None else delay
    if isinstance(exc, TransportError):
        return delay
    if isinstance(exc, ApiError) and exc.status is not None and exc.status >= 500:
        return delay
    return None


@dataclass
class RetryPolicy:
    attempts: int = DEFAULT_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    async def run(self, call: Callable[[], Awaitable[T]]) -> T:
        delay = self.base_delay
        for attempt in range(1, self.attempts + 1):
            try:
                return await call()
            except ApiError as exc:
                wait = retry_wait(exc, delay)
                if wait is None or attempt == self.attempts:
                    raise
            await self.sleep(wait + random.uniform(0, delay / 2))
            delay *= 2
        raise AssertionError("unreachable")
