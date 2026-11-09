"""The `claude-message` stream: events go to the Claude Code session that started the watcher.

The watcher finds its session through the cc-blackbox SDK and sends user messages over the session's messaging
socket. Events received close together go out as one message: the first event of a burst waits up to 3 s for the
rest. Each message is a `<glab-cli-extras-watcher>` block, because the receiver shows the content to the model but
never the sender label. `seq` keeps every body unique, so the receiver's duplicate check never drops a real event.

Delivery is best effort: the receiver may rate-limit a message, or hold it until its user approves it. The changelog
always has every event.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from html import escape
from pathlib import Path

from cc_blackbox import (
    ActiveSession,
    ClaudeBlackboxError,
    MessageTooLarge,
    MessagingUnavailable,
    Priority,
    RateLimitedSender,
    SessionGone,
    UnsupportedProtocol,
    current_session,
)

from glab_cli_extras.events import Event
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.watch.sinks import StatusOutput, StdoutSink

BURST_WAIT = 3.0
MAX_CONTENT_BYTES = 64 * 1024
LIVENESS_INTERVAL = 30.0
FLUSH_TIMEOUT = 10.0


def sender_name(ref: ResourceRef) -> str:
    """The name Claude Code shows for the sender: `glab-watch mr group/project!123`."""
    short = ref.short.split("/", 1)[1] if "/" in ref.short else ref.short
    return f"glab-watch {ref.kind} {short}"


def _event_line(event: Event) -> str:
    payload: dict[str, object] = {"kind": event.kind, "seq": event.seq, "data": event.data}
    if event.artifacts:
        payload["artifacts"] = list(event.artifacts)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def format_message(ref: ResourceRef, events: list[Event], changelog: Path) -> str:
    """One message for a batch of events. Above 64 KB the body becomes a count per kind; the changelog has it all."""
    seqs = [e.seq for e in events]
    seq = str(seqs[0]) if min(seqs) == max(seqs) else f"{min(seqs)}-{max(seqs)}"
    head = f'<glab-cli-extras-watcher resource="{escape(ref.key)}" seq="{seq}" changelog="{escape(str(changelog))}">'
    tail = "</glab-cli-extras-watcher>"
    body = "\n".join(_event_line(e) for e in events)
    content = f"{head}\n{body}\n{tail}"
    if len(content.encode("utf-8")) <= MAX_CONTENT_BYTES:
        return content
    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    summary = json.dumps(
        {"truncated": True, "events": len(events), "kinds": counts, "read": str(changelog)}, separators=(",", ":")
    )
    return f"{head}\n{summary}\n{tail}"


class ClaudeMessageSink:
    name = "claude-message"

    def __init__(
        self,
        session: ActiveSession,
        ref: ResourceRef,
        changelog: Path,
        priority: Priority,
        status: StatusOutput,
        *,
        sender: RateLimitedSender | None = None,
        clock: Callable[[], float] = time.monotonic,
        session_alive: Callable[[], bool] | None = None,
    ) -> None:
        if session.messaging_socket is None:
            raise MessagingUnavailable("the session has messaging turned off")
        self.session = session
        self.ref = ref
        self.changelog = changelog
        self.priority = priority
        self.status = status
        self.envelope_name = sender_name(ref)
        self.sender = sender or RateLimitedSender(
            f"glab-watch:{os.getpid()}", sender_name=self.envelope_name, priority=priority
        )
        self._socket = session.messaging_socket
        self._clock = clock
        self._session_alive = session_alive or _session_still_current
        self._buffer: list[Event] = []
        self._deadline: float | None = None
        self._gone = False
        self._fallback: StdoutSink | None = None
        self._last_liveness = clock()

    def describe(self) -> str:
        who = self.session.name or self.session.address
        return (
            f"claude-message to session {who!r} via {self._socket} "
            f"(priority {self.priority}, sender {self.envelope_name!r})"
        )

    def emit(self, events: list[Event]) -> None:
        if not events:
            return
        if self._fallback is not None:
            self._fallback.emit(events)
            return
        self._buffer.extend(events)
        if self._deadline is None:
            self._deadline = self._clock() + BURST_WAIT
        if any(e.kind == "watch_ended" for e in events):
            self._send_buffer()

    def seconds_until_flush(self) -> float | None:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - self._clock())

    def tick(self) -> None:
        if self._deadline is not None and self._clock() >= self._deadline:
            self._send_buffer()

    def _send_buffer(self) -> None:
        events, self._buffer, self._deadline = self._buffer, [], None
        if not events or self._gone:
            return
        if self._fallback is not None:
            self._fallback.emit(events)
            return
        content = format_message(self.ref, events, self.changelog)
        try:
            self.sender.send(self._socket, content)
        except SessionGone:
            self._gone = True
        except (UnsupportedProtocol, MessagingUnavailable) as exc:
            # The session stopped taking messages from us, for example after a Claude Code update changed the
            # protocol. Keep the events flowing on stdout rather than dropping them.
            self.status.line(f"stream fallback: cannot message the session ({exc}); events now go to stdout")
            self._fallback = StdoutSink()
            self._fallback.emit(events)
        except (MessageTooLarge, ClaudeBlackboxError, OSError) as exc:
            self.status.line(f"could not message the session: {exc}; the events are in {self.changelog}")

    def alive(self) -> bool:
        if self._gone:
            return False
        now = self._clock()
        if now - self._last_liveness >= LIVENESS_INTERVAL:
            self._last_liveness = now
            if not self._session_alive():
                self._gone = True
        return not self._gone

    def close(self) -> None:
        self._send_buffer()
        deadline = self._clock() + FLUSH_TIMEOUT
        while not self._gone and self.sender.pending_count(self._socket) and self._clock() < deadline:
            time.sleep(min(1.0, self.sender.seconds_until_next_token(self._socket) + 0.05))
            try:
                self.sender.flush()
            except SessionGone:
                self._gone = True
            except (ClaudeBlackboxError, OSError) as exc:
                self.status.line(f"could not message the session: {exc}")
                return


def _session_still_current() -> bool:
    session = current_session()
    return session is not None and session.can_receive_messages


def find_session() -> ActiveSession | None:
    """The Claude Code session this process runs under, or None."""
    try:
        return current_session()
    except (ClaudeBlackboxError, OSError):
        return None
