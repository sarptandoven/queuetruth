"""Rate limits.

Postgres token buckets per *tenant* (SPEC 8.7; security review M4: buckets used to be per API key, so minting keys
multiplied a tenant's budget): `read` 120/min (burst 60), `write` 30/min (burst 10), `submit` 10/min (burst 5). The
bucket is the one of the tenant bound to the calling transaction (`qt_take_token`). Headers follow
draft-ietf-httpapi-ratelimit-headers-11 (draft-based, documented as such): `RateLimit-Policy: "<bucket>";q=<per
minute>;w=60` and `RateLimit: "<bucket>";r=<whole tokens left>;t=<seconds until the next token>`; a 429 adds
`Retry-After`.

`FailureThrottle` (L4) is an in-process token bucket per client address for failed authentications. It is consulted
before any database access, so a client sending well-formed but unknown keys gets 429 after the burst instead of one
database lookup per request. The client address is the TCP peer unless `QT_FORWARDED_ALLOW_IPS` names the proxy.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Decision:
    """Outcome of one token take."""

    allowed: bool
    headers: dict[str, str]
    retry_after_s: int


def take(conn: psycopg.Connection, bucket: str, per_min: float, burst: float) -> Decision:
    """Takes one token from the bound tenant's bucket inside the caller's tenant transaction."""
    rate = per_min / 60.0
    row = conn.execute("select * from qt_take_token(%s, %s, %s)", (bucket, rate, float(burst))).fetchone()
    assert row is not None
    allowed, remaining, retry = bool(row[0]), float(row[1]), float(row[2])
    whole = max(0, math.floor(remaining))
    t = 0 if whole >= 1 else max(1, math.ceil((1 - remaining) / rate))
    headers = {
        "RateLimit-Policy": f'"{bucket}";q={int(per_min)};w=60',
        "RateLimit": f'"{bucket}";r={whole};t={t}',
    }
    retry_after = max(1, math.ceil(retry)) if not allowed else 0
    if not allowed:
        headers["Retry-After"] = str(retry_after)
    return Decision(allowed, headers, retry_after)


class FailureThrottle:
    """Per-client token bucket of failed authentications (thread-safe, bounded to `max_clients` addresses)."""

    def __init__(self, per_min: float, burst: float, max_clients: int = 10_000) -> None:
        self.rate = max(per_min, 1e-9) / 60.0
        self.burst = float(burst)
        self.max_clients = max_clients
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _level(self, client: str, now: float) -> float:
        tokens, ts = self._buckets.get(client, (self.burst, now))
        return min(self.burst, tokens + max(0.0, now - ts) * self.rate)

    def retry_after(self, client: str) -> int | None:
        """Seconds until `client` may try again, or None when it may authenticate now."""
        with self._lock:
            level = self._level(client, time.monotonic())
        return None if level >= 1 else max(1, math.ceil((1 - level) / self.rate))

    def failed(self, client: str) -> None:
        """Records one failed authentication of `client`."""
        now = time.monotonic()
        with self._lock:
            self._buckets[client] = (max(0.0, self._level(client, now) - 1), now)
            self._buckets.move_to_end(client)
            while len(self._buckets) > self.max_clients:
                self._buckets.popitem(last=False)
