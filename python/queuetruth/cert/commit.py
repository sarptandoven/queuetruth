"""Timestamped hash commitments: commit to files now (forecasts, order schedules, shadow specs), reveal them later.

`commit(files)` draws a fresh 32-byte nonce per file and writes two documents:

- the public `qt.commitment` (leaf digests, root, `committed_at`, optional Ed25519 signature): publish or hand it
  over now; without the nonces it reveals nothing about the files but their number;
- the private `qt.commitment_opening` (per-file name, size, sha256 and nonce): keep it secret until reveal. Any
  subset of its items opens exactly those items, so files can be revealed one at a time.

Leaf i = sha256(`b"qt-commit-v1\\0leaf\\0"` + nonce_i + UTF-8 name_i + `b"\\0"` + sha256(file_i) as 32 bytes) and
root = sha256(`b"qt-commit-v1\\0root\\0"` + leaf_0 + ... + leaf_(n-1)). The nonce makes the commitment hiding (a
low-entropy forecast such as `0.62` cannot be found by hashing guesses) and sha256 makes it binding.

`committed_at` is whatever clock the committer's machine showed (or `SOURCE_DATE_EPOCH`). The commitment proves
that the files existed no later than the moment the root was first seen by someone else; anchoring the root with an
RFC 3161 time-stamping authority, a public post or a counterparty is what turns it into evidence of time.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from queuetruth.cert import keys
from queuetruth.cert.hashing import sha256_file
from queuetruth.cert.models import (
    Check,
    Commitment,
    CommitmentOpening,
    CommitmentVerification,
    OpeningItem,
    RevealOutcome,
    VerifiedCommitment,
)
from queuetruth.cert.util import now_ns, write_json
from queuetruth.errors import QtError

LEAF_TAG = b"qt-commit-v1\x00leaf\x00"
ROOT_TAG = b"qt-commit-v1\x00root\x00"


def leaf(nonce_hex: str, name: str, sha256_hex: str) -> str:
    """The leaf digest of one committed file."""
    if "\x00" in name:
        raise QtError("QT-INPUT-002", f"committed name {name!r} contains a NUL character")
    return hashlib.sha256(
        LEAF_TAG + bytes.fromhex(nonce_hex) + name.encode("utf-8") + b"\x00" + bytes.fromhex(sha256_hex)
    ).hexdigest()


def root_of(leaves: list[str]) -> str:
    """The root digest over the leaves in order."""
    return hashlib.sha256(ROOT_TAG + b"".join(bytes.fromhex(x) for x in leaves)).hexdigest()


def commitment_body(doc: dict[str, Any]) -> dict[str, Any]:
    """What a commitment signature covers: the document without `signature`."""
    return {k: v for k, v in doc.items() if k != "signature"}


def commit(
    files: list[Path],
    *,
    names: list[str] | None = None,
    label: str | None = None,
    key: keys.SigningKey | None = None,
) -> tuple[Commitment, CommitmentOpening]:
    """Commits to `files` (names default to the file names; they must be unique)."""
    if not files:
        raise QtError("QT-USAGE-001", "commit: give at least one file")
    names = names if names is not None else [f.name for f in files]
    if len(set(names)) != len(names):
        dup = sorted({n for n in names if names.count(n) > 1})
        raise QtError("QT-USAGE-001", f"commit: duplicate names {dup}", "commit files with distinct names")
    items: list[OpeningItem] = []
    for i, (f, name) in enumerate(zip(files, names, strict=True)):
        if not f.is_file():
            raise QtError("QT-INPUT-001", f"{f}: file not found (commit files, not directories)")
        items.append(
            OpeningItem(index=i, name=name, bytes=f.stat().st_size, sha256=sha256_file(f), nonce=secrets.token_hex(32))
        )
    leaves = [leaf(it.nonce, it.name, it.sha256) for it in items]
    c = Commitment(label=label, committed_at=now_ns(), n_items=len(items), leaves=leaves, root=root_of(leaves))
    if key is not None:
        body = commitment_body(c.model_dump(mode="json"))
        c = c.model_copy(update={"signature": key.sign(keys.COMMIT_DOMAIN, body)})
    return c, CommitmentOpening(root=c.root, items=items)


def load_doc(path: Path, model: type[Any]) -> tuple[Any, dict[str, Any]]:
    """(validated model, raw dict) of a JSON document; `QT-INPUT-001/002` when missing or invalid."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e.strerror or e}") from None
    except (UnicodeDecodeError, ValueError) as e:
        raise QtError("QT-INPUT-002", f"{path}: invalid JSON ({e})") from None
    if not isinstance(raw, dict) or raw.get("schema") != model.SCHEMA:
        got = raw.get("schema") if isinstance(raw, dict) else type(raw).__name__
        raise QtError("QT-INPUT-002", f"{path}: expected a {model.SCHEMA} document, got {got!r}")
    try:
        return model.model_validate(raw), raw
    except ValidationError as e:
        first = e.errors()[0]
        loc = "/".join(str(x) for x in first["loc"])
        raise QtError("QT-INPUT-002", f"{path}: not a valid {model.SCHEMA} document ({loc}: {first['msg']})") from None


