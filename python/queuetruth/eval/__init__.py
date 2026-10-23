"""Zoo evaluation against exact truth and Python baselines (SPEC 4.3 `queuetruth.eval`, 5.6).

`zoo`: `assign_strata`, `evaluate_zoo` (qt.zoo_errors + leaderboard on `common_population`). `baselines`:
`baseline_predictions`, `join_features`. `leaderboard`: `rank_leaderboard` (by absolute fill-rate bias at a sort
horizon, default 60 s), `bias_sentence` (one plain-English line per model). Semantics are stated in each submodule.
"""

from __future__ import annotations

from queuetruth.eval.baselines import KINDS, baseline_predictions, join_features
from queuetruth.eval.leaderboard import (
    DEFAULT_SORT_HORIZON_NS,
    abort_shares,
    bias_sentence,
    rank_leaderboard,
    sort_horizon,
)
from queuetruth.eval.zoo import (
    aborted_shadows,
    assign_strata,
    common_population,
    evaluate_zoo,
    family,
    leaderboard_population,
    planned_life_table,
    without_aborted,
)

__all__ = [
    "DEFAULT_SORT_HORIZON_NS",
    "KINDS",
    "abort_shares",
    "aborted_shadows",
    "assign_strata",
    "baseline_predictions",
    "bias_sentence",
    "common_population",
    "evaluate_zoo",
    "family",
    "join_features",
    "leaderboard_population",
    "planned_life_table",
    "rank_leaderboard",
    "sort_horizon",
    "without_aborted",
]
