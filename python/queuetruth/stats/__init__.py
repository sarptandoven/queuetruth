"""Statistics used by evaluation and the scorer (SPEC 4.3 `queuetruth.stats`, 5.5.7, 5.6, 5.7.5).

numpy/scipy only at runtime. `stable_seed`/`splitmix64` are the frozen port of `qt_types` (in `_seed`).
Semantics of each family are stated in its submodule:
- `paired`: McNemar, exact binomial, Wilcoxon, paired permutation.
- `clustered`: cluster-robust primary discordance test (Obuchowski, block bootstrap; audit ruling A10).
- `survival`: Kaplan-Meier, Aalen-Johansen, log-rank, Gray.
- `resampling`: block bootstrap. `multiplicity`: Holm, BH, BY. `equivalence`: TOST.
- `calibration`: Brier, log loss, Murphy, reliability, HL, Spiegelhalter, calibration slope/intercept.
- `discrimination`: Uno C, time-dependent AUC (improper).
"""

from __future__ import annotations

from queuetruth.stats._seed import splitmix64, stable_seed
from queuetruth.stats._types import CI, McNemarResult, StepFunction, TestResult, WilcoxonResult
from queuetruth.stats.calibration import (
    bins,
    brier,
    calibration_slope_intercept,
    hosmer_lemeshow,
    log_loss,
    murphy,
    reliability_table,
    spiegelhalter_z,
)
from queuetruth.stats.clustered import ClusteredDiscordance, clustered_mcnemar, obuchowski
from queuetruth.stats.discrimination import auc_at, uno_c_index
from queuetruth.stats.equivalence import tost_from_ci, tost_p, tost_p_bootstrap
from queuetruth.stats.multiplicity import adjust, benjamini_hochberg, benjamini_yekutieli, holm
from queuetruth.stats.paired import binom_exact_two_sided, mcnemar, paired_permutation, wilcoxon_signed_rank
from queuetruth.stats.resampling import (
    block_counts,
    block_labels,
    block_replicates,
    bootstrap_p_two_sided,
    bootstrap_rng,
    bootstrap_se,
    bootstrap_seed,
    day_block_bootstrap,
    percentile_ci,
)
from queuetruth.stats.survival import aalen_johansen, gray_test, kaplan_meier, logrank

__all__ = [
    "CI",
    "ClusteredDiscordance",
    "McNemarResult",
    "StepFunction",
    "TestResult",
    "WilcoxonResult",
    "aalen_johansen",
    "adjust",
    "auc_at",
    "benjamini_hochberg",
    "benjamini_yekutieli",
    "binom_exact_two_sided",
    "bins",
    "block_counts",
    "block_labels",
    "block_replicates",
    "bootstrap_p_two_sided",
    "bootstrap_rng",
    "bootstrap_se",
    "bootstrap_seed",
    "brier",
    "calibration_slope_intercept",
    "clustered_mcnemar",
    "day_block_bootstrap",
    "gray_test",
    "holm",
    "hosmer_lemeshow",
    "kaplan_meier",
    "log_loss",
    "logrank",
    "mcnemar",
    "murphy",
    "obuchowski",
    "paired_permutation",
    "percentile_ci",
    "reliability_table",
    "spiegelhalter_z",
    "splitmix64",
    "stable_seed",
    "tost_from_ci",
    "tost_p",
    "tost_p_bootstrap",
    "uno_c_index",
    "wilcoxon_signed_rank",
]
