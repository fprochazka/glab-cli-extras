"""JSON conversion for the frozen snapshot dataclasses.

Snapshots travel from the daemon to the client (which stores the last one in `state.json`) and back after a daemon
restart, so the new daemon diffs against what the client saw last. Only the types snapshots use are supported:
dataclasses, `tuple[X, ...]`, `dict[str, X]`, `X | None` and JSON scalars.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import types
import typing
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

T = TypeVar("T")


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return value


def _convert(tp: Any, data: Any) -> Any:
    if tp is Any:
        return data
    origin = get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        options = [a for a in get_args(tp) if a is not type(None)]
        if data is None:
            return None
        return _convert(options[0], data) if len(options) == 1 else data
    if origin is tuple:
        args = get_args(tp)
        if not isinstance(data, list | tuple):
            raise ValueError(f"expected a list for {tp}, got {type(data).__name__}")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_convert(args[0], item) for item in data)
        return tuple(_convert(a, item) for a, item in zip(args, data, strict=True))
    if origin is dict:
        _, value_type = get_args(tp)
        if not isinstance(data, dict):
            raise ValueError(f"expected an object for {tp}")
        return {str(k): _convert(value_type, v) for k, v in data.items()}
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        if not isinstance(data, dict):
            raise ValueError(f"expected an object for {tp.__name__}")
        hints = get_type_hints(tp)
        kwargs = {f.name: _convert(hints[f.name], data[f.name]) for f in dataclasses.fields(tp) if f.name in data}
        return tp(**kwargs)
    if tp is float and isinstance(data, int | float) and not isinstance(data, bool):
        return float(data)
    if tp in (int, str, bool, float) and not isinstance(data, tp):
        raise ValueError(f"expected {tp.__name__}, got {type(data).__name__}")
    return data


def from_jsonable(cls: type[T], data: Any) -> T:
    """Rebuild a dataclass from `to_jsonable` output. Raises ValueError on a shape mismatch."""
    try:
        result = _convert(cls, data)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"cannot load {cls.__name__}: {exc}") from exc
    return typing.cast(T, result)


def stable_hash(value: Any) -> str:
    canonical = json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
