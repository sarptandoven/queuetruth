"""Canonical JSON: the one implementation, in each byte form that is already published.

Every digest queuetruth publishes (run ids, certificates, ledger entries, forecast and sandbox chains, atlas leaves
and roots, request hashes) hashes the canonical JSON of a document. The forms differ in bytes, so each one keeps its
name and its exact output; pick the form that the document's contract names, never a new one.

- `jcs` (default): RFC 8785 (SPEC 7.3). Keys sorted by UTF-16 code units, no whitespace, numbers in ECMAScript
  form (`1e+21`, `1e-7`, `-0.0` as `0`), integers exact, strings escaped as RFC 8785 says (only `"`, `\\`, and
  control characters; the rest verbatim). Non-finite floats raise `ValueError`, non-string keys `TypeError`. Run
  ids, certificates, the ledger, forecast ledger and sandbox log use it. `qt_schema::canonical_json` is its Rust
  twin (`schemas/run_id_kat.json` checks both).
- `compact`: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. Python float repr (`-0.0`,
  `1e-07`), keys sorted by code point, NaN and infinity written as `NaN` / `Infinity`. Request hashes of the test
  client and ledger query values use it.
- `atlas-v1`: `compact`, refusing NaN and infinity (`ValueError`). The atlas pipeline (`scripts/scale`) hashes unit
  specs, run ids and Merkle leaves with it; every published day root and atlas root depends on these bytes.
- `compact-ascii`: `compact` with `ensure_ascii=True` (non-ASCII as `\\uXXXX`). The scales of a canonical live log.
- `sorted`: `json.dumps(sort_keys=True)` with the default separators (`", "`, `": "`) and `ensure_ascii=True`.
  Reference identities of the scorer (`queuetruth.score._io`) and the nightly environment digests use it.

`canonical_bytes` is the UTF-8 encoding of `canonical_json`.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any, Literal

Form = Literal["jcs", "compact", "atlas-v1", "compact-ascii", "sorted"]
FORMS: tuple[str, ...] = ("jcs", "compact", "atlas-v1", "compact-ascii", "sorted")

#: The C encoder's string escaping (`json.encoder.encode_basestring`) is exactly RFC 8785's: `"` and `\\` escaped,
#: `\\b \\f \\n \\r \\t` short, other control characters as `\\u00xx`, everything else verbatim. It is much faster
#: than a Python loop, and ledgers hash every entry twice. `tests/integrity` checks it against the RFC rule.
_encode_str = json.encoder.encode_basestring  # type: ignore[attr-defined]


def format_float(x: float) -> str:
    """ECMAScript Number::toString of a finite float (shortest round-trip digits), as RFC 8785 writes numbers."""
    if not math.isfinite(x):
        raise ValueError(f"non-finite number {x!r} is not allowed in canonical JSON")
    if x == 0.0:
        return "0"
    sign = "-" if x < 0 else ""
    tup = Decimal(repr(abs(x))).as_tuple()
    digits = "".join(str(d) for d in tup.digits).lstrip("0")
    exp = int(tup.exponent)
    stripped = digits.rstrip("0")
    exp += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = k + exp  # value = 0.digits * 10^n
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        es = f"+{e}" if e >= 0 else str(e)
        out = digits + "e" + es if k == 1 else digits[0] + "." + digits[1:] + "e" + es
    return sign + out


def escape_string(s: str) -> str:
    """A JSON string literal escaped as RFC 8785 says (the reference rule; `jcs` uses the equal C encoder)."""
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def utf16_key(s: str) -> bytes:
    """Sort key of an object member name under RFC 8785 (UTF-16 code units)."""
    return s.encode("utf-16-be")


def _jcs(value: Any) -> str:
    if isinstance(value, str):
        return _encode_str(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_float(value)
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise TypeError(f"object keys must be strings, got {k!r}")
        items = sorted(value.items(), key=lambda kv: utf16_key(kv[0]))
        return "{" + ",".join(_encode_str(k) + ":" + _jcs(v) for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs(v) for v in value) + "]"
    raise TypeError(f"value of type {type(value).__name__} is not JSON")


def _check_finite(x: Any) -> None:
    if isinstance(x, float) and not math.isfinite(x):
        raise ValueError("canonical_json: NaN / inf are not allowed")
    if isinstance(x, dict):
        for v in x.values():
            _check_finite(v)
    elif isinstance(x, list | tuple):
        for v in x:
            _check_finite(v)


def canonical_json(value: Any, form: Form = "jcs") -> str:
    """The canonical JSON text of `value` in `form` (module docstring)."""
    if form == "jcs":
        return _jcs(value)
    if form == "compact":
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if form == "atlas-v1":
        _check_finite(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if form == "compact-ascii":
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if form == "sorted":
        return json.dumps(value, sort_keys=True)
    raise ValueError(f"form must be one of {FORMS}, got {form!r}")


def canonical_bytes(value: Any, form: Form = "jcs") -> bytes:
    """`canonical_json(value, form)` as UTF-8."""
    return canonical_json(value, form).encode("utf-8")


def value_text(value: Any) -> str:
    """A value as text inside a rendered claim: strings verbatim, everything else `jcs` canonical JSON."""
    if isinstance(value, str):
        return value
    return _jcs(value)


__all__ = [
    "FORMS",
    "Form",
    "canonical_bytes",
    "canonical_json",
    "escape_string",
    "format_float",
    "utf16_key",
    "value_text",
]
