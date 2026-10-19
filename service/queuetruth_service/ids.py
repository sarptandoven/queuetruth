"""Resource ids `<prefix>_<26-char ULID>` (SPEC 4.5) and RFC 3339 timestamps for service documents."""

from __future__ import annotations

import re
import secrets
import time
from datetime import UTC, datetime

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: SPEC 4.5 id grammar: resource prefixes, `del` for tombstones (deletion records), and the internal row prefixes
#: `use` (usage events) and `aud` (audit rows), which no endpoint takes as a path parameter.
PREFIXES = ("ten", "key", "up", "ds", "job", "rp", "cal", "sc", "rpt", "del", "use", "aud")
#: The prefix of tombstone ids (`qt` tombstones, `/v1/tombstones`).
TOMBSTONE_PREFIX = "del"
ID_RE = re.compile(r"^[a-z]{2,4}_[0-9A-HJKMNP-TV-Z]{26}$")


def ulid() -> str:
    """A 26-char Crockford base32 ULID (48-bit ms timestamp + 80 random bits)."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def new_id(prefix: str) -> str:
    """A new id with one of the known prefixes."""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    return f"{prefix}_{ulid()}"


def rfc3339(ts: datetime | None) -> str | None:
    """UTC RFC 3339 string with a `Z` suffix and microseconds, or None."""
    if ts is None:
        return None
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now() -> datetime:
    """Current UTC time."""
    return datetime.now(UTC)
