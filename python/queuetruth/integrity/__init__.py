"""Integrity primitives, one implementation each (contracts layer): canonical JSON in its published byte forms,
sha256 of bytes, text, files and streams, the hash-chain digests and the atlas Merkle tree.

Every run id, certificate, ledger entry, day root and atlas root queuetruth has published reproduces bit for bit
through these functions (`python/tests/integrity` pins them). Run ids themselves stay in `queuetruth.schemas.runid`.
"""

from __future__ import annotations

from queuetruth.integrity.canonical import (
    FORMS,
    canonical_bytes,
    canonical_json,
    escape_string,
    format_float,
    utf16_key,
    value_text,
)
from queuetruth.integrity.chain import GENESIS_ZERO, entry_digest, ledger_line
from queuetruth.integrity.digest import (
    CHUNK,
    canonical_sha256,
    sha256_bytes,
    sha256_file,
    sha256_stream,
    sha256_text,
)
from queuetruth.integrity.merkle import leaf_hash, merkle_proof, merkle_root, verify_proof

__all__ = [
    "CHUNK",
    "FORMS",
    "GENESIS_ZERO",
    "canonical_bytes",
    "canonical_json",
    "canonical_sha256",
    "entry_digest",
    "escape_string",
    "format_float",
    "leaf_hash",
    "ledger_line",
    "merkle_proof",
    "merkle_root",
    "sha256_bytes",
    "sha256_file",
    "sha256_stream",
    "sha256_text",
    "utf16_key",
    "value_text",
    "verify_proof",
]
