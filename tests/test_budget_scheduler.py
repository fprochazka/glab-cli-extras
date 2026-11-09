from __future__ import annotations

import pytest

from glab_cli_extras.daemon.scheduler import Scheduler
from glab_cli_extras.gitlab.budget import HostBudget

NOW = 1_000_000.0


def test_paces_on_remaining_over_time_to_reset_capped_by_the_minute_limit() -> None:
    budget = HostBudget()
    budget.observe(
        {"ratelimit-remaining": "600", "ratelimit-reset": str(int(NOW + 600)), "ratelimit-limit": "480"}, NOW
    )
    # Half of 600 requests over 600 s is 0.5/s, below the cap of 480 * 0.5 / 60 = 4/s.
    assert budget.rate(NOW) == pytest.approx(0.5)
    budget.observe({"ratelimit-remaining": "14000", "ratelimit-reset": str(int(NOW + 600))}, NOW)
    assert budget.rate(NOW) == pytest.approx(4.0)


def test_fixed_cap_without_headers() -> None:
    budget = HostBudget(fixed_per_minute=60)
    assert budget.rate(NOW) == pytest.approx(1.0)
    budget.observe({"content-type": "application/json"}, NOW)
    assert budget.remaining is None


def test_token_bucket_blocks_and_refills() -> None:
    budget = HostBudget(fixed_per_minute=60, burst=2, tokens=2)
    assert budget.can_spend(1, NOW)
    budget.charge(1, NOW)
    budget.charge(1, NOW)
    assert not budget.can_spend(1, NOW)
    assert budget.wait_time(1, NOW) == pytest.approx(1.0)
    assert budget.can_spend(1, NOW + 1.0)


def test_429_blocks_the_host_and_backs_off_exponentially() -> None:
    budget = HostBudget()
    budget.on_rate_limited(30, NOW)
    assert budget.blocked_until == NOW + 30
    assert not budget.can_spend(0.1, NOW + 29)
    budget.on_rate_limited(None, NOW + 31)
    assert budget.blocked_until == NOW + 31 + 10
    budget.on_success()
    assert budget.backoff == 5


def test_stretch_factor_spreads_shortage_evenly() -> None:
    budget = HostBudget(fixed_per_minute=60)
    assert budget.stretch_factor(0.5, NOW) == 1.0
    assert budget.stretch_factor(3.0, NOW) == pytest.approx(3.0)


def test_scheduler_round_robin_and_urgent_first() -> None:
    scheduler = Scheduler()
    scheduler.schedule("busy-pipeline", 0)
    scheduler.schedule("quiet-mr", 5)
    scheduler.mark_served("busy-pipeline", 1)
    scheduler.schedule("busy-pipeline", 2)
    # Both due: the one served longest ago (never) goes first, even though it became due later.
    assert scheduler.ready(10) == ["quiet-mr", "busy-pipeline"]
    scheduler.schedule("terminal-probe", 9, urgent=True)
    assert scheduler.ready(10)[0] == "terminal-probe"
    assert scheduler.ready(1) == []
    assert scheduler.next_due() == 2


def test_scheduler_forget_and_remove() -> None:
    scheduler = Scheduler()
    scheduler.schedule("a", 1)
    assert "a" in scheduler
    scheduler.remove("a")
    assert "a" not in scheduler and scheduler.next_due() is None
