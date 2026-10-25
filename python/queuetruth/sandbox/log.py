"""The committed order log: every instruction of an episode, in order, with the information set it was made on,
hash-chained (`qt.sandbox_order_log`).

Entry hash = `sha256(canonical_json(entry without "hash"))` (RFC 8785, `queuetruth.schemas.runid.canonical_json`);
each entry names the previous entry's hash in `prev`, the first one names `GENESIS`; `root` = the last hash. Changing,
dropping, reordering or inserting any entry changes every later hash. Entries also carry the outcome digests that
make the log re-executable (`verify`): an `advance` entry holds the sha256 of the observation the agent received, a
`submit` entry the order the sandbox derived from the request (id, latency, collar price), `finish` the digest of the
final orders and fills.
"""

from __future__ import annotations

from typing import Any

from queuetruth.sandbox._util import digest, sha256_text
from queuetruth.sandbox.models import LogEntry, SandboxOrderLog

#: `prev` of the first entry.
GENESIS = sha256_text("queuetruth.sandbox.order_log.v1")


def entry_hash(entry: dict[str, Any]) -> str:
    """Hash of an entry (every field but `hash`)."""
    return digest({k: v for k, v in entry.items() if k != "hash"})


class CommitLog:
    """Append-only hash-chained log of one episode."""

    def __init__(self, *, engine_version: str, dataset_id: str, spec_id: str, spec: dict[str, Any]) -> None:
        self.engine_version = engine_version
        self.dataset_id = dataset_id
        self.spec_id = spec_id
        self.spec = spec
        self.entries: list[dict[str, Any]] = []

    @property
    def root(self) -> str:
        """Hash of the last entry (`GENESIS` while empty)."""
        return self.entries[-1]["hash"] if self.entries else GENESIS

    def append(
        self, kind: str, *, clock_ns: int, visible_ns: int, rows_seen: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Commits one entry and returns it (with its hash)."""
        entry: dict[str, Any] = {
            "seq": len(self.entries),
            "kind": kind,
            "clock_ns": int(clock_ns),
            "visible_ns": int(visible_ns),
            "rows_seen": int(rows_seen),
            "data": data,
            "prev": self.root,
        }
        entry["hash"] = entry_hash(entry)
        LogEntry.model_validate(entry)
        self.entries.append(entry)
        return entry

    def document(self) -> SandboxOrderLog:
        """The `qt.sandbox_order_log` document."""
        return SandboxOrderLog.model_validate(
            {
                "schema": "qt.sandbox_order_log",
                "schema_version": 1,
                "engine_version": self.engine_version,
                "dataset_id": self.dataset_id,
                "spec_id": self.spec_id,
                "spec": self.spec,
                "entries": self.entries,
                "root": self.root,
            }
        )


def check_chain(doc: dict[str, Any]) -> list[str]:
    """Problems of a log's hash chain (empty when every hash, link, sequence number and the root check out)."""
    problems: list[str] = []
    prev = GENESIS
    entries = doc.get("entries") or []
    for i, e in enumerate(entries):
        if e.get("seq") != i:
            problems.append(f"entry {i}: seq is {e.get('seq')!r}")
        if e.get("prev") != prev:
            problems.append(f"entry {i}: prev does not name the previous entry's hash")
        h = entry_hash(e)
        if e.get("hash") != h:
            problems.append(f"entry {i}: hash does not match its content")
        prev = e.get("hash", h)
    if doc.get("root") != (entries[-1].get("hash") if entries else GENESIS):
        problems.append("root is not the last entry's hash")
    return problems


__all__ = ["GENESIS", "CommitLog", "check_chain", "entry_hash"]
