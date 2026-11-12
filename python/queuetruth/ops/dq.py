"""`dq`: the data-quality battery."""

from __future__ import annotations

from typing import Any


def dq(spec: Any) -> Any:
    """`dq`: the data-quality battery of `queuetruth.dq` over one dataset (and, with `compare`, a second dataset of
    the same sessions). `spec` is a `queuetruth.dq.DqSpec` or a dict of its fields; returns the `qt.dq_report`
    (`queuetruth.dq.DqReport`) and, with `spec.out`, writes `dq_report.json` and `dq_report.html` there."""
    from queuetruth.dq import run

    return run(spec)
