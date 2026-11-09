from __future__ import annotations

import pytest

from glab_cli_extras.events import (
    KINDS_BY_RESOURCE,
    LIFECYCLE_KINDS,
    Event,
    FilterError,
    resolve_filter,
)


def test_default_is_everything_the_resource_tracks_but_the_threshold_kinds() -> None:
    expected = frozenset(KINDS_BY_RESOURCE["mr"]) - {"quiet", "pipeline_stuck"} | frozenset(LIFECYCLE_KINDS)
    assert resolve_filter("mr", None, None) == expected


def test_a_threshold_option_adds_its_kind_to_the_default_and_to_its_group() -> None:
    assert "quiet" in resolve_filter("mr", None, None, ["quiet"])
    assert "pipeline_stuck" in resolve_filter("pipeline", ["pipeline"], None, ["pipeline_stuck"])
    assert "quiet" not in resolve_filter("mr", ["status"], None)
    assert "quiet" in resolve_filter("mr", ["status"], None, ["quiet"])


def test_groups_expand_to_kinds_the_resource_produces() -> None:
    selected = resolve_filter("mr", ["status,comments"], None)
    assert "state_changed" in selected and "thread_created" in selected
    assert "job_finished" not in selected and "mr_opened" not in selected
    assert set(LIFECYCLE_KINDS) <= selected


def test_exclude_removes_a_group() -> None:
    selected = resolve_filter("mr", None, ["comments", "pipeline"])
    assert not {"thread_created", "head_pipeline_changed", "pipeline_finished"} & selected
    assert "pushed" in selected


def test_repeated_and_comma_separated_values_combine() -> None:
    assert resolve_filter("pipeline", ["job_finished", "finished"], None) == frozenset(
        {"job_finished", "finished", *LIFECYCLE_KINDS}
    )


@pytest.mark.parametrize(
    ("kind", "include", "exclude", "message"),
    [
        ("mr", ["stauts"], None, "unknown event kind or group 'stauts'"),
        ("pipeline", ["comments"], None, "no events for a pipeline watch"),
        ("pipeline", ["mr_opened"], None, "never produces 'mr_opened'"),
        ("mr", None, ["lifecycle"], "cannot be excluded"),
        ("branch", None, ["watch_started"], "cannot be excluded"),
        ("mr", ["quiet"], None, "only with --quiet-after"),
        ("pipeline", ["quiet"], None, "never produces 'quiet'"),
    ],
)
def test_mistakes_fail_at_startup(
    kind: str, include: list[str] | None, exclude: list[str] | None, message: str
) -> None:
    with pytest.raises(FilterError, match=message):
        resolve_filter(kind, include, exclude)


def test_event_json_round_trip() -> None:
    event = Event(
        "job_finished",
        "pipeline gitlab.example.com/group/project!5",
        3,
        "2026-11-09T10:00:00.000Z",
        {"job_id": 1},
        ("/tmp/log",),
    )
    assert Event.from_json(event.to_json()) == event
    assert "artifacts" not in Event("x", "r", 1, "t", {}).to_json()
