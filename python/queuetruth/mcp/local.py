"""Read-only access to local output directories for the MCP local tools (SPEC 4.6, tools marked L).

Every path argument is resolved against the root (`QT_MCP_ROOT`, default the working directory); after resolving
symlinks (`os.path.realpath`) it must stay inside the root, else `QT-CONFIG-001`. The same holds for every path found
*inside* a directory argument: summary candidates, listed documents and the files of a why-trace dataset are resolved
one by one, and a child that is a symlink to somewhere outside the root is refused (security review L3). Files are
opened through their resolved path. Nothing here writes: files are opened read-only and `_core.why` runs with
`out = null`. A summary is any JSON file whose top-level `schema` names a `qt.*` document of
SPEC 3.10; it is validated with the shared Pydantic model before it is returned unchanged.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from queuetruth.errors import QtError
from queuetruth.schemas import specs as sp
from queuetruth.schemas.models import DOC_MODELS, parse_document

ENV_ROOT = "QT_MCP_ROOT"
MAX_JSON_BYTES = 64 << 20
MAX_DEPTH = 6
MAX_SCANNED_FILES = 20_000
MAX_ENTRIES = 2_000
PREFERRED_SUMMARIES = ("summary.json", "calibration.json", "model.json", "report.json", "manifest.json")
_SCHEMA_PREFIX = re.compile(rb'^\s*\{\s*"schema"\s*:\s*"(qt\.[a-z0-9_]+)"')


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON constant {name}")


class LocalOutputs:
    """Resolves and reads output directories under one root."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        base = root if root is not None else os.environ.get(ENV_ROOT) or os.getcwd()
        self.root = Path(base).expanduser().resolve()

    def resolve(self, path: str) -> Path:
        """Absolute path of `path` (relative paths are taken from the root); must stay inside the root."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise QtError("QT-USAGE-001", "path must be a non-empty string")
        p = Path(path).expanduser()
        full = self.inside(p if p.is_absolute() else self.root / p)
        if full is None:
            raise QtError("QT-CONFIG-001", f"path {path!r} is outside {ENV_ROOT} ({self.root})")
        return full

    def inside(self, path: Path) -> Path | None:
        """The real path of `path` (every symlink resolved) when it lies inside the root, else None."""
        real = Path(os.path.realpath(path))
        return real if real == self.root or self.root in real.parents else None

    def contain(self, path: Path) -> Path:
        """`inside`, raising `QT-CONFIG-001` for a path (typically a symlinked child) that leaves the root."""
        real = self.inside(path)
        if real is None:
            raise QtError("QT-CONFIG-001", f"{path.name!r} resolves outside {ENV_ROOT} ({self.root})")
        return real

    def rel(self, path: Path) -> str:
        """`path` relative to the root, POSIX separators."""
        return path.relative_to(self.root).as_posix() or "."

    @staticmethod
    def sniff_schema(path: Path) -> str | None:
        """The `schema` of a JSON document when it is the first key (cheap prefix read), else None."""
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
        except OSError:
            return None
        match = _SCHEMA_PREFIX.match(head)
        if match is None:
            return None
        name = match.group(1).decode()
        return name if name in DOC_MODELS else None

    def load_document(self, path: Path) -> dict[str, Any]:
        """Parses and validates one `qt.*` JSON document under the root; returns it unchanged."""
        path = self.contain(path)
        if not path.is_file():
            raise QtError("QT-INPUT-001", f"{self.rel(path)}: not a file")
        if path.stat().st_size > MAX_JSON_BYTES:
            raise QtError("QT-INPUT-002", f"{self.rel(path)}: larger than {MAX_JSON_BYTES} bytes")
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f, parse_constant=_reject_constant)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            raise QtError("QT-INPUT-002", f"{self.rel(path)}: not valid JSON: {e}") from e
        if not isinstance(doc, dict) or doc.get("schema") not in DOC_MODELS:
            raise QtError("QT-INPUT-002", f"{self.rel(path)}: not a qt.* document (missing or unknown `schema`)")
        try:
            parse_document(doc)
        except ValidationError as e:
            err = e.errors()[0]
            loc = ".".join(str(x) for x in err.get("loc", ()))
            raise QtError("QT-INPUT-002", f"{self.rel(path)}: invalid {doc['schema']}: {loc}: {err['msg']}") from e
        return doc

    def read_summary(self, path: str) -> dict[str, Any]:
        """A summary file, or the summary of an output directory (`summary.json` first, else the only document)."""
        full = self.resolve(path)
        if full.is_dir():
            for name in PREFERRED_SUMMARIES:
                cand = full / name
                if cand.is_symlink() or cand.exists():
                    real = self.contain(cand)  # a symlinked child must not lead outside the root
                    if real.is_file() and self.sniff_schema(real):
                        return self.load_document(real)
            docs = sorted(
                real
                for p in full.glob("*.json")
                if (real := self.inside(p)) is not None and real.is_file() and self.sniff_schema(real)
            )
            if len(docs) != 1:
                found = ", ".join(self.rel(p) for p in docs) or "none"
                raise QtError(
                    "QT-INPUT-001",
                    f"{self.rel(full)}: expected one qt.* summary, found {found}",
                    "pass the path of one JSON file (see qt_local_list_outputs)",
                )
            return self.load_document(docs[0])
        if not full.exists():
            raise QtError("QT-INPUT-001", f"{self.rel(full)}: no such file or directory")
        return self.load_document(full)

    def list_outputs(self, path: str) -> dict[str, Any]:
        """`{entries: [{path, schema}]}`: every `qt.*` JSON document under `path` (depth <= 6, sorted)."""
        full = self.resolve(path)
        if not full.exists():
            raise QtError("QT-INPUT-001", f"{self.rel(full)}: no such file or directory")
        if full.is_file():
            schema = self.sniff_schema(full)
            return {"entries": [{"path": self.rel(full), "schema": schema}] if schema else []}
        entries: list[dict[str, str]] = []
        scanned = 0
        base_depth = len(full.parts)
        for dirpath, dirnames, filenames in os.walk(full, followlinks=False):
            here = Path(dirpath)
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and len(here.parts) - base_depth < MAX_DEPTH
            )
            for name in sorted(filenames):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                scanned += 1
                if scanned > MAX_SCANNED_FILES:
                    break
                real = self.inside(here / name)
                if real is None or not real.is_file():  # symlinks leading outside the root are skipped
                    continue
                schema = self.sniff_schema(real)
                if schema:
                    entries.append({"path": self.rel(here / name), "schema": schema})
        entries.sort(key=lambda e: e["path"])
        return {"entries": entries[:MAX_ENTRIES]}

    def check_tree(self, top: Path) -> None:
        """Raises `QT-CONFIG-001` when any entry under `top` is a symlink that resolves outside the root (the engine
        reads every file of a dataset directory)."""
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            for name in (*dirnames, *filenames):
                p = Path(dirpath) / name
                if p.is_symlink():
                    self.contain(p)

    def why(self, dataset_path: str, shadows_path: str, shadow_id: str, run: Callable[[str], str]) -> dict[str, Any]:
        """Runs the why-trace for one shadow (`run` = `_core.why`) with `out = null`; returns `qt.why_trace`."""
        dataset = self.resolve(dataset_path)
        shadows = self.resolve(shadows_path)
        if not dataset.is_dir():
            raise QtError("QT-INPUT-001", f"{self.rel(dataset)}: dataset directory not found")
        if not shadows.is_file():
            raise QtError("QT-INPUT-001", f"{self.rel(shadows)}: shadows file not found")
        self.check_tree(dataset)
        spec = sp.WhySpec(
            dataset=sp.PathRef(path=str(dataset)), shadows=sp.PathRef(path=str(shadows)), shadow_id=shadow_id, out=None
        )
        text = run(spec.model_dump_json())
        try:
            doc = json.loads(text, parse_constant=_reject_constant)
        except ValueError as e:
            raise QtError("QT-ENGINE-001", f"why returned invalid JSON: {e}") from e
        if not isinstance(doc, dict) or doc.get("schema") != "qt.why_trace":
            raise QtError("QT-ENGINE-001", "why did not return a qt.why_trace document")
        try:
            parse_document(doc)
        except ValidationError as e:
            raise QtError("QT-ENGINE-001", f"why returned an invalid qt.why_trace: {e.errors()[0]['msg']}") from e
        return doc


def core_why(spec_json: str) -> str:
    """`queuetruth._core.why` with `QtCoreError` mapped to `QtError`."""
    try:
        from queuetruth import _core
    except ImportError as e:
        raise QtError("QT-ENGINE-001", "queuetruth._core is not available", "run `make develop`") from e
    try:
        return _core.why(spec_json)
    except _core.QtCoreError as e:
        if len(e.args) >= 2:
            raise QtError(str(e.args[0]), str(e.args[1])) from e
        raise QtError.from_display(str(e)) from e
    except NotImplementedError as e:
        raise QtError("QT-ENGINE-001", f"why is not implemented in this build: {e}") from e


__all__ = ["ENV_ROOT", "LocalOutputs", "core_why"]
