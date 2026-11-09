"""`python -m glab_cli_extras.daemon`: the daemon process. Clients start it; it is not meant to be run by hand."""

from __future__ import annotations

import sys

from glab_cli_extras.daemon.lifecycle import run_daemon

if __name__ == "__main__":
    sys.exit(run_daemon())
