"""`ingest`: source files to a normalized dataset."""

from __future__ import annotations

from typing import Any

from queuetruth.ops._call import _native, _validate
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


def ingest(spec: s.IngestSpec | dict[str, Any]) -> m.IngestSummary:
    """`ingest`: source files -> normalized dataset (every format of SPEC 4.2)."""
    return _native("ingest", _validate(s.IngestSpec, spec), m.IngestSummary)


OPERATIONS = (
    OperationDef(
        "ingest",
        s.IngestSpec,
        ingest,
        m.IngestSummary,
        "Source files to a normalized order-level dataset.",
        cost="minutes",
        service_kind="ingest",
    ),
)
