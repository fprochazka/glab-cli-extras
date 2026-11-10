"""Which merge request a glab-discussion command acts on.

Explicit context is all or nothing, so a command never acts on an MR the caller did not name: `--mr-url`, or all of
`--hostname`, `--project` and `--mr-iid`, or none of them for the current branch's MR (`glab mr view`). A partial set
is a usage error. glab-discussion used to fall back to the current branch's MR silently in that case.
"""

from __future__ import annotations

import argparse

from glab_cli_extras.discussion.models import MrContext
from glab_cli_extras.gitlab.client import encode_project
from glab_cli_extras.gitlab.refs import current_branch_mr, normalize_mr_url, parse_mr_url


class ContextError(ValueError):
    """The context flags name no MR exactly. A usage error."""


def resolve_mr_context(args: argparse.Namespace) -> MrContext:
    """The MR the flags name. `project_id` holds the URL-encoded project path, which every endpoint accepts."""
    mr_url = getattr(args, "mr_url", None)
    if mr_url:
        host, path, iid = parse_mr_url(mr_url)
        return MrContext(host, encode_project(path), path, iid, normalize_mr_url(mr_url))
    hostname = getattr(args, "hostname", None)
    project = getattr(args, "project", None)
    mr_iid = getattr(args, "mr_iid", None)
    if hostname and project and mr_iid:
        url = f"https://{hostname}/{project}/-/merge_requests/{mr_iid}"
        return MrContext(hostname, encode_project(project), project, int(mr_iid), url)
    given = [name for name, value in (("--hostname", hostname), ("--project", project), ("--mr-iid", mr_iid)) if value]
    if given:
        missing = [n for n in ("--hostname", "--project", "--mr-iid") if n not in given]
        raise ContextError(f"{', '.join(given)} needs {' and '.join(missing)} too, or pass --mr-url")
    mr = current_branch_mr()
    return MrContext(mr.host, encode_project(mr.project), mr.project, mr.iid, mr.web_url)
