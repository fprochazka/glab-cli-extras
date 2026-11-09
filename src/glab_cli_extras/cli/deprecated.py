"""The notice that the standalone glab-pipeline or glab-discussion package is still installed.

Their last release is a wrapper that depends on glab-cli-extras and declares the same console script. uv installs the
glab-cli-extras script over the wrapper's, so the wrapper's own notice never runs; this one runs in its place. It is
one line on stderr, so callers that parse stdout and the exit code see no difference.
"""

from __future__ import annotations

import sys
from typing import TextIO


def notice(command: str) -> str:
    return (
        f"{command}: this package is deprecated; glab-cli-extras provides the same command."
        f" Run: uv tool uninstall {command} && uv tool install glab-cli-extras"
    )


def warn_if_installed(command: str, stderr: TextIO | None = None) -> None:
    """Print the notice when the distribution named like `command` is installed next to glab-cli-extras.

    The lookup finds the distribution by name without reading its metadata, which costs a few milliseconds on top of
    the command's own imports. It never fails the command.
    """
    try:
        import importlib.metadata

        importlib.metadata.distribution(command)
    except Exception:
        return
    stream = stderr if stderr is not None else sys.stderr
    try:
        stream.write(notice(command) + "\n")
        stream.flush()
    except Exception:
        pass
