"""Which resource polls next.

Resources wait in due-time order. Among those that are due, urgent ones go first (probes that can end a watch), then
the one served longest ago. That round-robin keeps one busy pipeline from starving a quiet MR watch when the budget
is short. The scheduler holds no clock and does no I/O; the engine passes the time in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _Entry:
    due: float
    urgent: bool


class Scheduler:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._last_served: dict[str, float] = {}

    def schedule(self, key: str, due: float, *, urgent: bool = False) -> None:
        self._entries[key] = _Entry(due, urgent)

    def remove(self, key: str) -> None:
        self._entries.pop(key, None)

    def forget(self, key: str) -> None:
        self._entries.pop(key, None)
        self._last_served.pop(key, None)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def due_at(self, key: str) -> float | None:
        entry = self._entries.get(key)
        return entry.due if entry else None

    def next_due(self) -> float | None:
        return min((e.due for e in self._entries.values()), default=None)

    def ready(self, now: float) -> list[str]:
        """Due keys in serving order: urgent first, then least recently served, then earliest due."""
        due = [(key, entry) for key, entry in self._entries.items() if entry.due <= now]
        due.sort(key=lambda item: (not item[1].urgent, self._last_served.get(item[0], float("-inf")), item[1].due))
        return [key for key, _ in due]

    def mark_served(self, key: str, now: float) -> None:
        self._entries.pop(key, None)
        self._last_served[key] = now
