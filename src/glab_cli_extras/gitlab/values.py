"""Small conversions of GitLab API values."""

from __future__ import annotations

from typing import Any


def gid_to_int(gid: Any) -> int:
    """`gid://gitlab/Ci::Pipeline/123` → 123."""
    return int(str(gid).rsplit("/", 1)[-1])


def lower(value: Any) -> str:
    return str(value).lower() if value is not None else ""
