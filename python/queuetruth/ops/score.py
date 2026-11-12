"""`score`: the sim-vs-live scorecard."""

from __future__ import annotations

from typing import Any

from queuetruth.ledger import recorded as _ledger_recorded
from queuetruth.ops._call import _validate
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


@_ledger_recorded("score")
def score(spec: s.ScorecardSpec | dict[str, Any], **kwargs: Any) -> m.ScorecardSummary:
    """`score` (`queuetruth.score.build_scorecard`; keyword extras `window_ns`, `match_window_ns`, `self_exclude`).
    Failed gates are reported in `verdict.gates_passed` (the CLI exits 1)."""
    from queuetruth.score import build_scorecard

    return build_scorecard(_validate(s.ScorecardSpec, spec), **kwargs)
