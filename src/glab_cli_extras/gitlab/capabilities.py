"""What a GitLab host's GraphQL schema offers, which depends on its edition and version.

GitLab Community Edition (CE) lacks the fields of the Enterprise Edition (EE), such as `MergeRequest.approvalsLeft`,
and a query that selects a missing field is rejected as a whole. So the client asks each host once a day for
`metadata { version enterprise }` and leaves out the EE-only fields on CE.

Schema introspection would answer "which fields exist" directly, but GitLab does not run it: any query with
`__schema` or `__type` gets a static dump of the whole schema (8 MB on gitlab.com), and a query with `__typename`
next to `__type` is refused with "Field '__type' doesn't exist". The edition flag needs GitLab 15.6 or newer.

When GitLab rejects a field that these capabilities said exists, the caller records the field as missing, so the next
query leaves it out without another failed request.

Two stores keep the capabilities per host for `TTL` seconds: `CapabilityStore` in memory, for the daemon, which writes
no files, and `DiskCapabilityStore`, which also keeps `<cache dir>/<host>/capabilities.json` for the short-lived
commands. Several processes may write the same file at once; each write replaces the whole file atomically, so a
reader sees one complete version, and the last writer wins. Both versions describe the same host, so that is enough.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from glab_cli_extras.paths import sanitize_part
from glab_cli_extras.store.atomic import atomic_write_json

TTL = 24 * 60 * 60
"""Capabilities change only on a GitLab upgrade, so a day-old answer is good enough."""
PROBE_RETRY = 60 * 60
"""After a probe that got no answer (network, 5xx, 429, 401), wait this long before the next one. Kept in memory only:
a short-lived command probes at most once, and the daemon does not probe on every poll."""
PROBE_QUERY = "query GitLabCapabilities { metadata { version enterprise } }"
EE_ONLY_MR_FIELDS = frozenset({"approvalsLeft"})
"""`MergeRequest` fields defined in GitLab's `ee/` code, missing on CE."""
FILE_NAME = "capabilities.json"
FORMAT = 1


@dataclass(frozen=True, slots=True)
class Capabilities:
    checked_at: float
    version: str | None = None
    enterprise: bool | None = None
    """None when GitLab did not tell, an instance older than 15.6, say: every field is then assumed to exist."""
    missing_mr_fields: frozenset[str] = field(default_factory=frozenset)
    """`MergeRequest` fields GitLab rejected, whatever the edition said."""

    def has_mr_field(self, name: str) -> bool:
        if name in self.missing_mr_fields:
            return False
        return not (self.enterprise is False and name in EE_ONLY_MR_FIELDS)

    def without_mr_field(self, name: str) -> Capabilities:
        return replace(self, missing_mr_fields=self.missing_mr_fields | {name})

    def is_fresh(self, now: float) -> bool:
        # A time in the future comes from a wrong clock; such an entry would never expire.
        return self.checked_at <= now < self.checked_at + TTL

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "checked_at": self.checked_at,
            "version": self.version,
            "enterprise": self.enterprise,
            "missing_mr_fields": sorted(self.missing_mr_fields),
        }

    @staticmethod
    def from_json(data: Any) -> Capabilities:
        """Raise ValueError on anything but the current format."""
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError("unknown capabilities format")
        checked_at = data.get("checked_at")
        version = data.get("version")
        enterprise = data.get("enterprise")
        missing = data.get("missing_mr_fields")
        if (
            not isinstance(checked_at, int | float)
            or isinstance(checked_at, bool)
            or not (version is None or isinstance(version, str))
            or not (enterprise is None or isinstance(enterprise, bool))
            or not isinstance(missing, list)
            or not all(isinstance(m, str) for m in missing)
        ):
            raise ValueError("malformed capabilities")
        return Capabilities(float(checked_at), version, enterprise, frozenset(missing))


def parse_probe(data: Any, now: float) -> Capabilities:
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if not isinstance(metadata, dict):
        return Capabilities(now)
    version = metadata.get("version")
    enterprise = metadata.get("enterprise")
    return Capabilities(
        now,
        version if isinstance(version, str) else None,
        enterprise if isinstance(enterprise, bool) else None,
    )


class CapabilityStore:
    """The capabilities of each host, in memory, until they expire."""

    def __init__(self) -> None:
        self._entries: dict[str, Capabilities] = {}
        self._probe_failed_at: dict[str, float] = {}

    def load(self, host: str, now: float) -> Capabilities | None:
        """The fresh capabilities of `host`, or None."""
        entry = self._entries.get(host)
        return entry if entry is not None and entry.is_fresh(now) else None

    def save(self, host: str, capabilities: Capabilities) -> None:
        self._entries[host] = capabilities
        self._probe_failed_at.pop(host, None)

    def probe_failed(self, host: str, now: float) -> None:
        self._probe_failed_at[host] = now

    def may_probe(self, host: str, now: float) -> bool:
        failed_at = self._probe_failed_at.get(host)
        return failed_at is None or not failed_at <= now < failed_at + PROBE_RETRY


class DiskCapabilityStore(CapabilityStore):
    """The memory store, backed by one file per host under `root`.

    The file is a cache: one that is missing, unreadable, malformed or expired reads as no entry, and a failed write is
    ignored. A probe that got no answer is never written down.
    """

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    def path(self, host: str) -> Path:
        return self.root / sanitize_part(host) / FILE_NAME

    def load(self, host: str, now: float) -> Capabilities | None:
        found = super().load(host, now)
        if found is not None:
            return found
        try:
            entry = Capabilities.from_json(json.loads(self.path(host).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        if not entry.is_fresh(now):
            return None
        super().save(host, entry)
        return entry

    def save(self, host: str, capabilities: Capabilities) -> None:
        super().save(host, capabilities)
        with contextlib.suppress(OSError):
            atomic_write_json(self.path(host), capabilities.to_json())
