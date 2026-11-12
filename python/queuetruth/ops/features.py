"""`features`: per-shadow features at entry."""

from __future__ import annotations

from typing import Any

from queuetruth.ops._call import _native, _validate
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


def extract_features(spec: s.FeatureSpec | dict[str, Any]) -> m.FeatureSummary:
    """`features`."""
    return _native("extract_features", _validate(s.FeatureSpec, spec), m.FeatureSummary)


OPERATIONS = (
    OperationDef(
        "features",
        s.FeatureSpec,
        extract_features,
        m.FeatureSummary,
        "Per-shadow features at entry, for the hazard model.",
        cost="minutes",
    ),
)
