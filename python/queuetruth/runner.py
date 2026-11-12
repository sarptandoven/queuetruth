"""One runner for every operation (plan section 5.3, step 8): `run(name, spec, out=..., context=...)`.

    from queuetruth.runner import RunContext, run
    res = run("replay", {"dataset": {"path": "data/day1"}, "shadows": {"path": "shadows.parquet"}}, out="out/replay")
    res.document["stats"]["records"], res.summary, res.files

It looks the operation up in the table (`queuetruth.ops.table`), checks every `{"path"}` reference against the
caller's allowed roots, validates the spec with the operation's model (an invalid spec is `QT-CONFIG-001`), puts the
outputs where the caller says, refuses a spec that needs the network when the caller does not allow it, runs the
operation with its warnings captured, and returns the JSON document, the answer line, the warnings and the files it
wrote. The HTTP service's job runners and the MCP server's run tools call it, so an operation behaves the same from
Python, the service and MCP. The command line keeps its own path for now (`queuetruth.cli`).

`RunContext` is the caller: `roots` (None: no containment, as for a Python caller on its own machine), `base` (where
relative paths start), `resolve_paths` (replace each path with its real path, so nothing can move under the run),
`path_refs_only` (refuse the service's `dataset_id`, `upload_id` and `job_file` references) and `allow_network`
(off by default: an operation that would download says so instead).
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from queuetruth.dataset import MANIFEST_FILE
from queuetruth.errors import QtError
from queuetruth.schemas.runid import is_ref

#: Most entries the symlink check of one directory walks (`QT-INPUT-002` beyond).
MAX_TREE_ENTRIES = 100_000
#: Reference fields that name an order-level dataset (a directory with `manifest.json`).
DATASET_FIELDS = ("dataset", "data", "compare")


def check_dataset_tree(
    top: Path, inside: Callable[[Path], Path | None], what: str, *, require_manifest: bool = True
) -> None:
    """A dataset directory: `manifest.json` must exist when `require_manifest` (`QT-INPUT-001`; checked first, so a
    broad directory such as the root is refused without walking it), and no entry may be a symlink for which `inside`
    returns None (`QT-CONFIG-001`). At most `MAX_TREE_ENTRIES` entries are walked (`QT-INPUT-002` beyond)."""
    if require_manifest and not (top / MANIFEST_FILE).is_file():
        raise QtError("QT-INPUT-001", f"{what}: not a dataset directory (no manifest.json)")
    seen = 0
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        for name in (*dirnames, *filenames):
            seen += 1
            if seen > MAX_TREE_ENTRIES:
                raise QtError("QT-INPUT-002", f"{what}: more than {MAX_TREE_ENTRIES} entries; not checked")
            p = Path(dirpath) / name
            if p.is_symlink() and inside(p) is None:
                raise QtError("QT-CONFIG-001", f"{what}: {p.name!r} resolves outside the allowed roots")


class Roots:
    """Allowed roots (real paths). Relative paths start at `base` (default: the first root). `env_hint` names the
    settings that set the roots, for the error hint."""

    def __init__(self, roots: Sequence[str | os.PathLike[str]], base: str | os.PathLike[str] | None = None) -> None:
        real = [Path(os.path.realpath(Path(r).expanduser())) for r in roots]
        if not real:
            raise ValueError("Roots needs at least one root")
        self.roots: tuple[Path, ...] = tuple(dict.fromkeys(real))
        self.root = Path(os.path.realpath(Path(base).expanduser())) if base is not None else self.roots[0]
        self.env_hint: str | None = None

    def inside(self, path: Path) -> Path | None:
        """The real path of `path` when it lies inside an allowed root, else None."""
        real = Path(os.path.realpath(path))
        return real if any(real == r or r in real.parents for r in self.roots) else None

    def resolve(self, path: str, what: str) -> Path:
        """Real path of an input (relative paths from `root`) inside the allowed roots, which must exist."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise QtError("QT-USAGE-001", f"{what}: path must be a non-empty string")
        p = Path(path).expanduser()
        real = self.inside(p if p.is_absolute() else self.root / p)
        if real is None:
            hint = f"allowed: {', '.join(str(r) for r in self.roots)}"
            raise QtError(
                "QT-CONFIG-001",
                f"{what}: {path!r} is outside the allowed roots",
                f"{hint} ({self.env_hint})" if self.env_hint else hint,
            )
        if not real.exists():
            raise QtError("QT-INPUT-001", f"{what}: {path!r}: no such file or directory")
        return real

    def contain_output(self, path: str, what: str) -> Path:
        """An output location (it may not exist yet): its nearest existing parent must lie inside the roots and
        the location itself must not be a symlink. Returns the path, absolute (relative paths from `root`)."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise QtError("QT-USAGE-001", f"{what}: path must be a non-empty string")
        p = Path(path).expanduser()
        full = p if p.is_absolute() else self.root / p
        probe = full
        while not probe.exists() and not probe.is_symlink() and probe != probe.parent:
            probe = probe.parent
        if self.inside(probe) is None or (full.is_symlink() and self.inside(full) is None):
            raise QtError("QT-CONFIG-001", f"{what}: {path!r} is outside the allowed roots")
        return full

    def check_tree(self, top: Path, what: str, *, require_manifest: bool = True) -> None:
        """`check_dataset_tree` against the allowed roots."""
        check_dataset_tree(top, self.inside, what, require_manifest=require_manifest)

    def rel(self, path: Path) -> str:
        """`path` relative to `root` when inside it, else absolute (POSIX separators)."""
        try:
            return path.relative_to(self.root).as_posix() or "."
        except ValueError:
            return path.as_posix()

    def refs(self, value: Any, loc: str, *, resolve: bool, path_refs_only: bool) -> Any:
        """`value` with every `{"path"}` reference checked against the roots (a directory: no symlink leading out;
        a dataset field: `manifest.json` too) and, with `resolve`, replaced by its real path. With
        `path_refs_only`, a service reference is `QT-CONFIG-001`."""
        if isinstance(value, dict):
            if is_ref(value):
                if set(value) != {"path"}:
                    if not path_refs_only:
                        return value
                    key = next(iter(value))
                    raise QtError(
                        "QT-CONFIG-001",
                        f'{loc}: {key} references are service ids; local runs take {{"path": ...}}',
                        "use the remote submit tools for service datasets and uploads",
                    )
                real = self.resolve(str(value["path"]), loc)
                if real.is_dir():
                    field_name = loc.rsplit(".", 1)[-1].split("[", 1)[0]
                    self.check_tree(real, loc, require_manifest=field_name in DATASET_FIELDS)
                return {"path": str(real) if resolve else value["path"]}
            kw = {"resolve": resolve, "path_refs_only": path_refs_only}
            return {k: self.refs(v, f"{loc}.{k}", **kw) for k, v in value.items()}
        if isinstance(value, list):
            return [
                self.refs(v, f"{loc}[{i}]", resolve=resolve, path_refs_only=path_refs_only) for i, v in enumerate(value)
            ]
        return value


@dataclass(frozen=True)
class RunContext:
    """Who runs the operation, and what it may touch (module docstring)."""

    roots: Sequence[str | os.PathLike[str]] | Roots | None = None
    base: str | os.PathLike[str] | None = None
    resolve_paths: bool = False
    path_refs_only: bool = False
    allow_network: bool = False

    def containment(self) -> Roots | None:
        """The allowed roots as a `Roots`, or None (no containment)."""
        if self.roots is None or isinstance(self.roots, Roots):
            return self.roots
        return Roots(self.roots, self.base)


@dataclass
class RunOutcome:
    """One run: the operation, its JSON `document`, the answer line (`summary`, when the result has one), the
    warnings it raised, the files it wrote (sorted) and its output location."""

    name: str
    document: dict[str, Any]
    summary: str | None = None
    warnings: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    out: Path | None = None


def _as_dict(spec: Any) -> dict[str, Any]:
    if isinstance(spec, BaseModel):
        return spec.model_dump(mode="json", by_alias=True)
    if not isinstance(spec, dict):
        raise QtError("QT-USAGE-001", "spec must be a JSON object")
    return dict(spec)


def document_of(result: Any, name: str) -> dict[str, Any]:
    """The JSON document of an operation's result: a Pydantic document, a result object with `.doc`, or a dict."""
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json", by_alias=True)
    doc = getattr(result, "doc", result)
    if isinstance(doc, BaseModel):
        return doc.model_dump(mode="json", by_alias=True)
    if isinstance(doc, dict):
        return doc
    raise QtError("QT-ENGINE-001", f"{name}: the operation returned {type(result).__name__}, not a document")


