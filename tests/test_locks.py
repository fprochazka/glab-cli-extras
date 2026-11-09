from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from glab_cli_extras.store.locks import FileLock, LockTimeout

HOLDER = """
import sys, time
from pathlib import Path
from glab_cli_extras.store.locks import FileLock
lock = FileLock(Path(sys.argv[1]))
assert lock.try_acquire()
print("held", flush=True)
sys.stdin.readline()
"""


def test_a_second_holder_is_refused_until_release(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    first, second = FileLock(path), FileLock(path)
    assert first.try_acquire()
    assert not second.try_acquire()
    with pytest.raises(LockTimeout):
        second.acquire(timeout=0.2)
    first.release()
    assert second.try_acquire()
    second.release()


def test_the_os_releases_the_lock_when_the_holder_dies(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
    lock = FileLock(path)
    assert not lock.try_acquire()
    holder.kill()
    holder.wait(timeout=10)
    lock.acquire(timeout=5)
    lock.release()
