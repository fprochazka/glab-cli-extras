"""Dispatch of daemon requests to the engine."""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

from glab_cli_extras import __version__
from glab_cli_extras.daemon.engine import Engine, EngineError
from glab_cli_extras.daemon.transport import PROTOCOL_VERSION
from glab_cli_extras.events import ALL_KINDS
from glab_cli_extras.gitlab.client import ApiError
from glab_cli_extras.gitlab.refs import RefError, ResourceRef
from glab_cli_extras.sync.job_logs import LOGS_MODES, LogContent, LogsMode


class RequestError(ValueError):
    pass


def _str(request: dict[str, Any], name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not value:
        raise RequestError(f"{name} must be a non-empty string")
    return value


async def handle(engine: Engine, request: dict[str, Any], stop: asyncio.Event) -> dict[str, Any]:
    op = request.get("op")
    try:
        if op == "hello":
            return {"ok": True, "protocol": PROTOCOL_VERSION, "version": __version__, "pid": os.getpid()}
        if op == "subscribe":
            options = request.get("options") or {}
            if not isinstance(options, dict):
                raise RequestError("options must be an object")
            raw_events = options.get("events")
            if not isinstance(raw_events, list) or not all(isinstance(e, str) for e in raw_events):
                raise RequestError("options.events must be a list of event kinds")
            unknown = set(raw_events) - ALL_KINDS
            if unknown:
                raise RequestError(f"unknown event kinds: {', '.join(sorted(unknown))}")
            logs = options.get("logs", "failed")
            if logs not in LOGS_MODES:
                raise RequestError(f"options.logs must be one of {', '.join(LOGS_MODES)}")
            logs_mode: LogsMode = logs
            result = engine.subscribe(
                _str(request, "client_id"),
                ResourceRef.from_wire(request.get("resource")),
                frozenset(raw_events),
                logs_mode,
                options.get("baseline"),
            )
            return {"ok": True, **result}
        if op == "poll":
            cursors = request.get("cursors") or {}
            if not isinstance(cursors, dict) or not all(isinstance(v, int) for v in cursors.values()):
                raise RequestError("cursors must map subscription ids to integers")
            return {"ok": True, **engine.poll(_str(request, "client_id"), cursors)}
        if op == "fetch_job_log":
            job_id = request.get("job_id")
            if not isinstance(job_id, int):
                raise RequestError("job_id must be an integer")
            content = await engine.fetch_job_log(_str(request, "host"), _str(request, "project"), job_id)
            return {"ok": True, "log": encode_log(content)}
        if op == "fetch_threads":
            ids = request.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise RequestError("ids must be a list of discussion ids")
            discussions = await engine.fetch_threads(ResourceRef.from_wire(request.get("resource")), set(ids))
            return {"ok": True, "discussions": discussions}
        if op == "fetch_dump":
            dump = await engine.fetch_dump(ResourceRef.from_wire(request.get("resource")))
            return {"ok": True, "dump": dump}
        if op == "unsubscribe":
            removed = engine.unsubscribe(_str(request, "client_id"), _str(request, "subscription_id"))
            return {"ok": True, "removed": removed}
        if op == "status":
            now = engine.clock()
            return {
                "ok": True,
                "pid": os.getpid(),
                "version": __version__,
                "protocol": PROTOCOL_VERSION,
                "uptime_s": round(now - engine.started_at, 1),
                "idle_s": round(engine.idle_for(now), 1),
                **engine.status(),
            }
        if op == "stop":
            stop.set()
            return {"ok": True, "pid": os.getpid()}
        return {"ok": False, "error": f"unknown op {op!r}"}
    except (RequestError, RefError, EngineError) as exc:
        return {"ok": False, "error": str(exc)}
    except ApiError as exc:
        return {"ok": False, "error": str(exc), "gitlab_status": exc.status}


def encode_log(content: LogContent) -> dict[str, Any]:
    """A log on the wire: its bytes base64-encoded, because a trace need not be valid UTF-8."""
    body = base64.b64encode(content.body).decode("ascii")
    return {"body_b64": body, "truncated": content.truncated, "size": content.size}


def decode_log(data: Any) -> LogContent:
    if not isinstance(data, dict) or not isinstance(data.get("body_b64"), str):
        raise ValueError("the daemon sent no log")
    size = data.get("size")
    known_size = size if isinstance(size, int) else None
    return LogContent(base64.b64decode(data["body_b64"]), bool(data.get("truncated")), known_size)
