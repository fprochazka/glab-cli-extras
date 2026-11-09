"""Dispatch of daemon requests to the engine."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from glab_cli_extras import __version__
from glab_cli_extras.artifacts.job_logs import LOGS_MODES, LogsMode
from glab_cli_extras.daemon.engine import Engine, EngineError
from glab_cli_extras.daemon.transport import PROTOCOL_VERSION
from glab_cli_extras.events import ALL_KINDS
from glab_cli_extras.gitlab.refs import RefError, ResourceRef


class RequestError(ValueError):
    pass


def _str(request: dict[str, Any], name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not value:
        raise RequestError(f"{name} must be a non-empty string")
    return value


def handle(engine: Engine, request: dict[str, Any], stop: asyncio.Event) -> dict[str, Any]:
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
