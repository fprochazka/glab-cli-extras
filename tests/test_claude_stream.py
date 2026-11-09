"""The claude-message stream, against a real Unix socket that reads frames the way a Claude Code session does."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cc_blackbox import UnsupportedProtocol

from conftest import Clock
from glab_cli_extras.events import Event
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.watch import streams
from glab_cli_extras.watch.changelog import Changelog
from glab_cli_extras.watch.claude import MAX_CONTENT_BYTES, ClaudeMessageSink, format_message, sender_name
from glab_cli_extras.watch.sinks import StatusOutput, StdoutSink
from test_watcher import REF as WATCHED
from test_watcher import ScriptedBackend, daemon_event, run

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Claude Code messaging has no Windows transport")

REF = ResourceRef("mr", "gitlab.example.com", "group/project", 42)


class Receiver:
    """Accepts connections on a Unix socket and keeps every frame it reads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.frames: list[dict[str, Any]] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while chunk := conn.recv(65536):
                    data += chunk
                for line in data.splitlines():
                    if line.strip():
                        self.frames.append(json.loads(line))

    def close(self) -> None:
        self._server.close()
        self.path.unlink(missing_ok=True)


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    directory = Path(tempfile.mkdtemp(prefix="gce-sock-"))
    rx = Receiver(directory / "s.sock")
    yield rx
    rx.close()
    directory.rmdir()


def fake_session(socket_path: Path, scratchpad: Path | None = None) -> Any:
    return SimpleNamespace(
        name="fix-the-login-form",
        address="personal/fix-the-login-form",
        messaging_socket=socket_path,
        can_receive_messages=True,
        scratchpad_dir=scratchpad,
        compatibility=SimpleNamespace(compatible=True, reasons=()),
    )


def event(kind: str, seq: int, **data: Any) -> Event:
    return Event(kind, REF.key, seq, "2026-11-09T10:00:00.000Z", data)


def wait_frames(rx: Receiver, count: int) -> list[dict[str, Any]]:
    for _ in range(200):
        if len(rx.frames) >= count:
            break
        threading.Event().wait(0.01)
    return rx.frames


def test_a_burst_goes_out_as_one_message_after_three_seconds(receiver: Receiver, clock: Clock, tmp_path: Path) -> None:
    sink = ClaudeMessageSink(
        cast(Any, fake_session(receiver.path)), REF, tmp_path / "events.jsonl", "next", StatusOutput(), clock=clock
    )
    assert "priority next" in sink.describe() and "'glab-watch mr group/project!42'" in sink.describe()
    sink.emit([event("pushed", 3, to="b" * 40)])
    sink.emit([event("thread_created", 4, discussion_id="abc")])
    sink.tick()
    assert sink.seconds_until_flush() == 3.0
    assert receiver.frames == []
    clock.advance(3)
    sink.tick()
    frames = wait_frames(receiver, 1)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == "user" and frame["priority"] == "next"
    assert frame["from"] == f"glab-watch:{os.getpid()}"
    content = frame["message"]["content"]
    assert content.startswith('<cross-session-message from="did:glab-watch:')
    assert 'from-name="glab-watch mr group/project!42"' in content
    assert (
        f'<glab-cli-extras-watcher resource="{REF.key}" seq="3-4" changelog="{tmp_path / "events.jsonl"}">' in content
    )
    assert '{"kind":"thread_created","seq":4,"data":{"discussion_id":"abc"}}' in content


def test_watch_ended_is_sent_at_once_and_priority_is_passed(receiver: Receiver, clock: Clock, tmp_path: Path) -> None:
    sink = ClaudeMessageSink(
        cast(Any, fake_session(receiver.path)), REF, tmp_path / "e.jsonl", "later", StatusOutput(), clock=clock
    )
    sink.emit([event("state_changed", 9, to="merged"), event("watch_ended", 10, reason="merged")])
    frames = wait_frames(receiver, 1)
    assert frames[0]["priority"] == "later" and 'seq="9-10"' in frames[0]["message"]["content"]
    sink.close()


def test_a_gone_session_ends_the_watch(receiver: Receiver, clock: Clock, tmp_path: Path) -> None:
    sink = ClaudeMessageSink(
        cast(Any, fake_session(receiver.path)), REF, tmp_path / "e.jsonl", "next", StatusOutput(), clock=clock
    )
    receiver.close()
    sink.emit([event("pushed", 1)])
    clock.advance(3)
    sink.tick()
    assert not sink.alive()


def test_liveness_is_rechecked_every_30_seconds(receiver: Receiver, clock: Clock, tmp_path: Path) -> None:
    alive = [True]
    sink = ClaudeMessageSink(
        cast(Any, fake_session(receiver.path)),
        REF,
        tmp_path / "e.jsonl",
        "next",
        StatusOutput(),
        clock=clock,
        session_alive=lambda: alive[0],
    )
    alive[0] = False
    assert sink.alive()
    clock.advance(30)
    assert not sink.alive()


