"""`report`: the HTML report."""

from __future__ import annotations

from typing import Any

from queuetruth.ops._call import _validate
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


def report(spec: s.ReportSpec | dict[str, Any]) -> m.ReportSummary:
    """`report` (`queuetruth.report.render`)."""
    from queuetruth.report import render

    return render(_validate(s.ReportSpec, spec))


OPERATIONS = (
    OperationDef(
        "report",
        s.ReportSpec,
        report,
        m.ReportSummary,
        "One HTML report over finished output directories.",
        out_name="report.html",
        service_kind="report",
    ),
)
