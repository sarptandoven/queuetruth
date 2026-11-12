"""The operation table (plan section 5.3, step 8): one `OperationDef` per operation, declared next to its module.

Each module of `queuetruth.ops` that defines operations has an `OPERATIONS` tuple, and so does the `operation`
module of a verb's package (`queuetruth.check.operation`, `queuetruth.fit.operation`, ...; the verbs use the
operations of `queuetruth.ops`, so they declare theirs at home). `table()` imports every public module of
`queuetruth.ops` and every `queuetruth.<package>.operation` and collects them, so a new operation is one
declaration in its own module and nothing else.
`queuetruth.runner.run` runs any of them by name; the MCP server lists a tool for each one with a `tool` name, and
the service's job runners call the runner for theirs (`service_kind`).

An operation takes one spec model and returns one document. `fn` is called with the validated spec model and may
return a Pydantic document, a result object with `.doc` (the verbs: `check`, `fit`, `compare`, `data`, `atlas`,
`demo`) or a plain dict; the runner turns it into the JSON document.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from queuetruth.errors import QtError

#: How long one run takes on a laptop, roughly: `instant` (under a second), `seconds`, `minutes`, `hours`.
Cost = Literal["instant", "seconds", "minutes", "hours"]
COSTS: tuple[str, ...] = ("instant", "seconds", "minutes", "hours")
#: Whether the operation running now may reach the network. `queuetruth.runner.run` sets it from its context; an
#: operation that asks before it downloads (`check` without data, `atlas`, `demo`) passes it as its consent.
NETWORK_ALLOWED: ContextVar[bool] = ContextVar("queuetruth_network_allowed", default=False)


@dataclass(frozen=True)
class OperationDef:
    """One operation.

    `name` is the runner's name (`replay`, `check`); `spec` the input model; `fn(spec)` runs it; `document` the
    model of what it returns; `description` one line. `reads_local`, `writes` and `network` say what it may touch:
    local files, output files, the network. `needs_network(spec)` says whether this spec reaches the network (default:
    `network`), so a caller without network access can refuse it before it runs. `cost` is a rough class (`COSTS`).

    `out_field` is the spec field that names the output location (None: the operation writes no files). `out_name`
    is the file or directory the runner puts inside an output directory it chooses (`shadows.parquet` for shadow
    sampling, `dataset` for `data`); None means the directory itself. `run_id` says whether the core computes a
    `run_id` for this spec (the MCP run root reuses a finished run of the same `run_id`). `prepare` rewrites a spec
    dict before validation (replay expands bare order ids). `tool` and `title` name its MCP tool (None: not a tool);
    `service_kind` is its job kind on the HTTP service (None: not a service job)."""

    name: str
    spec: type[BaseModel]
    fn: Callable[[Any], Any]
    document: type[BaseModel]
    description: str
    reads_local: bool = True
    writes: bool = True
    network: bool = False
    cost: Cost = "seconds"
    out_field: str | None = "out"
    out_name: str | None = None
    run_id: bool = False
    needs_network: Callable[[Any], bool] | None = None
    prepare: Callable[[Any], Any] | None = None
    tool: str | None = None
    title: str | None = None
    service_kind: str | None = None

    def __post_init__(self) -> None:
        if self.cost not in COSTS:
            raise ValueError(f"operation {self.name!r}: cost {self.cost!r} is not one of {COSTS}")
        if self.out_field is not None and self.out_field not in self.spec.model_fields:
            raise ValueError(f"operation {self.name!r}: {self.spec.__name__} has no field {self.out_field!r}")
        if self.writes != (self.out_field is not None):
            raise ValueError(f"operation {self.name!r}: writes and out_field disagree")

    def uses_network(self, spec: BaseModel) -> bool:
        """Whether running this (validated) spec reaches the network."""
        if not self.network:
            return False
        return True if self.needs_network is None else bool(self.needs_network(spec))


class VerbDocument(BaseModel):
    """The document of a verb (`qt.check`, `qt.fit`, `qt.compare`, `qt.data`): its `schema` name and the answer
    line; the other fields are described in the verb's docs and kept as they are (`extra="allow"`)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: str = Field(alias="schema", pattern=r"^qt\.[a-z0-9_.]+$")


def _declaring_modules() -> list[str]:
    """`queuetruth.ops.<module>` for every public module of the package, then `queuetruth.<package>.operation` for
    every package of `queuetruth` that has one (found on disk, so no other package is imported)."""
    here = Path(__file__).resolve().parent
    root = here.parent
    mods = [f"queuetruth.ops.{m.name}" for m in pkgutil.iter_modules([str(here)]) if not m.name.startswith("_")]
    for m in pkgutil.iter_modules([str(root)]):
        if m.ispkg and not m.name.startswith("_") and (root / m.name / "operation.py").is_file():
            mods.append(f"queuetruth.{m.name}.operation")
    return sorted(x for x in mods if x != __name__)


def ref_path(ref: Any) -> str | None:
    """The path of an optional `{"path"}` reference (a `PathRef`, a dict or None)."""
    from queuetruth.ops._call import _path_of

    return None if ref is None else _path_of(ref, "path")


def ref_paths(refs: Any) -> list[str]:
    """The paths of a list of `{"path"}` references."""
    return [str(ref_path(r)) for r in refs]


def validate(op: OperationDef, spec: Any) -> BaseModel:
    """`spec` (a dict or a model) validated with `op.spec`; an invalid spec is `QT-CONFIG-001` with every error in
    `details`."""
    from queuetruth.ops._call import _validate

    return _validate(op.spec, spec)


@cache
def table() -> Mapping[str, OperationDef]:
    """Every operation by name, sorted by name: the `OPERATIONS` of every declaring module (module docstring). Two
    operations of one name, or two of one tool name, are an error."""
    found: dict[str, OperationDef] = {}
    tools: dict[str, str] = {}
    for mod in _declaring_modules():
        for op in getattr(importlib.import_module(mod), "OPERATIONS", ()):
            if op.name in found:
                raise ValueError(f"operation {op.name!r} is declared twice")
            if op.tool is not None:
                if op.tool in tools:
                    raise ValueError(f"tool {op.tool!r} is declared by {tools[op.tool]!r} and {op.name!r}")
                tools[op.tool] = op.name
            found[op.name] = op
    return dict(sorted(found.items()))


def get(name: str) -> OperationDef:
    """The operation `name` (`QT-USAGE-001` for an unknown one, naming the known ones)."""
    ops = table()
    if name not in ops:
        raise QtError("QT-USAGE-001", f"unknown operation {name!r}", f"one of: {', '.join(ops)}")
    return ops[name]


def by_tool(tool: str) -> OperationDef | None:
    """The operation whose MCP tool is `tool`, if any."""
    return next((op for op in table().values() if op.tool == tool), None)


def by_service_kind(kind: str) -> OperationDef | None:
    """The operation behind the service job kind `kind`, if any."""
    return next((op for op in table().values() if op.service_kind == kind), None)


__all__ = [
    "COSTS",
    "NETWORK_ALLOWED",
    "Cost",
    "OperationDef",
    "VerbDocument",
    "by_service_kind",
    "by_tool",
    "get",
    "ref_path",
    "ref_paths",
    "table",
    "validate",
]
