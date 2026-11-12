"""Hash chains: each entry names the hash of the one before it, so changing, dropping or reordering an entry
changes every later hash.

Three chains are published and each keeps its rule; they share these pieces:

- the run ledger (`queuetruth.ledger`): `hash = sha256(jcs(record))`, `record.prev` = the previous hash,
  `GENESIS_ZERO` for the first record; a line is `{"hash":H,"record":R}` (`ledger_line`);
- the forecast ledger (`queuetruth.forecast.ledger`): `entry_sha256 = entry_digest(entry, "entry_sha256")`,
  `prev_sha256` = the previous entry's, `GENESIS_ZERO` for the first;
- the sandbox order log (`queuetruth.sandbox.log`): `hash = entry_digest(entry, "hash")`, the first `prev` is
  `sha256_text("queuetruth.sandbox.order_log.v1")`.
"""

from __future__ import annotations

import hashlib
from typing import Any

from queuetruth.integrity.canonical import canonical_bytes

#: `prev` of the first entry of the run ledger and the forecast ledger.
GENESIS_ZERO = "0" * 64

_LINE_PREFIX = b'{"hash":"'
_LINE_MID = b'","record":'


def entry_digest(entry: dict[str, Any], without: str) -> str:
    """Hex sha256 of the `jcs` canonical JSON of `entry` without its own hash field `without`."""
    return hashlib.sha256(canonical_bytes({k: v for k, v in entry.items() if k != without})).hexdigest()


def ledger_line(record: dict[str, Any]) -> tuple[bytes, str]:
    """(line with newline, hash) of a run ledger record: the canonical JSON of `{"hash": H, "record": R}` where `H`
    is the sha256 of the exact bytes of `R` in the line."""
    body = canonical_bytes(record)
    h = hashlib.sha256(body).hexdigest()
    return _LINE_PREFIX + h.encode("ascii") + _LINE_MID + body + b"}\n", h


__all__ = ["GENESIS_ZERO", "entry_digest", "ledger_line"]
