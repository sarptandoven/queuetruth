"""The atlas Merkle tree (the manifest contract, `docs/scale.md`).

Leaves are `sha256(b"\\x00" + canonical_bytes(leaf_doc, "atlas-v1"))` in unit-id order; inner nodes are
`sha256(b"\\x01" + left + right)`; an odd node is carried up unchanged; the root of no leaves is `sha256(b"")`.
A day root covers that day's unit leaves and the atlas root is the same tree over the day roots in date order.
A proof is the sibling path from the leaf up, `[["L" or "R", sibling hex], ...]`, with carried levels skipped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from queuetruth.integrity.canonical import canonical_bytes

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"


def leaf_hash(leaf_doc: dict[str, Any]) -> str:
    """Hex leaf hash of one unit's leaf document."""
    return hashlib.sha256(LEAF_PREFIX + canonical_bytes(leaf_doc, "atlas-v1")).hexdigest()


def _level_up(level: list[bytes]) -> list[bytes]:
    nxt = []
    for i in range(0, len(level), 2):
        if i + 1 < len(level):
            nxt.append(hashlib.sha256(NODE_PREFIX + level[i] + level[i + 1]).digest())
        else:
            nxt.append(level[i])
    return nxt


def merkle_root(leaves: Sequence[str]) -> str:
    """Hex root over hex leaves, in the order given."""
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    level = [bytes.fromhex(x) for x in leaves]
    while len(level) > 1:
        level = _level_up(level)
    return level[0].hex()


def merkle_proof(leaves: Sequence[str], index: int) -> list[list[str]]:
    """Sibling path of leaf `index`: `[["L" or "R", sibling hex], ...]` from the leaf up (carried nodes skipped)."""
    level = [bytes.fromhex(x) for x in leaves]
    path: list[list[str]] = []
    i = index
    while len(level) > 1:
        sib = i ^ 1
        if sib < len(level):
            path.append(["L" if sib < i else "R", level[sib].hex()])
        level = _level_up(level)
        i //= 2
    return path


def verify_proof(leaf: str, proof: Sequence[Sequence[str]], root: str) -> bool:
    """True when `proof` takes `leaf` to `root`."""
    h = bytes.fromhex(leaf)
    for side, sib in proof:
        s = bytes.fromhex(sib)
        h = hashlib.sha256(NODE_PREFIX + (s + h if side == "L" else h + s)).digest()
    return h.hex() == root


__all__ = ["LEAF_PREFIX", "NODE_PREFIX", "leaf_hash", "merkle_proof", "merkle_root", "verify_proof"]
