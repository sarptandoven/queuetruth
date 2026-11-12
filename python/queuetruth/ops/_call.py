"""Shared plumbing of the operations: the compiled core and its errors, spec validation, spec negotiation with the
core, and output directories."""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from queuetruth.errors import QtError

S = TypeVar("S", bound=BaseModel)
D = TypeVar("D", bound=BaseModel)


def _core() -> Any:
    try:
        from queuetruth import _core as core
    except ImportError as e:  # pragma: no cover - only without a built extension
        raise QtError("QT-ENGINE-001", f"queuetruth._core is not available: {e}", "build it with `make develop`") from e
    return core


def _qt_error(e: BaseException) -> QtError:
    args = getattr(e, "args", ())
    code = getattr(e, "code", None) or (args[0] if len(args) >= 2 else "QT-ENGINE-001")
    message = getattr(e, "message", None) or (args[1] if len(args) >= 2 else str(e))
    return QtError(str(code), str(message))


def _call(name: str, *args: Any) -> Any:
    """`_core.<name>(*args)` with `QtCoreError` mapped to `QtError`."""
    core = _core()
    try:
        return getattr(core, name)(*args)
    except core.QtCoreError as e:
        raise _qt_error(e) from None


def _validate(model: type[S], spec: S | dict[str, Any]) -> S:
    if isinstance(spec, model):
        return spec
    try:
        if isinstance(spec, BaseModel):
            return model.model_validate(spec.model_dump(mode="json", by_alias=True))
        return model.model_validate(spec)
    except ValidationError as e:
        errors = json.loads(e.json(include_url=False))
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        msg = f"{model.__name__}: {loc + ': ' if loc else ''}{first.get('msg', 'invalid spec')}"
        if len(errors) > 1:
            msg += f" (+{len(errors) - 1} more)"
        raise QtError("QT-CONFIG-001", msg, details={"errors": errors}) from None


def _dump(spec: BaseModel) -> dict[str, Any]:
    return spec.model_dump(mode="json", by_alias=True)


def _parse_doc(doc: type[D], text: str, origin: str) -> D:
    try:
        return doc.model_validate_json(text)
    except ValidationError as e:
        errors = json.loads(e.json(include_url=False))
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise QtError(
            "QT-ENGINE-001",
            f"{origin} returned a document that does not match {doc.__name__}: {loc}: {first.get('msg', '')}",
            details={"errors": errors},
        ) from None


def _field_default(spec: BaseModel, key: str) -> Any:
    f = type(spec).model_fields.get(key)
    if f is None:
        return None
    v = f.get_default(call_default_factory=True)
    return v.model_dump(mode="json", by_alias=True) if isinstance(v, BaseModel) else v


def _call_spec(name: str, spec: BaseModel, *extra: Any, movable: tuple[str, ...] = ()) -> str:
    """`_core.<name>(spec JSON, *extra)`. A field the core's spec mirror does not know yet (Python schema ahead of
    the Rust build) is dropped when it holds its default, or when it is in `movable` (carried by `extra`); any other
    unknown field stays an error."""
    doc = _dump(spec)
    for _ in range(len(doc) + 1):
        try:
            return _call(name, json.dumps(doc), *extra)
        except QtError as e:
            hit = re.search(r"unknown field `([A-Za-z0-9_]+)`", e.message)
            key = hit.group(1) if hit else None
            if e.code != "QT-CONFIG-001" or key is None or key not in doc:
                raise
            if key not in movable and doc[key] != _field_default(spec, key):
                raise QtError(
                    "QT-CONFIG-001",
                    f"{key}: not supported by this queuetruth._core build (only its default is accepted)",
                    "rebuild the extension (make develop) or leave the field at its default",
                ) from None
            doc.pop(key)
    raise QtError("QT-ENGINE-001", f"queuetruth._core.{name}: spec negotiation did not converge")


def _native(name: str, spec: BaseModel, doc: type[D]) -> D:
    return _parse_doc(doc, _call_spec(name, spec), f"queuetruth._core.{name}")


def _path_of(ref: Any, what: str) -> str:
    p = getattr(ref, "path", None)
    if p is None and isinstance(ref, dict):
        p = ref.get("path")
    if not isinstance(p, str):
        raise QtError("QT-CONFIG-001", f"{what}: a local {{'path': ...}} reference is required here")
    return p


def _write_json(doc: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@contextmanager
def _out_dir(out: str | None, prefix: str) -> Iterator[Path]:
    """`out` as a directory, or a temporary directory removed afterwards."""
    if out:
        p = Path(out)
        p.mkdir(parents=True, exist_ok=True)
        yield p
        return
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield Path(tmp)


def _resolve_ref(ref: dict[str, Any]) -> str:
    from queuetruth.score._io import resolve_ref

    return resolve_ref(ref)
