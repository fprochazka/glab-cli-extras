"""`python -m glab_cli_extras.watch.daemon`: glab-watch's daemon. Watchers start it, never a person."""

from __future__ import annotations

from cli_watch_kit.daemon import main

from glab_cli_extras.watch.app import APP

if __name__ == "__main__":
    main(APP)
