"""`fit-hazard`: the hazard model of `queuetruth.model`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from queuetruth.ledger import recorded as _ledger_recorded
from queuetruth.ops._call import _validate
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


@_ledger_recorded("fit-hazard")
def fit_hazard(spec: s.HazardFitSpec | dict[str, Any]) -> m.HazardModelCard:
    """`fit-hazard` (`queuetruth.model.fit_hazard`)."""
    from queuetruth.model import fit_hazard as _fit

    sp = _validate(s.HazardFitSpec, spec)
    # SPEC 4.4 `--features DIR`: a features output directory resolves to its `features.parquet`
    path = getattr(sp.features, "path", None)
    if path is not None and Path(path).is_dir() and (Path(path) / "features.parquet").is_file():
        sp = sp.model_copy(update={"features": s.PathRef(path=str(Path(path) / "features.parquet"))})
    return _fit(sp)


OPERATIONS = (
    OperationDef(
        "fit_hazard",
        s.HazardFitSpec,
        fit_hazard,
        m.HazardModelCard,
        "The hazard model fitted on extracted features; returns its model card.",
        cost="minutes",
    ),
)
