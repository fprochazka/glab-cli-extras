"""GraphQL queries shipped as package data."""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def load(name: str) -> str:
    return files(__name__).joinpath(f"{name}.graphql").read_text(encoding="utf-8")
