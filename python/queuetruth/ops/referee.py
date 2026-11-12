"""`referee fixtures` and `referee run`."""

from __future__ import annotations

from typing import Any

from queuetruth.ops._call import _call, _parse_doc, _validate
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


def referee_fixtures(out: str) -> list[str]:
    """`referee fixtures`: writes the bug-class fixtures under `out`; returns their directories."""
    doc = _parse_doc(m.FixturesSummary, _call("referee_fixtures", str(out)), "queuetruth._core.referee_fixtures")
    return [f.path for f in doc.fixtures]


def referee(spec: s.RefereeSpec | dict[str, Any]) -> m.RefereeReport:
    """`referee run` (`queuetruth.score.run_referee`)."""
    from queuetruth.score import run_referee

    return run_referee(_validate(s.RefereeSpec, spec))


OPERATIONS = (
    OperationDef(
        "referee",
        s.RefereeSpec,
        referee,
        m.RefereeReport,
        "A simulator checked against the bug-class fixtures.",
    ),
)
