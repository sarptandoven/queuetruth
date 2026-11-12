"""`data` as an operation: local files of one or more days into one dataset (`queuetruth.data.add`). Fetching a
public day (`data fetch iex`) is not an operation yet. Its `OPERATIONS` join the operation table
(`queuetruth.ops.table`)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from queuetruth.ops.table import OperationDef, VerbDocument, ref_paths
from queuetruth.schemas.specs import PathRef, SpecModel


class DataSpec(SpecModel):
    """`data add`: the source files and the dataset directory `out`; `format`, `venue` and `date` (YYYY-MM-DD)
    override what is inferred, `symbols` keeps only those symbols."""

    files: Annotated[list[PathRef], Field(min_length=1)]
    format: str | None = None
    venue: str | None = None
    date: str | None = None
    symbols: list[str] | None = None
    out: str | None = None


def run_data(spec: DataSpec) -> Any:
    """`queuetruth.data.add` with the spec's fields."""
    from queuetruth.data import add
    from queuetruth.errors import QtError

    if spec.out is None:
        raise QtError("QT-CONFIG-001", "data: out (the dataset directory) is required")
    return add(
        ref_paths(spec.files), spec.out, format=spec.format, venue=spec.venue, date=spec.date, symbols=spec.symbols
    )


OPERATIONS = (
    OperationDef(
        "data",
        DataSpec,
        run_data,
        VerbDocument,
        "Order-level files of one or more days into one dataset, with the format, venue and dates inferred and the "
        "data-quality checks run; returns qt.data.",
        cost="minutes",
        out_name="dataset",
        tool="qt_data",
        title="Add data",
    ),
)
