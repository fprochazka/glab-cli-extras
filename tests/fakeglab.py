"""A fake `glab` that answers `glab api -i` and `glab repo view -F json` from canned routes.

It renders responses the way glab 1.128 prints them with `-i`: the status line ends in LF, header lines in CRLF, a
blank CRLF line, then the body. The same routes serve two uses:

- `FakeGlab` is an in-process runner for the client and the pollers.
- Run as a script (`python fakeglab.py ...`, through the `glab` wrapper `install_fake_glab` writes), it reads its
  routes from the JSON file named by `FAKE_GLAB_SPEC` and appends each call to `FAKE_GLAB_LOG`. The daemon tests and
  the CLI tests put that wrapper first on PATH.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

from glab_cli_extras.gitlab.glab import ProcessResult

RATE_LIMIT_HEADERS = {
    "Ratelimit-Limit": "480",
    "Ratelimit-Name": "throttle_authenticated_api",
    "Ratelimit-Observed": "12",
    "Ratelimit-Remaining": "14388",
    "Ratelimit-Reset": "1794220200",
}


def render(status: int, body: Any = None, headers: dict[str, str] | None = None) -> bytes:
    reason = HTTPStatus(status).phrase
    lines = [f"HTTP/2.0 {status} {reason}\n".encode()]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}\r\n".encode())
    lines.append(b"\r\n")
    if body is None:
        payload = b""
    elif isinstance(body, bytes):
        payload = body
    elif isinstance(body, str):
        payload = body.encode()
    else:
        payload = json.dumps(body).encode()
    return b"".join(lines) + payload


@dataclass
class Route:
    match: str
    """Substring of the endpoint, or of the GraphQL query text for `graphql`."""
    body: Any = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    etag: str | None = None
    """When set, the response carries this ETag, and a request with a matching If-None-Match gets a 304."""
    contains: str | None = None
    """An extra substring the joined arguments must contain, to tell apart requests to the same endpoint."""


@dataclass
class Call:
    args: list[str]

    @property
    def endpoint(self) -> str:
        return self.args[-1]

    @property
    def headers(self) -> dict[str, str]:
        result = {}
        for flag, value in zip(self.args, self.args[1:], strict=False):
            if flag == "-H":
                name, _, val = value.partition(": ")
                result[name] = val
        return result

    @property
    def fields(self) -> dict[str, str]:
        result = {}
        for flag, value in zip(self.args, self.args[1:], strict=False):
            if flag in ("-f", "-F"):
                name, _, val = value.partition("=")
                result[name] = val
        return result


def answer(routes: Sequence[Route], args: Sequence[str], repo_view: Any = None) -> ProcessResult:
    call = Call(list(args))
    if list(args[:2]) == ["repo", "view"]:
        if repo_view is None:
            return ProcessResult(0, json.dumps({"error": {"message": "no git remotes found"}}).encode(), b"")
        return ProcessResult(0, json.dumps(repo_view).encode(), b"")
    joined = " ".join(args)
    target = call.fields.get("query", "") if call.endpoint == "graphql" else call.endpoint
    for route in routes:
        if route.match not in target or (route.contains and route.contains not in joined):
            continue
        headers = {**RATE_LIMIT_HEADERS, **route.headers}
        if route.etag is not None:
            if call.headers.get("If-None-Match") == route.etag:
                # Like GitLab's ETag middleware: no rate-limit headers on a cached 304.
                return ProcessResult(
                    1, render(304, None, {"Etag": route.etag, "X-Gitlab-From-Cache": "true"}), b"glab: HTTP 304\n"
                )
            headers["Etag"] = route.etag
        code = 0 if route.status < 300 else 1
        stderr = b"" if code == 0 else f"glab: HTTP {route.status}\n".encode()
        return ProcessResult(code, render(route.status, route.body, headers), stderr)
    return ProcessResult(1, b"", f"ERROR\n  no fake route for {target[:80]!r}\n".encode())


class FakeGlab:
    """An in-process `GlabRunner`."""

    def __init__(self, routes: list[Route] | None = None, repo_view: Any = None) -> None:
        self.routes = list(routes or [])
        self.repo_view = repo_view
        self.calls: list[Call] = []

    async def __call__(self, args: Sequence[str], timeout: float) -> ProcessResult:
        self.calls.append(Call(list(args)))
        return answer(self.routes, args, self.repo_view)

    def sync(self, args: Sequence[str]) -> ProcessResult:
        self.calls.append(Call(list(args)))
        return answer(self.routes, args, self.repo_view)

    def endpoints(self) -> list[str]:
        return [c.endpoint for c in self.calls]


def write_spec(path: Path, routes: Sequence[Route], repo_view: Any = None) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"routes": [asdict(r) for r in routes], "repo_view": repo_view}), encoding="utf-8")
    os.replace(tmp, path)


def install_fake_glab(bin_dir: Path) -> None:
    """Write a `glab` executable into `bin_dir` that runs this module as a script with the current Python."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    if os.name == "nt":
        (bin_dir / "glab.bat").write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        return
    wrapper = bin_dir / "glab"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _main(argv: list[str]) -> int:
    spec = json.loads(Path(os.environ["FAKE_GLAB_SPEC"]).read_text(encoding="utf-8"))
    routes = [Route(**r) for r in spec.get("routes", [])]
    log = os.environ.get("FAKE_GLAB_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    result = answer(routes, argv, spec.get("repo_view"))
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.exit(_main(sys.argv[1:]))
