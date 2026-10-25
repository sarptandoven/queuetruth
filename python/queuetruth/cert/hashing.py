"""File and tree hashing, normalized (determinism) digests and JSON pointers for certificates.

- `sha256_file`: streaming sha256 of a file's bytes.
- `tree_files`: every regular file under a directory, sorted by POSIX relative path, hashed. Symbolic links are
  refused (a certificate names bytes, not where a link happens to point today).
- `normalized_sha256`: what `verify-cert --rerun` compares (SPEC 7.2): a JSON file canonicalised (RFC 8785) without
  `timings` at any depth and without the execution-only fields (`out`, `threads`) of its top-level `spec` echo;
  any other file hashed as bytes. A JSON file that does not parse is hashed as bytes. With `unmap` (the inverse of
  `verify-cert --map`) the paths a re-executed run wrote under the mapped location are first written back as the
  certified ones: every JSON string that is such a path, in any other file every occurrence of a mapped prefix
  that starts and ends at a path-component boundary, and in JSON every recorded sha256 and size of such a file.
- `map_path`: whole-component prefix rewriting of a path (`--map OLD=NEW`).
- `json_pointer`: RFC 6901 lookup.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.schemas.runid import EXECUTION_ONLY, canonical_json

CHUNK = 1 << 20
#: Keys dropped at any depth before a JSON output is compared (SPEC 7.2: wall-clock values live only there).
VOLATILE_KEYS = ("timings",)


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of `data`."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file's bytes (read in 1 MiB chunks)."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e.strerror or e}") from None
    return h.hexdigest()


def tree_files(root: Path) -> list[tuple[str, int, str]]:
    """(relative POSIX path, bytes, sha256) of every regular file under `root`, sorted by path.

    Raises `QT-INPUT-002` for a symbolic link inside the tree and `QT-INPUT-001` when `root` is missing."""
    if not root.is_dir():
        raise QtError("QT-INPUT-001", f"{root}: directory not found")
    out: list[tuple[str, int, str]] = []
    for p in sorted(root.rglob("*"), key=lambda x: x.relative_to(root).as_posix()):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            raise QtError(
                "QT-INPUT-002",
                f"{root}: {rel} is a symbolic link",
                "certificates hash file contents; copy the target into place instead of linking it",
            )
        if p.is_file():
            out.append((rel, p.stat().st_size, sha256_file(p)))
    return out


def strip_volatile(doc: Any, top: bool = True) -> Any:
    """`doc` without `timings` at any depth and, at the top level, without `spec.out` / `spec.threads`."""
    if isinstance(doc, dict):
        out: dict[str, Any] = {}
        for k, v in doc.items():
            if k in VOLATILE_KEYS:
                continue
            if top and k == "spec" and isinstance(v, dict):
                v = {kk: vv for kk, vv in v.items() if kk not in EXECUTION_ONLY}
            out[k] = strip_volatile(v, top=False)
        return out
    if isinstance(doc, list):
        return [strip_volatile(v, top=False) for v in doc]
    return doc


def _has_non_finite(v: Any) -> bool:
    if isinstance(v, float):
        return not math.isfinite(v)
    if isinstance(v, dict):
        return any(_has_non_finite(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_non_finite(x) for x in v)
    return False


def map_path(path: str, mapping: list[tuple[str, str]]) -> str:
    """`path` with the first matching `OLD=NEW` prefix replaced (whole path components only)."""
    for old, new in mapping:
        o = old.rstrip("/")
        if path == o or path.startswith(o + "/"):
            return new.rstrip("/") + path[len(o) :]
    return path


def map_strings(doc: Any, mapping: list[tuple[str, str]], files: dict[str, tuple[str, int]] | None = None) -> Any:
    """A copy of a JSON document with every string that is a path under a mapped prefix rewritten. `files` maps the
    sha256 of a file to (sha256, size) once its paths are mapped back: an object recording that file's `sha256`
    (and `bytes`) gets the mapped-back values."""
    if not mapping and not files:
        return doc
    if isinstance(doc, str):
        return map_path(doc, mapping)
    if isinstance(doc, dict):
        out = {k: map_strings(v, mapping, files) for k, v in doc.items()}
        sha = doc.get("sha256")
        if files and isinstance(sha, str) and sha in files:
            out["sha256"], size = files[sha]
            if isinstance(doc.get("bytes"), int):
                out["bytes"] = size
        return out
    if isinstance(doc, list):
        return [map_strings(v, mapping, files) for v in doc]
    return doc


def map_bytes(data: bytes, mapping: list[tuple[str, str]]) -> bytes:
    """`data` with every occurrence of a mapped prefix (UTF-8) that ends at a path-component boundary rewritten."""
    for old, new in mapping:
        o = old.rstrip("/").encode("utf-8")
        if o and o in data:
            repl = new.rstrip("/").encode("utf-8")
            data = re.sub(
                rb"(?<![A-Za-z0-9_.~+-])" + re.escape(o) + rb"(?![A-Za-z0-9_.~+-])", lambda _m, r=repl: r, data
            )
    return data


def load_json_file(path: Path) -> Any:
    """A JSON file's document, or None when it does not parse (or holds a non-finite number)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return None if _has_non_finite(doc) else doc


def normalized_sha256(
    path: Path,
    raw_sha256: str | None = None,
    unmap: list[tuple[str, str]] | None = None,
    files: dict[str, tuple[str, int]] | None = None,
) -> str:
    """The determinism digest of one output file (see the module docstring). `files` maps the sha256 of a sibling
    output that embeds a mapped path to its (sha256, size) once mapped back (a JSON summary that records the digest
    of an HTML report naming the data path)."""
    unmap = unmap or []
    if path.suffix.lower() == ".json":
        doc = load_json_file(path)
        if doc is not None:
            return sha256_bytes(canonical_json(strip_volatile(map_strings(doc, unmap, files))).encode("utf-8"))
    if unmap:
        try:
            return sha256_bytes(map_bytes(path.read_bytes(), unmap))
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{path}: {e.strerror or e}") from None
    return raw_sha256 if raw_sha256 is not None else sha256_file(path)


def pointer_tokens(pointer: str) -> list[str]:
    """RFC 6901 reference tokens of a non-empty pointer (`/a/b~1c` -> `["a", "b/c"]`)."""
    if not pointer.startswith("/"):
        raise QtError("QT-USAGE-001", f"JSON pointer {pointer!r} must start with '/'")
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer[1:].split("/")]


def json_pointer(doc: Any, pointer: str) -> Any:
    """The value at `pointer` in `doc`; `KeyError` (with the failing token) when it does not exist."""
    node = doc
    for tok in pointer_tokens(pointer):
        if isinstance(node, dict):
            if tok not in node:
                raise KeyError(tok)
            node = node[tok]
        elif isinstance(node, list):
            if not tok.isdecimal() or (len(tok) > 1 and tok.startswith("0")) or int(tok) >= len(node):
                raise KeyError(tok)
            node = node[int(tok)]
        else:
            raise KeyError(tok)
    return node


def escape_token(token: str) -> str:
    """RFC 6901 escaping of one reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def canonical_value_text(value: Any) -> str:
    """A result value as it is printed inside a rendered claim: strings verbatim, everything else canonical JSON."""
    if isinstance(value, str):
        return value
    return canonical_json(value)
