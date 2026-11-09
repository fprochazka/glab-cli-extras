"""Run `glab api -i` as a subprocess and parse its output.

glab owns authentication, hosts, the keyring, OAuth refresh, proxies and certificates. This module never sees a token:
it only builds the `glab api` argument list and reads back what glab prints.

With `-i`, glab prints the HTTP status line, the response headers, a blank line and the body. It exits 1 for every
non-2xx status, a 304 included, but still prints the status line and the headers. The status is therefore read from the
output, never from the exit code. Output without a status line means the request never got a response (DNS, TLS,
connection refused, unknown host); glab explains why on stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_TIMEOUT = 60.0


class GlabNotFoundError(RuntimeError):
    """The `glab` executable is not on PATH."""


class GlabTransportError(RuntimeError):
    """glab produced no HTTP response: the request failed before the server answered, or glab itself failed."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    reason: str
    headers: Mapping[str, str]
    """Header names are lowercase. A repeated header keeps its last value."""
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        if not self.body.strip():
            return None
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class GlabRunner(Protocol):
    """Run glab with the given arguments (without the `glab` executable itself)."""

    async def __call__(self, args: Sequence[str], timeout: float) -> ProcessResult: ...


def glab_executable() -> str:
    path = shutil.which("glab")
    if path is None:
        raise GlabNotFoundError("The glab CLI is not on PATH. Install it from https://gitlab.com/gitlab-org/cli.")
    return path


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    return env


async def run_glab(args: Sequence[str], timeout: float) -> ProcessResult:
    """The default runner: no shell, stdin from devnull, a hard timeout."""
    process = await asyncio.create_subprocess_exec(
        glab_executable(),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    return ProcessResult(process.returncode if process.returncode is not None else -1, stdout, stderr)


def run_glab_sync(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> ProcessResult:
    """The blocking variant, for the CLI, which resolves refs before any event loop runs."""
    completed = subprocess.run(
        [glab_executable(), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        env=_child_env(),
        check=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def build_api_args(
    host: str,
    endpoint: str,
    *,
    method: str | None = None,
    headers: Mapping[str, str] | None = None,
    fields: Mapping[str, str] | None = None,
    typed_fields: Mapping[str, str | int | bool] | None = None,
) -> list[str]:
    """Build the argument list for `glab api -i`.

    `fields` become `-f name=value` (always strings); `typed_fields` become `-F name=value`, which glab converts to
    JSON numbers and booleans. For `graphql`, glab sends `query` as the query and every other field as a variable.
    """
    args = ["api", "--hostname", host, "-i"]
    if method is not None:
        args += ["-X", method]
    for name, value in (headers or {}).items():
        args += ["-H", f"{name}: {value}"]
    for name, value in (fields or {}).items():
        args += ["-f", f"{name}={value}"]
    for name, typed in (typed_fields or {}).items():
        rendered = ("true" if typed else "false") if isinstance(typed, bool) else str(typed)
        args += ["-F", f"{name}={rendered}"]
    args.append(endpoint)
    return args


def parse_include_output(stdout: bytes) -> HttpResponse | None:
    """Parse the output of `glab api -i`. Return None when it holds no HTTP status line."""
    first_end = stdout.find(b"\n")
    if first_end < 0:
        return None
    status_line = stdout[:first_end].rstrip(b"\r").decode("latin-1")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        return None
    status = int(parts[1])
    reason = parts[2] if len(parts) > 2 else ""

    headers: dict[str, str] = {}
    position = first_end + 1
    while position <= len(stdout):
        line_end = stdout.find(b"\n", position)
        if line_end < 0:
            line_end = len(stdout)
        line = stdout[position:line_end].rstrip(b"\r")
        position = line_end + 1
        if not line:
            break
        name, sep, value = line.decode("latin-1").partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    body = stdout[position:] if position <= len(stdout) else b""
    return HttpResponse(status, reason, headers, body)


def stderr_message(result: ProcessResult) -> str:
    """The useful part of glab's stderr: its box-drawing decoration and blank lines removed."""
    lines = [line.strip() for line in result.stderr.decode("utf-8", errors="replace").splitlines()]
    text = " ".join(line for line in lines if line and line != "ERROR")
    return text or f"glab exited with code {result.returncode}"
