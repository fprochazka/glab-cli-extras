"""The `claude-message` stream: events go to the Claude Code session that started the watcher.

cc-blackbox does the delivery: it batches the events that arrive within 3 s into one message, caps a message at 64 KB,
keeps below the session's rate limit, ends the watch when the session is gone, and moves the events to stdout when
messaging stops matching the SDK. This module holds what is specific to GitLab: each message is a `<glab-watcher>`
block for one resource, sent as `glab-watch:<pid>` under the name `glab-watch mr group/project!123`. Only the message
with `watch_started` names the resource dir and the changelog.

Delivery is best effort: the receiver may rate-limit a message, or hold it until its user approves it. The changelog
always has every event.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from cc_blackbox import ActiveSession, EventEnvelope, MessageEventSink, Priority, RateLimitedSender

from glab_cli_extras.events import WATCH_ENDED, WATCH_STARTED, Event
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.watch.sinks import StatusOutput

ENVELOPE_TAG = "glab-watcher"


def sender_name(ref: ResourceRef) -> str:
    """The name Claude Code shows for the sender: `glab-watch mr group/project!123`."""
    short = ref.short.split("/", 1)[1] if "/" in ref.short else ref.short
    return f"glab-watch {ref.kind} {short}"


def _event_payload(event: Event) -> Mapping[str, Any]:
    """An event as a message line: the envelope already names the resource, and the time is of no use there."""
    payload: dict[str, object] = {"kind": event.kind, "seq": event.seq, "data": event.data}
    if event.artifacts:
        payload["artifacts"] = list(event.artifacts)
    return payload


def _without_announced_dir(event: Event, directory: Path) -> Event:
    """`watch_started` as the announcing message shows it: the head names the dir, so the summary does not repeat
    it, and the threads dir is given relative to it. stdout and the changelog keep the full event."""
    summary = event.data.get("summary")
    if event.kind != WATCH_STARTED or not isinstance(summary, dict):
        return event
    summary = {k: v for k, v in summary.items() if not (k == "dir" and v == str(directory))}
    threads = summary.get("threads")
    if isinstance(threads, dict) and isinstance(threads.get("dir"), str):
        path = Path(threads["dir"])
        if path.is_relative_to(directory):
            summary["threads"] = {**threads, "dir": str(path.relative_to(directory))}
    return replace(event, data={**event.data, "summary": summary})


def message_envelope(ref: ResourceRef, changelog: Path) -> EventEnvelope[Event]:
    """`<glab-watcher resource="…" seq="…">`. The message with `watch_started` also names the resource dir and the
    changelog: repeating two long paths on every message was most of its text. Above 64 KB the body becomes a count
    per kind that points at the changelog."""
    return EventEnvelope(
        ENVELOPE_TAG,
        {"resource": ref.key},
        payload=_event_payload,
        announce_on=WATCH_STARTED,
        announce_attributes={"dir": str(changelog.parent), "changelog": str(changelog)},
        announced=lambda event: _without_announced_dir(event, changelog.parent),
        record=str(changelog),
    )


def claude_message_sink(
    session: ActiveSession,
    ref: ResourceRef,
    changelog: Path,
    priority: Priority,
    status: StatusOutput,
    *,
    rate_limiter: RateLimitedSender | None = None,
    clock: Callable[[], float] = time.monotonic,
    session_alive: Callable[[], bool] | None = None,
) -> MessageEventSink[Event]:
    """The sink for one watched resource. `watch_ended` goes out at once, so the session learns the end without the
    3 s burst wait."""
    return MessageEventSink(
        session,
        message_envelope(ref, changelog),
        sender=f"glab-watch:{os.getpid()}",
        sender_name=sender_name(ref),
        status=status.line,
        priority=priority,
        flush_on={WATCH_ENDED},
        rate_limiter=rate_limiter,
        clock=clock,
        session_alive=session_alive,
    )
