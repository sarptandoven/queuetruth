"""Run identity (SPEC 7.3): RFC 8785 (JCS) canonical JSON, execution-only field removal and `run_id`.

Mirror of `qt_schema::canonical_json` / `qt_schema::run_id`; `schemas/run_id_kat.json` holds shared known answers.

Schema 1.1 canonicalisation: the spec fields added in 1.1 (SPEC 3.11) are left out of the identity document when
they hold their default, so every spec that does not set them keeps its v0.1 `run_id`; a non-default value enters
the hash as an ordinary field. `V1_1_DEFAULTS` lists them per spec, the spec being recognised by a top-level key
that only it has (`sweep_gap_ns` = ReplaySpec, `write_fills` = ZooSpec, `markout_horizons_ns` = ScorecardSpec,
`fix` = LiveImportSpec, `format` = IngestSpec, `max_events` = WhySpec, `target` = ExportSpec, `gbdt` =
HazardFitSpec, `max_why` = ReportSpec). `family` is shared by CalibrationSpec and LiveCalibrationSpec; its entries name
fields only LiveCalibrationSpec has, so they never touch a CalibrationSpec.
Summary echoes (`spec_echo`) keep the fields.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from decimal import Decimal
from typing import Any

#: Keys removed (at any depth) before hashing and in summary echoes.
EXECUTION_ONLY = ("out", "threads")
#: (discriminating top-level key, field path, default): 1.1 spec fields dropped from the identity document when
#: equal (as canonical JSON) to the default. Same table as `qt_schema::canon::V1_1_DEFAULTS`.
V1_1_DEFAULTS: tuple[tuple[str, tuple[str, ...], Any], ...] = (
    ("sweep_gap_ns", ("fill_rule",), "exact"),
    ("sweep_gap_ns", ("hidden_print_policy",), None),
    ("sweep_gap_ns", ("exclude_order_ids",), []),
    ("sweep_gap_ns", ("exclude_order_ids", "*", "session_date"), None),
    ("write_fills", ("amends",), None),
    ("write_fills", ("exclude_order_ids",), []),
    ("write_fills", ("exclude_order_ids", "*", "session_date"), None),
    ("max_events", ("exclude_order_ids",), []),
    ("max_events", ("exclude_order_ids", "*", "session_date"), None),
    ("target", ("force",), False),
    ("markout_horizons_ns", ("window_ns",), 60_000_000_000),
    ("markout_horizons_ns", ("match_window_ns",), 1_000_000),
    ("markout_horizons_ns", ("self_exclude",), True),
    ("markout_horizons_ns", ("markout_past_end",), None),
    ("fix", ("tz",), None),
    ("fix", ("fix", "venue_name"), None),
    ("fix", ("fix", "checksum"), "exclude"),
    ("fix", ("fix", "data_dictionary"), None),
    ("fix", ("fix", "session_venues"), {}),
    ("fix", ("fix", "instrument_tag"), None),
    ("format", ("open_s",), None),
    ("format", ("close_s",), None),
    ("gbdt", ("zoo_shadows",), None),
    ("max_why", ("leaderboard_sort_horizon_ns",), None),
    ("family", ("self_exclude",), True),
    ("family", ("match_window_ns",), 1_000_000),
)
#: Key sets that identify a `DatasetRef` / `FileRef` object.
REF_KEYS = (frozenset({"path"}), frozenset({"dataset_id"}), frozenset({"upload_id"}), frozenset({"job_file"}))


def _format_float(x: float) -> str:
    """ECMAScript Number::toString for a finite float (shortest round-trip digits)."""
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


def _escape(s: str) -> str:
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


def _utf16_key(s: str) -> bytes:
    return s.encode("utf-16-be")


def canonical_json(value: Any) -> str:
    """RFC 8785 canonical JSON: sorted keys (UTF-16 code units), no whitespace, ES number format, ints exact."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, str):
        return _escape(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise TypeError(f"object keys must be strings, got {k!r}")
        items = sorted(value.items(), key=lambda kv: _utf16_key(kv[0]))
        return "{" + ",".join(_escape(k) + ":" + canonical_json(v) for k, v in items) + "}"
    raise TypeError(f"value of type {type(value).__name__} is not JSON")


def is_ref(value: Any) -> bool:
    """True for a `DatasetRef` / `FileRef` object (exactly one of the keys path, dataset_id, upload_id, job_file)."""
    return isinstance(value, dict) and frozenset(value) in REF_KEYS


def spec_echo(spec: Any) -> Any:
    """The spec with execution-only fields (`out`, `threads`) removed at any depth; references kept as given."""
    if isinstance(spec, dict):
        if is_ref(spec):
            return dict(spec)
        return {k: spec_echo(v) for k, v in spec.items() if k not in EXECUTION_ONLY}
    if isinstance(spec, list):
        return [spec_echo(v) for v in spec]
    return spec


def _identity(spec: Any, resolve: Callable[[dict[str, Any]], str]) -> Any:
    if isinstance(spec, dict):
        if is_ref(spec):
            return {"sha256": resolve(spec)}
        return {k: _identity(v, resolve) for k, v in spec.items() if k not in EXECUTION_ONLY}
    if isinstance(spec, list):
        return [_identity(v, resolve) for v in spec]
    return spec


def _drop_path(node: Any, path: tuple[str, ...], default: Any) -> Any:
    """`node` with `path` removed where it holds `default` (`*` = every item of a list); copies what it changes."""
    if not path:
        return node
    head, rest = path[0], path[1:]
    if head == "*":
        return [_drop_path(x, rest, default) for x in node] if isinstance(node, list) else node
    if not isinstance(node, dict) or head not in node:
        return node
    out = dict(node)
    if rest:
        out[head] = _drop_path(node[head], rest, default)
    elif canonical_json(node[head]) == canonical_json(default):
        del out[head]
    return out


def drop_v1_1_defaults(spec: Any) -> Any:
    """A copy of `spec` without the 1.1 fields that hold their default (`V1_1_DEFAULTS`; a `*` path element applies
    to every item of a list); other values unchanged."""
    if not isinstance(spec, dict):
        return spec
    out = spec
    for disc, path, default in V1_1_DEFAULTS:
        if disc in spec:
            out = _drop_path(out, path, default)
    return dict(out)


def identity_document(spec: dict[str, Any], engine_version: str, resolve: Callable[[dict[str, Any]], str]) -> str:
    """Canonical JSON of `{"engine_version": v, "spec": s}` with execution-only fields and defaulted 1.1 fields
    removed and refs resolved."""
    return canonical_json({"engine_version": engine_version, "spec": _identity(drop_v1_1_defaults(spec), resolve)})


def run_id(spec: dict[str, Any], engine_version: str, resolve: Callable[[dict[str, Any]], str]) -> str:
    """First 16 hex chars of sha256 of the identity document (SPEC 7.3).

    Pass the fully defaulted spec (`Spec.model_validate(x).model_dump(mode="json")`) so that omitted and explicit
    defaults give the same id. `resolve` maps a ref object to the sha256 of what it names.
    """
    doc = identity_document(spec, engine_version, resolve)
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]
