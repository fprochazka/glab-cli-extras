"""Exclusive OS file locks: `fcntl.flock` on POSIX, `msvcrt.locking` on Windows.

The OS releases the lock when the holding process dies, so a lock file left on disk means nothing. Being able to take
the daemon lock proves that no daemon is running.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import TracebackType


class LockTimeout(TimeoutError):
    pass


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _try_lock(self, fd: int) -> bool:
        if sys.platform == "win32":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if self._try_lock(fd):
            self._fd = fd
            return True
        os.close(fd)
        return False

    def acquire(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not lock {self.path} within {timeout:.0f} s")
            time.sleep(0.05)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if sys.platform == "win32":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)

    def __enter__(self) -> FileLock:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()
