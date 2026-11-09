"""Per-host request budget, paced on GitLab's `RateLimit-*` response headers.

GitLab reports `RateLimit-Remaining` and `RateLimit-Reset` for the real throttle period, while `RateLimit-Limit` is
normalized to one minute. The budget therefore paces on Remaining over the time left until Reset, and uses Limit only
as a per-minute ceiling. The daemon takes `share` of that allowance and leaves the rest to the user's other tools.

A host that sends no rate-limit headers (the throttle is opt-in on self-managed instances) gets a fixed cap.

Time is passed in by the caller (wall-clock seconds, since `RateLimit-Reset` is a Unix timestamp), which keeps the
class deterministic under test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SHARE = 0.5
DEFAULT_FIXED_PER_MINUTE = 60.0
DEFAULT_BURST = 10.0
NOT_MODIFIED_COST = 0.1
"""An ETag 304 was free in live tests, but that is undocumented, so it still costs a little."""
MIN_BACKOFF = 5.0
MAX_BACKOFF = 300.0


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


@dataclass
class HostBudget:
    share: float = DEFAULT_SHARE
    fixed_per_minute: float = DEFAULT_FIXED_PER_MINUTE
    burst: float = DEFAULT_BURST
    tokens: float = DEFAULT_BURST
    last_refill: float | None = None
    remaining: int | None = None
    reset_at: float | None = None
    limit_per_minute: int | None = None
    blocked_until: float = 0.0
    backoff: float = MIN_BACKOFF
    rate_limited_count: int = 0
    _saw_headers: bool = field(default=False, repr=False)

    def rate(self, now: float) -> float:
        """Requests per second the daemon may spend on this host right now."""
        if self.remaining is not None and self.reset_at is not None and self.reset_at > now:
            rate = self.remaining * self.share / max(1.0, self.reset_at - now)
            if self.limit_per_minute:
                rate = min(rate, self.limit_per_minute * self.share / 60.0)
            return max(rate, 0.01)
        if self.limit_per_minute:
            return self.limit_per_minute * self.share / 60.0
        return self.fixed_per_minute / 60.0

    def _refill(self, now: float) -> None:
        if self.last_refill is None:
            self.last_refill = now
            return
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate(now))
        self.last_refill = now

    def can_spend(self, cost: float, now: float) -> bool:
        self._refill(now)
        return now >= self.blocked_until and self.tokens >= min(cost, self.burst)

    def wait_time(self, cost: float, now: float) -> float:
        """Seconds until `can_spend(cost)` could become true."""
        self._refill(now)
        blocked = max(0.0, self.blocked_until - now)
        missing = max(0.0, min(cost, self.burst) - self.tokens)
        return max(blocked, missing / self.rate(now))

    def charge(self, cost: float, now: float) -> None:
        self._refill(now)
        self.tokens -= cost

    def observe(self, headers: Mapping[str, str], now: float) -> None:
        """Update from response headers (lowercase names). A response without rate-limit headers changes nothing."""
        remaining = _int_header(headers, "ratelimit-remaining")
        reset = _int_header(headers, "ratelimit-reset")
        limit = _int_header(headers, "ratelimit-limit")
        if remaining is None and reset is None and limit is None:
            return
        self._saw_headers = True
        if remaining is not None:
            self.remaining = remaining
        if reset is not None:
            self.reset_at = float(reset)
        if limit is not None:
            self.limit_per_minute = limit

    def on_success(self) -> None:
        self.backoff = MIN_BACKOFF

    def on_rate_limited(self, retry_after: float | None, now: float) -> None:
        """Block the whole host: honor Retry-After, and back off exponentially on repeated 429s."""
        wait = max(retry_after or 0.0, self.backoff)
        self.blocked_until = max(self.blocked_until, now + wait)
        self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        self.rate_limited_count += 1
        self.tokens = min(self.tokens, 0.0)

    def stretch_factor(self, demand_per_second: float, now: float) -> float:
        """How much every poll interval on this host must grow so the total demand fits the allowance."""
        rate = self.rate(now)
        if demand_per_second <= rate:
            return 1.0
        return demand_per_second / rate

    @property
    def premium_level(self) -> bool:
        """Whether the per-minute limit looks like a paid gitlab.com tier or a generous self-managed throttle."""
        return (self.limit_per_minute or 0) >= 400

    def describe(self, now: float) -> dict[str, Any]:
        return {
            "rate_per_minute": round(self.rate(now) * 60, 1),
            "tokens": round(self.tokens, 2),
            "remaining": self.remaining,
            "reset_in_s": round(self.reset_at - now) if self.reset_at else None,
            "limit_per_minute": self.limit_per_minute,
            "blocked_for_s": round(max(0.0, self.blocked_until - now), 1),
            "rate_limited_count": self.rate_limited_count,
            "headers_seen": self._saw_headers,
        }
