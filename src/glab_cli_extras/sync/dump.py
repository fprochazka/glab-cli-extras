"""The REST half of a pipeline dump, and the extras its default plan asks for: fetch only, no files."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from glab_cli_extras.gitlab.client import GitLabClient, encode_project
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.pipeline import extras
from glab_cli_extras.pipeline.models import parse_bridges, parse_jobs, parse_pipeline
from glab_cli_extras.pipeline.summary import decide_extras


async def fetch_rest(client: GitLabClient, ref: ResourceRef) -> tuple[Any, list[Any], list[Any]]:
    """The pipeline, its jobs (retried ones included) and its bridges, as the REST API returns them."""
    base = f"projects/{encode_project(ref.project)}/pipelines/{ref.id}"
    pipeline, jobs, bridges = await asyncio.gather(
        client.get_json(base, kind="pipeline"),
        client.get_pages(f"{base}/jobs", {"include_retried": "true"}, kind="jobs"),
        client.get_pages(f"{base}/bridges", kind="bridges"),
    )
    return pipeline, jobs, bridges


async def fetch_dump(client: GitLabClient, ref: ResourceRef) -> dict[str, Any]:
    """Everything a finished pipeline's dump holds besides logs, as JSON: what `glab-pipeline inspect` fetches with
    its default options."""
    pipeline_raw, jobs_raw, bridges_raw = await fetch_rest(client, ref)
    pipeline = parse_pipeline(pipeline_raw)
    bridges = parse_bridges(bridges_raw)
    plan = decide_extras(pipeline, parse_jobs(jobs_raw), bridges, now=dt.datetime.now(dt.UTC))
    dump: dict[str, Any] = {"pipeline": pipeline_raw, "jobs": jobs_raw, "bridges": bridges_raw, "downstream": []}
    if plan.need_lint:
        dump["lint"] = await _quietly(extras.fetch_lint_cheap(client, ref.project, pipeline.sha, pipeline.ref))
    if plan.need_test_report:
        dump["test_report"] = await _quietly(extras.fetch_test_report(client, ref.project, pipeline.id))
    by_id = {b.id: b for b in bridges}
    for bid in plan.downstream_bridges:
        bridge = by_id.get(bid)
        result = await _quietly(extras.fetch_downstream(client, ref.project, bridge)) if bridge else None
        if result is not None:
            _, dpid, data = result
            dump["downstream"].append({"bridge_id": bid, "pipeline_id": dpid, "data": data})
    return dump


async def _quietly(call: Any) -> Any:
    """An extra that fails is left out, as glab-pipeline leaves it out with a warning."""
    try:
        return await call
    except Exception:  # noqa: BLE001
        return None
