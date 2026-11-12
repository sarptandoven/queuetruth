"""`dq`: the data-quality battery."""

from __future__ import annotations

from typing import Any

from queuetruth.ops.table import OperationDef


def dq(spec: Any) -> Any:
    """`dq`: the data-quality battery of `queuetruth.dq` over one dataset (and, with `compare`, a second dataset of
    the same sessions). `spec` is a `queuetruth.dq.DqSpec` or a dict of its fields; returns the `qt.dq_report`
    (`queuetruth.dq.DqReport`) and, with `spec.out`, writes `dq_report.json` and `dq_report.html` there."""
    from queuetruth.dq import run

    return run(spec)


def _dq_operations() -> tuple[OperationDef, ...]:
    from queuetruth.dq import DqReport, DqSpec

    return (
        OperationDef(
            "dq",
            DqSpec,
            dq,
            DqReport,
            "The data-quality battery over one dataset, or two datasets of the same sessions.",
        ),
    )


OPERATIONS = _dq_operations()
