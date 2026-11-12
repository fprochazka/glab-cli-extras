"""GraphQL queries shipped as package data."""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def load(name: str, *fragments: str) -> str:
    """The query in `<name>.graphql`, followed by the fragment files it spreads, so queries can share fields."""
    return "\n".join(
        files(__name__).joinpath(f"{part}.graphql").read_text(encoding="utf-8") for part in (name, *fragments)
    )
