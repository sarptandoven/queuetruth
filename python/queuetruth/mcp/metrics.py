"""Static metric glossary served by the MCP tool `qt_explain_metric` (definitions follow SPEC 5.1.4, 5.5.7, 5.6,
5.7.5). `proper` is true only for strictly proper scoring rules; bias, test and discrimination statistics are false.

Audit rulings reflected here (SPEC rev 2026-10-19): A2, labels are outcome-independent
(the label at h exists iff the planned, administrative end is at or after entry + h, whatever the outcome; truth
curves are Aalen-Johansen CIFs, never 1 - KM); A10, the primary discordance test is cluster-robust
(`discordance_clustered`) and the exact McNemar test is secondary, assuming independent orders."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class MetricExplanation(BaseModel):
    """Output of `qt_explain_metric`: `{name, definition, formula, proper, notes}`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    definition: str
    formula: str
    proper: bool
    notes: str


def _m(name: str, definition: str, formula: str, proper: bool, notes: str = "") -> tuple[str, MetricExplanation]:
    return name, MetricExplanation(name=name, definition=definition, formula=formula, proper=proper, notes=notes)


METRICS: dict[str, MetricExplanation] = dict(
    [
        _m(
            "brier",
            "Mean squared error of a predicted fill probability against the observed fill indicator at a horizon.",
            "mean((p - y)^2) over label-eligible rows: the label at h exists iff the planned (administrative) end "
            "is at or after entry + h, whatever the outcome (A2)",
            True,
            "Zoo leaderboard `brier {h: v}` and scorer/model tables; lower is better.",
        ),
        _m(
            "logloss",
            "Negative mean log-likelihood of the observed fill indicator under the predicted probability.",
            "logloss = -mean(y ln pc + (1 - y) ln(1 - pc)), pc = clip(p, 1e-6, 1 - 1e-6), over the same "
            "label-eligible rows as brier (A2)",
            True,
            "Strictly proper; heavily penalizes confident wrong predictions.",
        ),
        _m(
            "fill_rate_bias",
            "Signed difference between predicted (or simulated) and observed fill rates.",
            "zoo leaderboard (= fill_rate_bias_aj): mean(p) over label-eligible never-cancel rows minus the "
            "Aalen-Johansen CIF_fill(h) (A2); qt.zoo_errors fill_rate_bias rows: mean(p) - mean(y) on the eligible "
            "rows of each stratum. Scorer: (c - b)/N; CIs use a block bootstrap (session days, or 30-minute blocks "
            "with fewer than 5 days)",
            False,
            "Positive = the model or simulator overfills (scorer: b = live_only, c = sim_only). `direction` is "
            "`sim_overfills` if the 95% CI lower bound > 0, `sim_underfills` if the upper bound < 0.",
        ),
        _m(
            "overfill_bias",
            "Discordant fill outcomes at h_max as a share of shadows (model filled iff p >= 0.5).",
            "overfill_bias = (c - b) / N, b = truth-only fills, c = model-only fills",
            False,
            "Primary test: cluster-robust `discordance_clustered` (A10); the exact McNemar p in the leaderboard's "
            "`mcnemar` field is secondary and assumes independent orders.",
        ),
        _m(
            "qty_bias",
            "Signed filled-quantity error relative to order quantity.",
            "zoo: sum(e_fill_qty - truth_filled_qty) / sum(qty); scorer: (sum sim_filled_qty - sum live_filled_qty)"
            " / sum qty",
            False,
        ),
        _m(
            "relative_overfill",
            "Simulated fill count relative to live fill count minus one.",
            "relative_overfill = (n11 + c) / (n11 + b) - 1 (null if n11 + b = 0)",
            False,
        ),
        _m(
            "mcnemar",
            "Paired test of discordant fill outcomes between two arms (live vs sim, or truth vs model).",
            "chi2_cc = (|b - c| - 1)^2 / (b + c); p_exact = min(1, 2 sum_{i<=min(b,c)} C(b+c, i) / 2^(b+c)); "
            "odds_ratio = c / b with Wald CI exp(ln(c/b) +- 1.96 sqrt(1/b + 1/c))",
            False,
            "secondary test, assumes independent orders; the primary discordance test is cluster-robust (A10). "
            "Golden: b = 3, c = 12 -> p_exact = 0.03515625. A non-rejection with a small b + c is not evidence of "
            "realism.",
        ),
        _m(
            "discordance_clustered",
            "Cluster-robust discordance test of fill outcomes between two arms, the primary discordance test (A10).",
            "d = (c - b)/N with a cluster-robust p (Obuchowski with t(K-1) when there are at least 5 clusters, else "
            "block bootstrap); reports clusters, design effect and effective n",
            False,
            "Computed by `queuetruth.stats.clustered_mcnemar`; scorecards carry it as the `discordance_clustered` "
            "test row.",
        ),
        _m(
            "ttf_log_ratio_median",
            "Median log ratio of model (or sim) to true (or live) time to first fill over jointly filled orders.",
            "median_i ln(max(ttf_model, 1us) / max(ttf_truth, 1us))",
            False,
            "Positive = the model fills later than the truth.",
        ),
        _m(
            "ttf_iad",
            "Integrated absolute difference between the model's fill-probability curve and the true fill CDF.",
            "sum_j |mean_i p(h_j) - CIF_AJ(h_j)| (h_j - h_{j-1}) / h_max over the horizons plus h_max (null without"
            " h_max rows), means over label-eligible rows; never 1 - KM",
            False,
        ),
        _m(
            "qtrack_mae",
            "Time-weighted mean absolute error of a model's queue-ahead estimate against the exact queue ahead.",
            "mean over each pair's live interval of |F_est - F_exact| / (level_qty_at_live + qty)",
            False,
            "Only with zoo trajectories (`write_trajectory`).",
        ),
        _m(
            "rcll",
            "Right-censored log-likelihood of the learned hazard model on test order-landmarks.",
            "rcll = mean(-ln P(observed)), P = h_c(K) S(tau_{K-1}) for an event of cause c in interval K, "
            "S(tau_J) if it survived, S(tau_{K-1}) if censored in K; probabilities floored at 1e-12",
            True,
            "Primary model-selection score with ipcw_logloss.",
        ),
        _m(
            "ipcw_logloss",
            "Inverse-probability-of-censoring weighted log loss of the fill and cancel CIFs at a horizon tau.",
            "-mean_i [1{e<=tau, fill} ln F_fill(tau) / G(e) + 1{e<=tau, cancel} ln F_cancel(tau) / G(e) "
            "+ 1{e>tau} ln S(tau) / G(tau)], G = KM of censoring floored at 0.05",
            True,
            "Alberge et al. 2024, eq. 2. Rows censored before tau get weight 0.",
        ),
        _m(
            "ipcw_brier",
            "IPCW Brier score for the fill cause at a horizon tau.",
            "BS(tau) = mean_i w_i (1{e_i<=tau, fill} - F_fill(tau|x_i))^2, w_i = 1/G(e_i) for an event by tau, "
            "1/G(tau) if e_i > tau, 0 if censored before tau",
            True,
        ),
        _m(
            "ibs",
            "Integrated IPCW Brier score over the model's horizon grid.",
            "ibs = sum_j BS(tau_j) (tau_j - tau_{j-1}) / tau_J",
            True,
        ),
        _m(
            "bss_vs_climatology",
            "Brier skill score of the learned model against an Aalen-Johansen climatology by stratum.",
            "bss = 1 - BS_model / BS_clim",
            False,
            "A skill score, not itself a proper scoring rule; CI by day-block bootstrap of test orders.",
        ),
        _m(
            "calib_slope",
            "Slope of a logistic recalibration of outcomes on logit(p); 1 = well calibrated.",
            "IPCW-weighted (rows censored before tau weigh 0); slope from logistic regression on logit(p); "
            "intercept from logit P(y) = a + logit(p), slope fixed at 1",
            False,
        ),
        _m(
            "calib_intercept",
            "Intercept of a logistic recalibration with the slope fixed at 1; 0 = calibrated in the large.",
            "IPCW-weighted (rows censored before tau weigh 0); slope from logistic regression on logit(p); "
            "intercept from logit P(y) = a + logit(p), slope fixed at 1",
            False,
        ),
        _m(
            "spiegelhalter_z",
            "Spiegelhalter's calibration z statistic.",
            "sum w(y-p)(1-2p) / sqrt(sum w^2 (1-2p)^2 p(1-p)), w = IPCW weights",
            False,
        ),
        _m(
            "hosmer_lemeshow",
            "Hosmer-Lemeshow goodness-of-fit p-value over probability bins.",
            "IPCW-weighted over 10 equal-count bins; hl_p with K df (held out), hl_p_et with 8 df (et parity)",
            False,
        ),
        _m(
            "c_index_uno",
            "Uno's concordance index at 10 s.",
            "share of comparable pairs ranked correctly, IPCW-weighted",
            False,
            "Discrimination only (improper); never used for model selection.",
        ),
        _m(
            "auc",
            "Time-dependent AUC of the fill CIF at a horizon.",
            "P(score_i > score_j | i filled by tau, j not)",
            False,
            "Discrimination only (improper).",
        ),
        _m(
            "markout",
            "Signed price move after a fill, in ticks and in currency.",
            "mo_ticks = d (M(t+h) - P) / tick; mo_ccy = d (M(t+h) - P) 10^price_exp qty 10^size_exp multiplier; "
            "d = +1 buy, -1 sell",
            False,
            "M = mid (or size-weighted microprice) of the prevailing quote; null past the end of data. The SEC "
            "realized spread of the taker is RS_h = 2 mo of the maker. 605-style (single-venue mid), not NBBO; also "
            "in bps of the fill price and net of fees.",
        ),
        _m(
            "rank0_share",
            "Share of checked executions whose filled order was first in FIFO at its price (exact replay oracle).",
            "rank0_share = #{rank = 0} / executions_checked",
            False,
            "`rank0_share_sweep_adjusted` ignores orders ahead that executed earlier in the same sweep.",
        ),
        _m(
            "queue_ahead",
            "Displayed quantity resting ahead of an order at its price under exact FIFO.",
            "sum of sizes of orders at the same price and side with earlier priority",
            False,
            "`queue_ahead_at_live` is measured when the shadow becomes live; deciles of it define `queue_decile`.",
        ),
        _m(
            "tost",
            "Two one-sided tests for equivalence of fill rates (or markouts) within a margin.",
            "equivalent iff the 90% bootstrap CI lies in [-margin, margin]; tost_p from the same bootstrap replicates"
            " = max(frac(rep <= -margin), frac(rep >= margin))",
            False,
            "One method for the flag and the p-value (audit minor M22); a non-rejection with few discordant orders is"
            " never evidence of realism.",
        ),
    ]
)


def explain(name: str) -> dict[str, Any]:
    """The explanation of `name` as a JSON object; unknown names raise `KeyError`."""
    return METRICS[name].model_dump()


__all__ = ["METRICS", "MetricExplanation", "explain"]
