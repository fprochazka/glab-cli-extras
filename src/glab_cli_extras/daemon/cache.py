"""A small in-memory cache bounded by bytes, for what the daemon fetched on behalf of its clients.

The key always carries the version of what it holds (a job id of a finished job, a discussion id with the signature
of its notes), so a hit is never stale and nothing needs invalidating. The least recently used entries go first.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from typing import Generic, TypeVar

V = TypeVar("V")

DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class ByteBoundedCache(Generic[V]):
    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self.bytes = 0
        self.hits = 0
        self.misses = 0
        self._entries: OrderedDict[Hashable, tuple[V, int]] = OrderedDict()

    def get(self, key: Hashable) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry[0]

    def put(self, key: Hashable, value: V, size: int) -> None:
        if size > self.max_bytes:
            return
        old = self._entries.pop(key, None)
        if old is not None:
            self.bytes -= old[1]
        self._entries[key] = (value, size)
        self.bytes += size
        while self.bytes > self.max_bytes:
            _, (_, dropped) = self._entries.popitem(last=False)
            self.bytes -= dropped

    def __len__(self) -> int:
        return len(self._entries)

    def describe(self) -> dict[str, int]:
        return {"entries": len(self), "bytes": self.bytes, "hits": self.hits, "misses": self.misses}
