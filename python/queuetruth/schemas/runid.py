"""Run identity (SPEC 7.3): execution-only field removal and `run_id` over RFC 8785 canonical JSON.

One implementation, in Rust: `run_id`, `identity_document` and `drop_v1_1_defaults` call `queuetruth._core`
(`qt_schema::run_id`), which also owns the 1.1 defaults table. `resolve` still runs here, once per ref of the identity
document. `schemas/run_id_kat.json` holds the known answers.

Schema 1.1 canonicalisation: the spec fields added in 1.1 (SPEC 3.11) are left out of the identity document when
they hold their default, so every spec that does not set them keeps its v0.1 `run_id`; a non-default value enters
the hash as an ordinary field. `V1_1_DEFAULTS` lists them per spec, the spec being recognised by a top-level key
that only it has (`sweep_gap_ns` = ReplaySpec, `write_fills` = ZooSpec, `markout_horizons_ns` = ScorecardSpec,
`fix` = LiveImportSpec, `format` = IngestSpec, `max_events` = WhySpec, `target` = ExportSpec, `gbdt` =
HazardFitSpec, `max_why` = ReportSpec, `window_open_s` = ShadowSampleSpec). `family` is shared by CalibrationSpec
and LiveCalibrationSpec; its entries name fields only LiveCalibrationSpec has, so they never touch a CalibrationSpec.
Summary echoes (`spec_echo`) keep the fields.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable
from typing import Any

import queuetruth.integrity as _integrity

#: Keys removed (at any depth) before hashing and in summary echoes.
EXECUTION_ONLY = ("out", "threads")
#: Key sets that identify a `DatasetRef` / `FileRef` object.
REF_KEYS = (frozenset({"path"}), frozenset({"dataset_id"}), frozenset({"upload_id"}), frozenset({"job_file"}))


#: Old names of the canonical JSON pieces, now in `queuetruth.integrity` (deprecated here; still working).
_MOVED = {
    "canonical_json": ("queuetruth.integrity.canonical_json", _integrity.canonical_json),
    "_format_float": ("queuetruth.integrity.format_float", _integrity.format_float),
    "_escape": ("queuetruth.integrity.escape_string", _integrity.escape_string),
    "_utf16_key": ("queuetruth.integrity.utf16_key", _integrity.utf16_key),
}


_V1_1: tuple[tuple[str, tuple[str, ...], Any], ...] | None = None


def _core() -> Any:
    from queuetruth import _core

    return _core


def _v1_1_defaults() -> tuple[tuple[str, tuple[str, ...], Any], ...]:
    """(discriminating top-level key, field path, default): the 1.1 spec fields dropped from the identity document
    when equal (as canonical JSON) to the default; read from `qt_schema::canon::V1_1_DEFAULTS`."""
    global _V1_1
    if _V1_1 is None:
        _V1_1 = tuple((k, tuple(path), d) for k, path, d in json.loads(_core().v1_1_defaults()))
    return _V1_1


def __getattr__(name: str) -> Any:
    if name == "V1_1_DEFAULTS":
        return _v1_1_defaults()
    if name in _MOVED:
        new, fn = _MOVED[name]
        warnings.warn(
            f"queuetruth.schemas.runid.{name} moved to {new}; the old name will be removed in a future release",
            DeprecationWarning,
            stacklevel=2,
        )
        return fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


def _json(spec: Any) -> str:
    """JSON text of a spec for `_core`, refusing what canonical JSON refuses: a non-string key or an unsupported value
    (TypeError), a NaN or infinite float, or an integer outside 64 bits (ValueError)."""

    def check(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if not isinstance(k, str):
                    raise TypeError(f"canonical JSON keys must be str, got {type(k).__name__}")
                check(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                check(x)
        elif isinstance(v, int) and not isinstance(v, bool) and not -(2**63) <= v < 2**64:
            raise ValueError(f"integer {v} is outside 64 bits")

    check(spec)
    return json.dumps(spec, allow_nan=False, ensure_ascii=False)


def _refs(spec: Any, out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The refs of the identity document, in document order (execution-only keys skipped)."""
    if isinstance(spec, dict):
        if is_ref(spec):
            out.append(spec)
        else:
            for k, v in spec.items():
                if k not in EXECUTION_ONLY:
                    _refs(v, out)
    elif isinstance(spec, (list, tuple)):
        for v in spec:
            _refs(v, out)
    return out


def _resolved(spec: Any, resolve: Callable[[dict[str, Any]], str]) -> str:
    return json.dumps([[r, resolve(r)] for r in _refs(drop_v1_1_defaults(spec), [])])


def drop_v1_1_defaults(spec: Any) -> Any:
    """A copy of `spec` without the 1.1 fields that hold their default (`V1_1_DEFAULTS`; a `*` path element applies
    to every item of a list); other values unchanged."""
    if not isinstance(spec, dict):
        return spec
    return json.loads(_core().drop_v1_1_defaults(_json(spec)))


def identity_document(spec: dict[str, Any], engine_version: str, resolve: Callable[[dict[str, Any]], str]) -> str:
    """Canonical JSON of `{"engine_version": v, "spec": s}` with execution-only fields and defaulted 1.1 fields
    removed and refs resolved."""
    text = _json(spec)
    return _core().identity_document(text, engine_version, _resolved(spec, resolve))


def run_id(spec: dict[str, Any], engine_version: str, resolve: Callable[[dict[str, Any]], str]) -> str:
    """First 16 hex chars of sha256 of the identity document (SPEC 7.3).

    Pass the fully defaulted spec (`Spec.model_validate(x).model_dump(mode="json")`) so that omitted and explicit
    defaults give the same id. `resolve` maps a ref object to the sha256 of what it names.
    """
    text = _json(spec)
    return _core().run_id_resolved(text, engine_version, _resolved(spec, resolve))