def structure_check(c: Commitment) -> Check:
    """`n_items` and `root` agree with the leaves."""
    problems = []
    if c.n_items != len(c.leaves):
        problems.append(f"n_items {c.n_items} but {len(c.leaves)} leaves")
    if root_of(c.leaves) != c.root:
        problems.append("root is not the digest of the leaves")
    if problems:
        return Check(
            id="structure", status="fail", summary="the commitment is internally inconsistent", details=problems
        )
    return Check(id="structure", status="pass", summary=f"root recomputes from {len(c.leaves)} leaves")


def signature_check(
    c: Commitment, raw: dict[str, Any], trusted: set[str] | None
) -> tuple[list[Check], str | None, bool | None]:
    """Signature and key-pin checks of a commitment: (checks, key fingerprint, key trusted)."""
    out: list[Check] = []
    fp = c.signature.key_fingerprint if c.signature is not None else None
    if c.signature is None:
        out.append(Check(id="signature", status="skip", summary="unsigned commitment"))
    else:
        ok, why = keys.verify(c.signature, keys.COMMIT_DOMAIN, commitment_body(raw))
        out.append(Check(id="signature", status="pass" if ok else "fail", summary=why, details=[f"key {fp}"]))
    trusted_ok: bool | None = None
    if trusted:
        trusted_ok = fp is not None and fp in trusted
        out.append(
            Check(
                id="key_pin",
                status="pass" if trusted_ok else "fail",
                summary="signed by a trusted key" if trusted_ok else "not signed by any trusted key",
            )
        )
    return out, fp, trusted_ok


def open_items(c: Commitment, opening: CommitmentOpening) -> list[tuple[OpeningItem, bool]]:
    """(item, leaf matches) for every opened item; `QT-INPUT-002` when the opening is for another commitment."""
    if opening.root != c.root:
        raise QtError("QT-INPUT-002", "the opening is for another commitment (its root differs)")
    out = []
    for it in opening.items:
        ok = 0 <= it.index < len(c.leaves) and leaf(it.nonce, it.name, it.sha256) == c.leaves[it.index]
        out.append((it, ok))
    return out


