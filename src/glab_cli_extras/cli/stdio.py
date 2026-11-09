"""UTF-8 on stdin, stdout and stderr for every command, whatever the console code page is."""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Make the standard streams UTF-8, as the files are.

    Python encodes a pipe with the locale's code page, which is cp1252 on Windows. There an emoji in an MR title makes
    the event stream crash, and an em dash in a summary reaches the reader as another character. Bodies piped in with
    `--body -` are read as UTF-8 too. Streams replaced by test harnesses may lack reconfigure(); those are skipped.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