def summary_of(result: Any) -> str | None:
    """The answer line of a result (`.summary` of the shared result shape), if it has one."""
    s = getattr(result, "summary", None)
    return s if isinstance(s, str) and s else None


def produced_files(out: Path | None) -> list[Path]:
    """Every file at `out` (a file or a directory; symlinks are not followed), sorted."""
    if out is None or not out.exists() or out.is_symlink():
        return []
    if out.is_file():
        return [out]
    return sorted(p for p in out.rglob("*") if p.is_file() and not p.is_symlink())


def run(
    name: str,
    spec: BaseModel | dict[str, Any],
    *,
    out: str | os.PathLike[str] | None = None,
    context: RunContext | None = None,
) -> RunOutcome:
    """Runs the operation `name` on `spec` (module docstring). `out` is a directory for the outputs; it replaces
    the spec's own output field (with the operation's `out_name` inside it, such as `shadows.parquet`)."""
    from queuetruth.ops.table import NETWORK_ALLOWED, get, validate

    op = get(name)
    ctx = context or RunContext()
    roots = ctx.containment()
    data = _as_dict(spec)
    if roots is not None:
        data = roots.refs(data, "spec", resolve=ctx.resolve_paths, path_refs_only=ctx.path_refs_only)
    if op.prepare is not None:
        data = op.prepare(data)
    if out is not None:
        if op.out_field is None:
            raise QtError("QT-USAGE-001", f"{name}: this operation writes no files, so it takes no out")
        target = Path(out) / op.out_name if op.out_name else Path(out)
        data[op.out_field] = str(target)
    model = validate(op, data)
    where: Path | None = None
    if op.out_field is not None and getattr(model, op.out_field, None) is not None:
        loc = str(getattr(model, op.out_field))
        where = roots.contain_output(loc, f"spec.{op.out_field}") if roots is not None else Path(loc)
        if out is not None and op.out_name:
            where.parent.mkdir(parents=True, exist_ok=True)
    if op.uses_network(model) and not ctx.allow_network:
        raise QtError(
            "QT-CONFIG-001",
            f"{name}: this spec needs the network, which this caller does not allow",
            "give a local source in the spec (see its fields), or run it where the network is allowed",
        )
    token = NETWORK_ALLOWED.set(ctx.allow_network)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = op.fn(model)
    finally:
        NETWORK_ALLOWED.reset(token)
    if where is None:
        where = getattr(result, "out", None) if isinstance(getattr(result, "out", None), Path) else None
    seen: list[str] = []
    for w in caught:
        text = str(w.message)
        if text not in seen:
            seen.append(text)
    return RunOutcome(
        name=name,
        document=document_of(result, name),
        summary=summary_of(result),
        warnings=seen,
        files=produced_files(where),
        out=where,
    )


__all__ = [
    "DATASET_FIELDS",
    "MAX_TREE_ENTRIES",
    "Roots",
    "RunContext",
    "RunOutcome",
    "check_dataset_tree",
    "document_of",
    "produced_files",
    "run",
    "summary_of",
]