def reveal(
    commitment_path: Path,
    opening_path: Path,
    files: list[Path],
    *,
    trusted: set[str] | None = None,
) -> CommitmentVerification:
    """Checks that `files` are the committed content of the opened items (files matched to items by sha256)."""
    c, raw = load_doc(commitment_path, Commitment)
    opening, _ = load_doc(opening_path, CommitmentOpening)
    checks = [structure_check(c)]
    sig_checks, fp, trusted_ok = signature_check(c, raw, trusted)
    checks.extend(sig_checks)
    opened = open_items(c, opening)
    by_sha: dict[str, list[str]] = {}
    for f in files:
        if not f.is_file():
            raise QtError("QT-INPUT-001", f"{f}: file not found")
        by_sha.setdefault(sha256_file(f), []).append(str(f))
    used: set[str] = set()
    outcomes: list[RevealOutcome] = []
    for it, leaf_ok in opened:
        given = by_sha.get(it.sha256)
        if not leaf_ok:
            status = "leaf_mismatch"
        elif given:
            status = "match"
        elif files:
            status = "content_mismatch"
        else:
            status = "not_given"
        if given:
            used.update(given)
        outcomes.append(
            RevealOutcome(
                index=it.index, name=it.name, sha256=it.sha256, status=status, file=given[0] if given else None
            )
        )
    unmatched = sorted(str(f) for f in files if str(f) not in used)
    bad = [o for o in outcomes if o.status in ("leaf_mismatch", "content_mismatch")]
    if not opened:
        checks.append(Check(id="opening", status="fail", summary="the opening opens no item"))
    else:
        checks.append(
            Check(
                id="opening",
                status="fail" if any(o.status == "leaf_mismatch" for o in outcomes) else "pass",
                summary=f"{sum(o.status != 'leaf_mismatch' for o in outcomes)} of {len(outcomes)} opened item(s) "
                "match their leaf",
                details=[f"{o.index} {o.name}: {o.status}" for o in outcomes if o.status == "leaf_mismatch"],
            )
        )
    if files:
        checks.append(
            Check(
                id="content",
                status="fail" if bad or unmatched else "pass",
                summary=f"{sum(o.status == 'match' for o in outcomes)} of {len(outcomes)} opened item(s) match a "
                f"given file; {len(unmatched)} given file(s) match no opened item",
                details=[f"{o.index} {o.name}: {o.status}" for o in bad] + [f"unmatched: {u}" for u in unmatched],
            )
        )
    else:
        checks.append(Check(id="content", status="skip", summary="no files given: only the opening was checked"))
    status = "fail" if any(ch.status == "fail" for ch in checks) else "pass"
    return CommitmentVerification(
        commitment=VerifiedCommitment(
            path=str(commitment_path),
            root=c.root,
            label=c.label,
            committed_at=c.committed_at,
            n_items=c.n_items,
            signed=c.signature is not None,
            key_fingerprint=fp,
            key_trusted=trusted_ok,
        ),
        status=status,
        n_revealed=len(outcomes),
        revealed=outcomes,
        unmatched_files=unmatched,
        checks=checks,
    )


def partial_opening(opening: CommitmentOpening, names: list[str]) -> CommitmentOpening:
    """An opening of the named items only (in committed order): reveals those files and keeps the others hidden."""
    by_name = {it.name: it for it in opening.items}
    unknown = sorted(set(names) - set(by_name))
    if unknown:
        raise QtError(
            "QT-USAGE-001",
            f"no committed item named {', '.join(map(repr, unknown))}",
            f"items: {', '.join(it.name for it in opening.items)}",
        )
    wanted = set(names)
    return CommitmentOpening(root=opening.root, items=[it for it in opening.items if it.name in wanted])


def write_pair(c: Commitment, opening: CommitmentOpening, out: Path, opening_out: Path) -> None:
    """Writes the public commitment and the private opening (mode 0600)."""
    if out.resolve() == opening_out.resolve():
        raise QtError("QT-USAGE-001", "--out and --opening must be different files")
    write_json(c.model_dump(mode="json"), out)
    write_json(opening.model_dump(mode="json"), opening_out, private=True)


def default_opening_path(out: Path) -> Path:
    """`commitment.json` -> `commitment.opening.json`."""
    stem = out.name[: -len(".json")] if out.name.endswith(".json") else out.name
    return out.with_name(stem + ".opening.json")
