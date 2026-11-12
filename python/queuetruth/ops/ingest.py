"""`ingest`: source files to a normalized dataset."""

from __future__ import annotations

from typing import Any

from queuetruth.ops._call import _native, _validate
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


def ingest(spec: s.IngestSpec | dict[str, Any]) -> m.IngestSummary:
    """`ingest`: source files -> normalized dataset (every format of SPEC 4.2)."""
    return _native("ingest", _validate(s.IngestSpec, spec), m.IngestSummary)
