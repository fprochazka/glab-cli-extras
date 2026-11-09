"""Daemon lifecycle: the daemon's own main loop, and the client side that finds or starts it.

There is one daemon per user and wire-protocol version. It holds an exclusive lock on `daemon-v<N>.lock` for its
whole life, and exits after `idle_timeout` seconds without subscriptions. Clients start it on demand; the spawn lock
makes concurrent clients start one daemon, not one each.

The daemon is shared by every watcher of the user, so it must not be a descendant of the watcher that started it:
tools that stop a process together with its whole tree (Claude Code's task stop does) would take it down too. A new
session is not enough, because the parent stays the same. On POSIX the spawned process therefore forks once more
and exits as soon as the daemon is ready, so the daemon is reparented to init or the nearest subreaper.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from glab_cli_extras import __version__
from glab_cli_extras.daemon.engine import DEFAULT_CLIENT_POLL_MS, DEFAULT_LEASE_TIMEOUT, Engine
from glab_cli_extras.daemon.server import handle
from glab_cli_extras.daemon.transport import DaemonFiles, DaemonUnavailable, request, start_server, use_tcp
from glab_cli_extras.paths import ensure_private_dir
from glab_cli_extras.store.locks import FileLock

log = logging.getLogger("glab_cli_extras.daemon")

DEFAULT_IDLE_TIMEOUT = 30.0
SPAWN_TIMEOUT = 10.0
MAX_LOG_BYTES = 5 * 1024 * 1024
IDLE_TIMEOUT_ENV = "GLAB_CLI_EXTRAS_IDLE_TIMEOUT"
LEASE_TIMEOUT_ENV = "GLAB_CLI_EXTRAS_LEASE_TIMEOUT"
CLIENT_POLL_MS_ENV = "GLAB_CLI_EXTRAS_CLIENT_POLL_MS"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


async def serve(
    files: DaemonFiles, engine: Engine, idle_timeout: float, on_ready: Callable[[], None] | None = None
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

    async def on_request(req: dict[str, Any]) -> dict[str, Any]:
        return await handle(engine, req, stop)

    server, cleanup = await start_server(files, on_request)
    log.info(
        "daemon %s started, pid %d, protocol endpoint %s, idle timeout %.0f s",
        __version__,
        os.getpid(),
        files.describe_endpoint(),
        idle_timeout,
    )
    if on_ready is not None:
        on_ready()
    engine_task = asyncio.create_task(engine.run(stop))
    try:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 1.0)
            if engine.idle_for(engine.clock()) >= idle_timeout:
                log.info("no subscriptions for %.0f s, exiting", idle_timeout)
                stop.set()
    finally:
        # Remove the endpoint first, so a client that connects from now on starts a new daemon.
        cleanup()
        server.close()
        await server.wait_closed()
        stop.set()
        await engine_task
        log.info("daemon stopped")


def run_daemon(argv: Sequence[str] = ()) -> int:
    """The daemon process. On POSIX it detaches first, unless `--foreground` is given (for debugging)."""
    if os.name != "nt" and "--foreground" not in argv:
        return _detach()
    return _serve_daemon(None)


def _detach() -> int:
    """Fork the daemon off this launcher process, wait until it is ready or gave up, then let the launcher exit."""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(write_end)
        with os.fdopen(read_end, "rb") as ready:
            # One byte once the daemon listens or knows another daemon holds the lock; EOF if it died first.
            ready.read(1)
        return 0
    os.close(read_end)
    code = 1
    try:
        code = _serve_daemon(write_end)
    finally:
        logging.shutdown()
        # Never fall back into the launcher's code path, whatever happened above.
        os._exit(code)


def _serve_daemon(ready_fd: int | None) -> int:
    """Take the lock or exit, then serve until idle or stopped. `ready_fd` gets one byte when the launcher may go."""

    def notify() -> None:
        nonlocal ready_fd
        if ready_fd is not None:
            with contextlib.suppress(OSError):
                os.write(ready_fd, b"1")
                os.close(ready_fd)
            ready_fd = None

    files = DaemonFiles.default()
    ensure_private_dir(files.state_dir)
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("GLAB_CLI_EXTRAS_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    lock = FileLock(files.lock)
    try:
        if not lock.try_acquire():
            log.info("another daemon holds %s, exiting", files.lock)
            return 0
        engine = Engine(
            lease_timeout=_env_float(LEASE_TIMEOUT_ENV, DEFAULT_LEASE_TIMEOUT),
            client_poll_ms=int(_env_float(CLIENT_POLL_MS_ENV, DEFAULT_CLIENT_POLL_MS)),
        )
        asyncio.run(serve(files, engine, _env_float(IDLE_TIMEOUT_ENV, DEFAULT_IDLE_TIMEOUT), notify))
    finally:
        notify()
        lock.release()
    return 0


def ping(files: DaemonFiles, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        response = request(files, {"op": "hello"}, timeout=timeout)
    except (DaemonUnavailable, ValueError):
        return None
    return response if response.get("ok") else None


def _rotate_log(files: DaemonFiles) -> None:
    with contextlib.suppress(OSError):
        if files.log.stat().st_size > MAX_LOG_BYTES:
            os.replace(files.log, files.log.with_suffix(".log.1"))


def spawn_daemon(files: DaemonFiles) -> subprocess.Popen[bytes]:
    """Start a detached daemon process with its output in the log file."""
    ensure_private_dir(files.state_dir)
    _rotate_log(files)
    with open(files.log, "ab") as log_file:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": log_file,
            "cwd": str(files.state_dir),
            "close_fds": True,
        }
        command = [sys.executable, "-m", "glab_cli_extras.daemon"]
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                # Leave the caller's job object, so killing the caller's job does not kill the daemon. A job that
                # forbids breakaway makes this fail; the daemon then shares the caller's fate.
                return subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            except OSError:
                return subprocess.Popen(command, creationflags=flags, **kwargs)
        # The launcher forks the daemon off and exits once it is ready; see the module docstring.
        return subprocess.Popen(command, start_new_session=True, **kwargs)


def ensure_daemon(files: DaemonFiles, timeout: float = SPAWN_TIMEOUT) -> dict[str, Any]:
    """Return the hello response of a running daemon, starting one first if needed."""
    hello = ping(files)
    if hello is not None:
        return hello
    with FileLock(files.spawn_lock) as spawn_lock:
        spawn_lock.acquire(timeout)
        deadline = time.monotonic() + timeout
        spawned: subprocess.Popen[bytes] | None = None
        while time.monotonic() < deadline:
            hello = ping(files, timeout=1.0)
            if hello is not None:
                return hello
            # A daemon that is exiting on idle releases its lock last; spawn again once it is gone.
            if (spawned is None or spawned.poll() is not None) and not daemon_running(files):
                # Nothing holds the daemon lock, so no daemon is alive: any endpoint file left is stale.
                for stale in (files.socket, files.endpoint):
                    with contextlib.suppress(OSError):
                        stale.unlink()
                spawned = spawn_daemon(files)
            time.sleep(0.05)
    raise DaemonUnavailable(f"the daemon did not start within {timeout:.0f} s; see {files.log}")


def daemon_running(files: DaemonFiles) -> bool:
    """Whether a daemon process holds the lock, answering or not."""
    probe = FileLock(files.lock)
    if probe.try_acquire():
        probe.release()
        return False
    return True


def stop_daemon(files: DaemonFiles, timeout: float = 10.0) -> bool:
    """Ask the daemon to stop and wait until it released its lock. Return whether one was running."""
    if not daemon_running(files):
        return False
    with contextlib.suppress(DaemonUnavailable, ValueError):
        request(files, {"op": "stop"}, timeout=2.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not daemon_running(files):
            return True
        time.sleep(0.05)
    raise DaemonUnavailable(f"the daemon did not stop within {timeout:.0f} s; see {files.log}")


def transport_name() -> str:
    return "tcp" if use_tcp() else "unix"
