"""The daemon transport: NDJSON, one request and one response per connection.

POSIX uses a Unix socket in the private state dir. Windows uses loopback TCP on a random port; the port and a random
token are in a file only the user can read, and every request must carry the token, because any local user can
connect to a loopback port. Setting `GLAB_CLI_EXTRAS_TRANSPORT=tcp` selects TCP on POSIX too, which is how the
Windows path is tested on Linux.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glab_cli_extras.paths import daemon_state_dir, ensure_private_dir

PROTOCOL_VERSION = 1
MAX_LINE = 4 * 1024 * 1024
TRANSPORT_ENV = "GLAB_CLI_EXTRAS_TRANSPORT"

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class DaemonUnavailable(ConnectionError):
    """No daemon answers on the endpoint."""


def use_tcp() -> bool:
    return os.name == "nt" or os.environ.get(TRANSPORT_ENV) == "tcp" or not hasattr(socket, "AF_UNIX")


@dataclass(frozen=True)
class DaemonFiles:
    state_dir: Path

    @classmethod
    def default(cls) -> DaemonFiles:
        return cls(daemon_state_dir())

    def _name(self, suffix: str) -> Path:
        return self.state_dir / f"daemon-v{PROTOCOL_VERSION}.{suffix}"

    @property
    def lock(self) -> Path:
        return self._name("lock")

    @property
    def spawn_lock(self) -> Path:
        return self.state_dir / f"spawn-v{PROTOCOL_VERSION}.lock"

    @property
    def socket(self) -> Path:
        return self._name("sock")

    @property
    def endpoint(self) -> Path:
        return self._name("json")

    @property
    def log(self) -> Path:
        return self._name("log")

    def describe_endpoint(self) -> str:
        if use_tcp():
            return str(self.endpoint)
        return str(self.socket)


def _write_private(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


async def _read_line(reader: asyncio.StreamReader) -> bytes:
    return await reader.readuntil(b"\n")


async def start_server(files: DaemonFiles, handler: Handler) -> tuple[asyncio.AbstractServer, Callable[[], None]]:
    """Start listening. Return the server and a cleanup function that removes the endpoint file."""
    ensure_private_dir(files.state_dir)
    token = secrets.token_hex(16) if use_tcp() else None

    async def on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(_read_line(reader), 10)
            request = json.loads(line)
            if not isinstance(request, dict):
                response: dict[str, Any] = {"ok": False, "error": "request must be a JSON object"}
            elif token is not None and not hmac.compare_digest(str(request.get("token", "")), token):
                response = {"ok": False, "error": "bad token"}
            else:
                request.pop("token", None)
                response = await handler(request)
            writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    if token is None:
        with contextlib.suppress(FileNotFoundError):
            files.socket.unlink()
        server = await asyncio.start_unix_server(on_connection, path=str(files.socket), limit=MAX_LINE)
        os.chmod(files.socket, 0o600)

        def cleanup() -> None:
            with contextlib.suppress(OSError):
                files.socket.unlink()

        return server, cleanup

    server = await asyncio.start_server(on_connection, host="127.0.0.1", port=0, limit=MAX_LINE)
    port = server.sockets[0].getsockname()[1]
    _write_private(files.endpoint, json.dumps({"port": port, "token": token, "pid": os.getpid()}))

    def cleanup_tcp() -> None:
        with contextlib.suppress(OSError):
            files.endpoint.unlink()

    return server, cleanup_tcp


def _connect(files: DaemonFiles, timeout: float) -> tuple[socket.socket, str | None]:
    if use_tcp():
        try:
            endpoint = json.loads(files.endpoint.read_text(encoding="utf-8"))
            port, token = int(endpoint["port"]), str(endpoint["token"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DaemonUnavailable(f"no daemon endpoint in {files.endpoint}") from exc
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=timeout), token
        except OSError as exc:
            raise DaemonUnavailable(str(exc)) from exc
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(files.socket))
    except OSError as exc:
        sock.close()
        raise DaemonUnavailable(str(exc)) from exc
    return sock, None


def request(files: DaemonFiles, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    """Send one request and return the response. Raises DaemonUnavailable when nothing answers."""
    sock, token = _connect(files, timeout)
    with sock:
        body = dict(payload)
        if token is not None:
            body["token"] = token
        try:
            sock.sendall(json.dumps(body, separators=(",", ":")).encode() + b"\n")
            chunks = []
            total = 0
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if chunk.endswith(b"\n") or total > MAX_LINE:
                    break
        except OSError as exc:
            raise DaemonUnavailable(str(exc)) from exc
    raw = b"".join(chunks)
    if not raw.strip():
        raise DaemonUnavailable("the daemon closed the connection without answering")
    response = json.loads(raw)
    if not isinstance(response, dict):
        raise DaemonUnavailable("the daemon sent an unexpected response")
    return response
