"""glab-watch as a cli-watch-kit app: its name, daemon, envelope and the shape of its Claude Code messages.

The name stays `glab-cli-extras`, so the base dir (`<scratchpad>/glab-cli-extras`), the daemon's state dir
(`<XDG_RUNTIME_DIR>/glab-cli-extras`) and the `GLAB_CLI_EXTRAS_*` variables keep the names they had before the
library. The daemon's socket is `daemon-l<library protocol>-a<APP_PROTOCOL>.sock`, so a daemon of the protocol-4
glab-watch (`daemon-v4.sock`) in the same dir is a different daemon, and both can run while their watchers do.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from cli_watch_kit.app import WatchApp
from cli_watch_kit.events import WATCH_STARTED, Event

from glab_cli_extras import __version__
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.watch.source import GlabSource

APP_PROTOCOL = 5
"""glab-watch's own wire version. Before the library it was the daemon protocol, last 4."""


def relative_threads_dir(event: Event, directory: Path) -> Event:
    """The `watch_started` of the message that names the resource dir shows the threads dir relative to it: the
    message head already names the dir. stdout and the changelog keep the full path."""
    summary = event.data.get("summary")
    if event.kind != WATCH_STARTED or not isinstance(summary, dict):
        return event
    threads = summary.get("threads")
    if not isinstance(threads, dict) or not isinstance(threads.get("dir"), str):
        return event
    path = Path(threads["dir"])
    if not path.is_relative_to(directory):
        return event
    summary = {**summary, "threads": {**threads, "dir": str(path.relative_to(directory))}}
    return replace(event, data={**event.data, "summary": summary})


def sender_name(ref: ResourceRef) -> str:
    """The name Claude Code shows for the sender: `glab-watch mr group/project!123`, `glab-watch my-mrs <host>`."""
    if ref.kind == "my-mrs":
        return f"glab-watch my-mrs {ref.short}"
    short = ref.short.split("/", 1)[1] if "/" in ref.short else ref.short
    return f"glab-watch {ref.kind} {short}"


APP = WatchApp(
    name="glab-cli-extras",
    source=GlabSource,
    daemon_module="glab_cli_extras.watch.daemon",
    app_protocol=APP_PROTOCOL,
    version=__version__,
    status_prefix="glab-watch: ",
    envelope_tag="glab-watcher",
    command="glab-watch",
    env_prefix="GLAB_CLI_EXTRAS_",
    sender="glab-watch",
    announced=relative_threads_dir,
)
