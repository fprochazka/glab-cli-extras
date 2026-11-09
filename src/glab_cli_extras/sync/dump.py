"""The REST half of a pipeline dump: fetch only, no files."""

from __future__ import annotations

import asyncio
from typing import Any

from glab_cli_extras.gitlab.client import GitLabClient, encode_project
from glab_cli_extras.gitlab.refs import ResourceRef


async def fetch_rest(client: GitLabClient, ref: ResourceRef) -> tuple[Any, list[Any], list[Any]]:
    """The pipeline, its jobs (retried ones included) and its bridges, as the REST API returns them."""
    base = f"projects/{encode_project(ref.project)}/pipelines/{ref.id}"
    pipeline, jobs, bridges = await asyncio.gather(
        client.get_json(base, kind="pipeline"),
        client.get_pages(f"{base}/jobs", {"include_retried": "true"}, kind="jobs"),
        client.get_pages(f"{base}/bridges", kind="bridges"),
    )
    return pipeline, jobs, bridges
