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
        # strict=False: a raw control character in a title or a note body must not fail the whole response.
        return json.loads(self.body.decode("utf-8"), strict=False)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class GlabRunner(Protocol):
    """Run glab with the given arguments (without the `glab` executable itself), with `stdin` as its input."""

    async def __call__(self, args: Sequence[str], timeout: float, stdin: bytes | None = None) -> ProcessResult: ...


def glab_executable() -> str:
    path = shutil.which("glab")
    if path is None:
        raise GlabNotFoundError("The glab CLI is not on PATH. Install it from https://gitlab.com/gitlab-org/cli.")
    return path


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    return env


async def run_glab(args: Sequence[str], timeout: float, stdin: bytes | None = None) -> ProcessResult:
    """The default runner: no shell, stdin from devnull unless a body is given, a hard timeout."""
    process = await asyncio.create_subprocess_exec(
        glab_executable(),
        *args,
        stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout)
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    return ProcessResult(process.returncode if process.returncode is not None else -1, stdout, stderr)


def run_glab_sync(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT, stdin: bytes | None = None) -> ProcessResult:
    """The blocking variant, for the CLI, which resolves refs before any event loop runs."""
    completed = subprocess.run(
        [glab_executable(), *args],
        stdin=subprocess.DEVNULL if stdin is None else None,
        input=stdin,
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
    json_body: bool = False,
) -> list[str]:
    """Build the argument list for `glab api -i`.

    A request body is always JSON on stdin (`--input -`), never `-f`/`-F` fields: `-F` turns `true` and numbers into
    JSON types and reads a value starting with `@` as a file, form fields flatten nested objects such as a diff note's
    `position` (GitLab then drops it silently), and neither can send a list. GraphQL goes the same way, as
    `{"query": ..., "variables": ...}`.
    """
    args = ["api", "--hostname", host, "-i"]
    if method is not None:
        args += ["-X", method]
    for name, value in (headers or {}).items():
        args += ["-H", f"{name}: {value}"]
    if json_body:
        args += ["-H", "Content-Type: application/json", "--input", "-"]
    args.append(endpoint)
    return args


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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


def error_message(response: HttpResponse) -> str:
    """GitLab's own error text from an error response, else the status line."""
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "error", "error_description"):
            if key in payload:
                return str(payload[key])
    return f"HTTP {response.status} {response.reason}".strip()


def stderr_message(result: ProcessResult) -> str:
    """The useful part of glab's stderr: its box-drawing decoration and blank lines removed."""
    lines = [line.strip() for line in result.stderr.decode("utf-8", errors="replace").splitlines()]
    text = " ".join(line for line in lines if line and line != "ERROR")
    return text or f"glab exited with code {result.returncode}"
