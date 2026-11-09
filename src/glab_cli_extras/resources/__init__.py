"""One poller per resource kind."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from glab_cli_extras.resources.base import Poller
from glab_cli_extras.resources.branch import BranchPoller
from glab_cli_extras.resources.mr import MrPoller
from glab_cli_extras.resources.mr_list import MrListPoller
from glab_cli_extras.resources.pipeline import PipelinePoller

POLLERS: Mapping[str, type[Poller[Any]]] = {
    "mr": MrPoller,
    "pipeline": PipelinePoller,
    "mrs": MrListPoller,
    "branch": BranchPoller,
}
