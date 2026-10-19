"""Scorecard, markouts, position series, referee comparison and live calibration (SPEC 4.3 `queuetruth.score`,
5.7, 5.8; audit rulings A1, A5, A8, A9, A10; also `calibrate_live` and fills-only coverage).

Public: `build_scorecard`, `compute_markouts`, `position_series`, `compare_referee` (SPEC 4.3 signatures, with
keyword-only extras), plus `run_referee` (`referee run` driver), `calibrate_live` / `LiveCalibrationSpec`
(`calibrate-live`), the L2 own-order removal helpers of `queuetruth.score.l2_own` (trader review E2; also for
`calibrate-live`) and the `ScoreRunner` seam used to reach the engines.
"""

from __future__ import annotations

from queuetruth.score.calibrate_live import LiveCalibrationSpec, calibrate_live
from queuetruth.score.l2_own import (
    OwnFill,
    OwnFreeResult,
    OwnOrder,
    own_free_l2_dataset,
    own_free_l2_from_live,
    own_orders_from_log,
)
from queuetruth.score.markouts import compute_markouts, position_series
from queuetruth.score.referee import compare_referee, run_referee
from queuetruth.score.runner import DefaultRunner, ScoreRunner
from queuetruth.score.scorecard import build_scorecard, gate_error

__all__ = [
    "DefaultRunner",
    "LiveCalibrationSpec",
    "OwnFill",
    "OwnFreeResult",
    "OwnOrder",
    "ScoreRunner",
    "build_scorecard",
    "calibrate_live",
    "compare_referee",
    "compute_markouts",
    "gate_error",
    "own_free_l2_dataset",
    "own_free_l2_from_live",
    "own_orders_from_log",
    "position_series",
    "run_referee",
]
