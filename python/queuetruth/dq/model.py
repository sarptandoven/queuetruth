"""`qt.dq_spec` and `qt.dq_report`: the spec and the report document of `queuetruth dq`.

The models are registered in `queuetruth.schemas` (schema 1.2: `DqSpec` in `specs.py`, `DqReport` and its parts in
`models.py`; JSON Schemas in `schemas/json/` via `make schemas`); this module re-exports them with the status
helpers of the checks.
"""

from __future__ import annotations

import json
from typing import Literal

from queuetruth.schemas.models import DqCheck, DqCounts, DqDataset, DqEvidence, DqReport
from queuetruth.schemas.models import DqStatus as Status
from queuetruth.schemas.specs import DQ_DEFAULT_MAX_EVIDENCE as DEFAULT_MAX_EVIDENCE
from queuetruth.schemas.specs import DQ_DEFAULT_SESSION_TOLERANCE_NS as DEFAULT_SESSION_TOLERANCE_NS
from queuetruth.schemas.specs import DQ_DEFAULT_STALE_NS as DEFAULT_STALE_NS
from queuetruth.schemas.specs import DqSpec

#: Check statuses, best to worst (`skip` = not applicable or not run; never worse than `pass`).
STATUS_ORDER: dict[str, int] = {"skip": 0, "pass": 1, "warn": 2, "fail": 3}
FailOn = Literal["warn", "fail", "never"]


def worst(statuses: list[str]) -> str:
    """The worst of `statuses` (`pass` for none or only skips)."""
    w = max((STATUS_ORDER[s] for s in statuses), default=1)
    return {0: "pass", 1: "pass", 2: "warn", 3: "fail"}[w]


def gate(status: str, fail_on: str) -> bool:
    """Whether a report `status` fails the gate `fail_on`."""
    if fail_on == "never":
        return False
    return STATUS_ORDER[status] >= STATUS_ORDER[fail_on]


def json_schemas() -> dict[str, str]:
    """{file name: JSON Schema text} of `qt.dq_report` and `qt.dq_spec`, exactly as `make schemas` writes them to
    `schemas/json/`."""
    from queuetruth.schemas.export import json_schema

    return {
        f"{name}.schema.json": json.dumps(json_schema(name, model), indent=2, ensure_ascii=False) + "\n"
        for name, model in (("qt.dq_report", DqReport), ("qt.dq_spec", DqSpec))
    }


__all__ = [
    "DEFAULT_MAX_EVIDENCE",
    "DEFAULT_SESSION_TOLERANCE_NS",
    "DEFAULT_STALE_NS",
    "STATUS_ORDER",
    "DqCheck",
    "DqCounts",
    "DqDataset",
    "DqEvidence",
    "DqReport",
    "DqSpec",
    "FailOn",
    "Status",
    "gate",
    "json_schemas",
    "worst",
]