def test_oversized_batches_become_a_summary(tmp_path: Path) -> None:
    events = [event("thread_updated", i, body="x" * 2000) for i in range(1, 60)]
    content = format_message(REF, events, tmp_path / "events.jsonl")
    assert len(content.encode()) < MAX_CONTENT_BYTES
    assert '"truncated":true' in content and '"thread_updated":59' in content and 'seq="1-59"' in content


def test_sender_name_drops_the_host() -> None:
    assert sender_name(ResourceRef("branch", "gitlab.example.com", "group/project", branch="main")) == (
        "glab-watch branch group/project:main"
    )


def test_stream_selection(receiver: Receiver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status = StatusOutput()
    monkeypatch.setattr(streams, "find_session", lambda: None)
    plain = streams.open_stream("auto", REF, "next", None, tmp_path / "default", status)
    assert isinstance(plain.sink, StdoutSink)
    assert plain.changelog.dir.is_relative_to(tmp_path / "default")
    with pytest.raises(streams.StreamError, match="no Claude Code session found"):
        streams.open_stream("claude-message", REF, "next", None, tmp_path / "default", status)

    scratchpad = tmp_path / "scratchpad"
    monkeypatch.setattr(streams, "find_session", lambda: fake_session(receiver.path, scratchpad))
    chosen = streams.open_stream("auto", REF, "now", None, tmp_path / "default", status)
    assert isinstance(chosen.sink, ClaudeMessageSink) and chosen.sink.priority == "now"
    assert chosen.changelog.events_path == (
        scratchpad / "glab-watch" / "gitlab.example.com" / "group" / "project" / "mr-42" / "events.jsonl"
    )
    forced = streams.open_stream("stdout", REF, "next", tmp_path / "explicit", tmp_path / "default", status)
    assert isinstance(forced.sink, StdoutSink) and forced.changelog.dir.is_relative_to(tmp_path / "explicit")


def test_claude_messages_carry_the_watcher_numbering(tmp_path: Path) -> None:
    backend = ScriptedBackend(
        [[daemon_event("watch_started", 0, summary={}), daemon_event("watch_ended", 0, reason="finished")]]
    )
    changelog = Changelog(tmp_path, WATCHED)
    out = StringIO()
    run(backend, changelog, out)
    events = [Event.from_json(json.loads(line)) for line in out.getvalue().splitlines()]
    assert 'seq="1-2"' in format_message(WATCHED, events, changelog.events_path)


def test_an_incompatible_session_falls_back_to_stdout_with_a_reason(
    receiver: Receiver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = fake_session(receiver.path, tmp_path / "scratchpad")
    session.can_receive_messages = False
    reason = "messaging: the record speaks peer protocol 2, and this cc-blackbox version speaks 1."
    session.compatibility = SimpleNamespace(compatible=False, reasons=(reason,))
    monkeypatch.setattr(streams, "find_session", lambda: session)
    status = StatusOutput()
    chosen = streams.open_stream("auto", REF, "next", None, tmp_path / "default", status)
    assert isinstance(chosen.sink, StdoutSink)
    assert chosen.fallback is not None and "peer protocol 2" in chosen.fallback
    # The changelog still goes to the scratchpad: only messaging is incompatible.
    assert chosen.changelog.dir.is_relative_to(tmp_path / "scratchpad")
    with pytest.raises(streams.StreamError, match="peer protocol 2"):
        streams.open_stream("claude-message", REF, "next", None, tmp_path / "default", status)


class RefusingSender:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def send(self, target: object, content: str) -> None:
        self.calls += 1
        raise self.error

    def pending_count(self, target: object) -> int:
        return 0


def test_a_protocol_change_mid_run_moves_events_to_stdout(
    receiver: Receiver, clock: Clock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sender = RefusingSender(UnsupportedProtocol("The session speaks peer protocol 2."))
    sink = ClaudeMessageSink(
        cast(Any, fake_session(receiver.path)),
        REF,
        tmp_path / "e.jsonl",
        "next",
        StatusOutput(),
        clock=clock,
        sender=cast(Any, sender),
    )
    sink.emit([event("pushed", 1)])
    clock.advance(3)
    sink.tick()
    sink.emit([event("watch_ended", 2, reason="merged")])
    sink.close()
    out, err = capsys.readouterr()
    assert [json.loads(line)["seq"] for line in out.splitlines()] == [1, 2]
    assert "stream fallback: cannot message the session" in err
    assert sender.calls == 1 and sink.alive()
