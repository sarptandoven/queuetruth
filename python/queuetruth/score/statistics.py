"""Scorecard statistics on the per-order table (SPEC 5.7.5).

N = matched scored orders; `n11` both, `n00` neither, `b` live_only, `c` sim_only. `fill_rate_bias = (c - b) / N`,
`relative_overfill = (n11 + c) / (n11 + b) - 1`, `qty_bias = (sum sim qty - sum live qty) / sum qty`; 95% CIs and
two-sided bootstrap p-values by block bootstrap (blocks = session date of `ts_entry`; fewer than 5 dates -> local
30-minute blocks of `ts_entry` and the warning `ci_block=30min`; `bootstrap.block` forces either), one RNG stream per
test id (`stats.bootstrap_seed(seed, test_id)`). McNemar on (b, c) overall and per queue decile / liquidity bucket
(deciles of `queue_ahead_at_entry` and terciles of `trade_vol_60s` within instrument-day, SPEC 5.6 construction).
Time to fill: `d = ln max(ttf_sim, 1us) - ln max(ttf_live, 1us)` over jointly filled orders (median with CI,
Wilcoxon); Aalen-Johansen `CIF_fill` per arm (cause 1 fill, 2 cancel/expire, 0 censored) at `ttf_horizons_ns`; paired
permutation of arm labels with `D = sum_h |CIF_sim(h) - CIF_live(h)|` (primary ttf test); log-rank on the
cause-specific fill hazard and Gray's test (descriptive: they assume independent samples). Markouts: mean per (arm,
horizon, ref) over fills; paired first-fill difference (sim minus live) over orders filled in both arms; sim-only
vs live-only first-fill markouts (first ref). Taker fills (book crossed the order, audit ruling A5) are excluded from
the maker markout aggregates and reported separately (`markout_mean_taker`, `taker_fills` rows). Markout rows that
carry `mo_bps` / `mo_net_ccy` (review F10) add maker means in bps of the fill price (`markout_mean_bps`, statistic
`mean_bps`) and in currency net of fees (`markout_mean_net_ccy`, statistic `mean_net_ccy`) per arm and `<ref>:<h>`,
with their own bootstrap streams (descriptive; no p-value).

Primary family (audit ruling A10, cluster-robust; Holm-adjusted): `stats.clustered_mcnemar` on the bootstrap blocks
(Obuchowski with >= 5 clusters, else the block bootstrap of `(c - b) / N`; drawn with the `fill_rate_bias` stream so
its bootstrap CI equals the bias CI) and the markout diff at 1 s (first ref). The exact McNemar `p_exact` is
reported (summary `mcnemar`, headline) but enters the secondary family with the note "assumes independent orders";
the `fill_rate_bias` bootstrap p is descriptive.
Every secondary p-value is BH/BY-adjusted. TOST (M22): the flag from the 90% bootstrap CI and `tost_p` from the same
replicates, `max(frac(rep <= -margin), frac(rep >= margin))` (fill rate and markout diff alike).

Fills-only sources (never-filled orders absent) make every statistic that needs unfilled orders not computable:
bias, McNemar (p), strata tests, equivalence, CIFs and the ttf permutation/log-rank/Gray tests are null and the
headline says so; markouts and the time-to-fill comparison conditional on fill are still computed.

Summary part `discordance_clustered` (schema 1.1, SPEC 3.11 #9) carries the clustered test natively (null when not
computable: fills-only source or no matched orders); the `qt.score_tests` row and the `clusters:` warning stay for
v0.1 readers.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from queuetruth import stats

DAY_NS = 86_400_000_000_000
HALF_HOUR_NS = 1_800_000_000_000
US = 1_000
ONE_S = 1_000_000_000
CLASSES = ("both", "neither", "live_only", "sim_only")
NOT_COMPUTABLE = "not computable: fills-only source"
#: Optional markout-row columns -> (`qt.score_tests` test_id, statistic, note) of their maker means (review F10).
EXTRA_MARKOUTS: dict[str, tuple[str, str, str]] = {
    "mo_bps": ("markout_mean_bps", "mean_bps", "maker fills; bps of the fill price"),
    "mo_net_ccy": (
        "markout_mean_net_ccy",
        "mean_net_ccy",
        "maker fills; currency, net of fees (positive fee = cost; sim fills at the live fee rate)",
    ),
}

Row = dict[str, Any]


def fin(x: Any) -> float | None:
    """Finite float or None."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def classify(live: bool, sim: bool) -> str:
    """Per-order class from (live_filled, sim_filled)."""
    if live and sim:
        return "both"
    if live:
        return "live_only"
    if sim:
        return "sim_only"
    return "neither"


def _cut(values: list[float | None], groups: list[Any], qs: Sequence[float], top: int) -> list[int | None]:
    out: list[int | None] = [None] * len(values)
    by: dict[Any, list[int]] = {}
    for i, g in enumerate(groups):
        if values[i] is not None:
            by.setdefault(g, []).append(i)
    for idx in by.values():
        x = np.array([float(values[i]) for i in idx])  # type: ignore[arg-type]
        q = np.quantile(x, qs, method="linear")
        k = np.clip(np.searchsorted(q, x, side="right"), 0, top)
        for i, v in zip(idx, k, strict=True):
            out[i] = int(v)
    return out


def assign_strata(orders: list[Row]) -> None:
    """Sets `queue_decile` (0..9) and `liquidity_bucket` (0..2) in place, within (instrument_id, session_date)."""
    groups = [(o.get("instrument_id"), o.get("session_date")) for o in orders]
    qa = [fin(o.get("queue_ahead_at_entry")) for o in orders]
    tv = [fin(o.get("trade_vol_60s")) for o in orders]
    dec = _cut(qa, groups, [i / 10 for i in range(1, 10)], 9)
    liq = _cut(tv, groups, [1 / 3, 2 / 3], 2)
    for o, d, b in zip(orders, dec, liq, strict=True):
        o["queue_decile"] = d
        o["liquidity_bucket"] = b


def block_labels(orders: list[Row], block: str) -> tuple[np.ndarray, str]:
    """Bootstrap block per order and the block kind (`day` or `30min`)."""
    dates = np.array([int(o.get("session_date") or 0) for o in orders], dtype=np.int64)
    kind = block
    if block == "auto":
        kind = "day" if np.unique(dates).size >= 5 else "30min"
    if kind == "day":
        return dates, "day"
    local = np.array(
        [int(o.get("local_ns", int(o["ts_entry"]) - int(o.get("session_date") or 0) * DAY_NS)) for o in orders],
        dtype=np.int64,
    )
    return dates * 64 + np.floor_divide(local, HALF_HOUR_NS), "30min"


@dataclass
class Assembly:
    """Statistics of a scorecard: summary parts, `qt.score_tests` rows and warnings."""

    parts: dict[str, Any]
    tests: list[Row]
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Test:
    test_id: str
    family: str
    stratum_kind: str
    stratum: str
    statistic: str
    value: float
    p_value: float | None = None
    ci_lo: float | None = None
    ci_hi: float | None = None
    n: int = 0
    note: str | None = None
    adjust: bool = False
    p_adj: float | None = None
    adj_method: str = "none"

    def row(self) -> Row:
        return {
            "test_id": self.test_id,
            "family": self.family,
            "stratum_kind": self.stratum_kind,
            "stratum": self.stratum,
            "statistic": self.statistic,
            "value": float("nan") if self.value is None else float(self.value),
            "p_value": fin(self.p_value),
            "p_adj": fin(self.p_adj),
            "adj_method": self.adj_method,
            "ci_lo": fin(self.ci_lo),
            "ci_hi": fin(self.ci_hi),
            "n": int(self.n),
            "note": self.note,
        }


class _Boot:
    def __init__(self, blocks: np.ndarray, b: int, seed: int) -> None:
        self.blocks = blocks
        self.b = int(b)
        self.seed = int(seed)

    def run(self, test_id: str, stat: Callable[[np.ndarray], float]) -> tuple[float, np.ndarray]:
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore")
            return stats.block_replicates(self.blocks, stat, self.b, stats.bootstrap_seed(self.seed, test_id))

    @staticmethod
    def ci(reps: np.ndarray, alpha: float = 0.05) -> tuple[float | None, float | None]:
        lo, hi = stats.percentile_ci(reps, alpha)
        return fin(lo), fin(hi)


def _ratio(num: np.ndarray, den: np.ndarray) -> Callable[[np.ndarray], float]:
    def f(idx: np.ndarray) -> float:
        d = float(den[idx].sum())
        return float(num[idx].sum()) / d if d > 0 else float("nan")

    return f


def _cif(time: np.ndarray, cause: np.ndarray, horizons: Sequence[int]) -> np.ndarray:
    if time.size == 0:
        return np.full(len(horizons), np.nan)
    aj = stats.aalen_johansen(time, cause, causes=(1, 2))
    return np.array([float(aj[1].at(float(h), before=0.0)) for h in horizons])


def _event(o: Row, arm: str, horizon_cap: float) -> tuple[float, int]:
    t = o.get(f"{arm}_event_ns")
    c = o.get(f"{arm}_cause")
    if t is not None and c is not None:
        return float(t), int(c)
    ttf = o.get(f"ttf_{arm}_ns")
    if o.get(f"{arm}_filled") and ttf is not None:
        return float(max(int(ttf), 0)), 1
    return horizon_cap, 0


def assemble(
    orders: list[Row],
    markouts: list[Row] | None,
    spec: Any,
    *,
    fills_only: bool = False,
    extra: list[Row] | None = None,
    taker: set[tuple[str, str, int]] | None = None,
) -> Assembly:
    """Statistics of SPEC 5.7.5 on matched per-order rows (`qt.score_orders` columns plus optional `session_date`
    as days, `local_ns`, `{live,sim}_event_ns`, `{live,sim}_cause`) and `qt.score_markouts` rows. `extra` rows
    (`order_key`, `session_date`, `ts_entry`) are orders whose fills enter the markout means only (fills-only
    sources: live fills of unscheduled orders). `taker` holds (order_key, arm, fill_seq) of taker fills."""
    warn: list[str] = []
    tests: list[_Test] = []
    n = len(orders)
    boot_spec = spec.bootstrap
    blocks, kind = block_labels(orders, boot_spec.block)
    if kind == "30min" and boot_spec.block == "auto" and n:
        warn.append("ci_block=30min")
    bt = _Boot(blocks, boot_spec.b, boot_spec.seed)

    live = np.array([bool(o["live_filled"]) for o in orders], dtype=bool)
    sim = np.array([bool(o["sim_filled"]) for o in orders], dtype=bool)
    cls = [o.get("class") or classify(bool(o["live_filled"]), bool(o["sim_filled"])) for o in orders]
    n11 = sum(c == "both" for c in cls)
    n00 = sum(c == "neither" for c in cls)
    b = sum(c == "live_only" for c in cls)
    c = sum(c == "sim_only" for c in cls)
    two = {"both": n11, "neither": n00, "live_only": b, "sim_only": c}

    # ---- bias and McNemar --------------------------------------------------------------------------------------
    bias: dict[str, Any] = {
        "fill_rate_bias": None,
        "ci_lo": None,
        "ci_hi": None,
        "relative_overfill": None,
        "qty_bias": None,
        "qty_bias_ci_lo": None,
        "qty_bias_ci_hi": None,
        "direction": "no_significant_bias",
    }
    mc = stats.mcnemar(b, c)
    mcn: dict[str, Any] = {
        "b": b,
        "c": c,
        "chi2_cc": float(mc.chi2_cc),
        "p_chi2": fin(mc.p_chi2),
        "p_exact": fin(mc.p_exact),
        "odds_ratio": fin(mc.odds_ratio),
        "or_ci_lo": fin(mc.or_ci[0]) if mc.or_ci else None,
        "or_ci_hi": fin(mc.or_ci[1]) if mc.or_ci else None,
    }
    equiv = {
        "fill_rate_margin": float(spec.equivalence.fill_rate_margin),
        "tost_p": None,
        "fill_rate_equivalent": False,
        "markout_margin_ticks": float(spec.equivalence.markout_margin_ticks),
        "markout_horizon_ns": int(spec.equivalence.markout_horizon_ns),
        "markout_tost_p": None,
        "markout_equivalent": False,
    }
    by_decile: list[Row] = []
    by_liq: list[Row] = []
    cluster_info: dict[str, Any] | None = None
    clustered_part: dict[str, Any] | None = None
    computable = not fills_only and n > 0
    if fills_only:
        mcn.update(p_chi2=None, p_exact=None, odds_ratio=None, or_ci_lo=None, or_ci_hi=None)
        warn.append(f"fills_only: fill-rate bias, McNemar, strata tests, equivalence and CIF tests {NOT_COMPUTABLE}")
    if computable:
        ci_ind = sim & ~live
        bi_ind = live & ~sim
        diff = ci_ind.astype(float) - bi_ind.astype(float)
        est, reps = bt.run("fill_rate_bias", lambda idx: float(diff[idx].mean()) if idx.size else float("nan"))
        lo, hi = bt.ci(reps)
        lo90, hi90 = bt.ci(reps, 0.10)
        p_boot = stats.bootstrap_p_two_sided(reps, 0.0)
        bias.update(fill_rate_bias=fin(est), ci_lo=lo, ci_hi=hi)
        if lo is not None and lo > 0:
            bias["direction"] = "sim_overfills"
        elif hi is not None and hi < 0:
            bias["direction"] = "sim_underfills"
        if n11 + b > 0:
            bias["relative_overfill"] = (n11 + c) / (n11 + b) - 1.0
        tests.append(
            _Test(
                "fill_rate_bias",
                "secondary",
                "all",
                "all",
                "fill_rate_bias",
                est,
                p_boot,
                lo,
                hi,
                n,
                "block bootstrap; descriptive (same draws as discordance_clustered)",
            )
        )
        tests.append(
            _Test(
                "mcnemar",
                "secondary",
                "all",
                "all",
                "chi2_cc",
                mc.chi2_cc,
                mc.p_exact,
                None,
                None,
                b + c,
                "p = exact; assumes independent orders",
                adjust=True,
            )
        )
        if any("ambiguous_race" in o for o in orders):
            # Audit minor 15: the 2x2 without orders whose exact-arm shadow has an ambiguous latency race.
            keep = [k for k, o in enumerate(orders) if not o.get("ambiguous_race")]
            rf = {x: sum(cls[k] == x for k in keep) for x in ("both", "neither", "live_only", "sim_only")}
            mrf = stats.mcnemar(rf["live_only"], rf["sim_only"])
            n_race = n - len(keep)
            txt = "/".join(str(rf[x]) for x in ("both", "neither", "live_only", "sim_only"))
            tests.append(
                _Test(
                    "mcnemar_race_free",
                    "secondary",
                    "race",
                    "race_free",
                    "chi2_cc",
                    mrf.chi2_cc,
                    mrf.p_exact,
                    None,
                    None,
                    rf["live_only"] + rf["sim_only"],
                    f"2x2 without {n_race} race-flagged order(s) (both/neither/live_only/sim_only = {txt}); "
                    "p = exact; assumes independent orders",
                )
            )
            warn.append(
                f"race: {n_race} of {n} order(s) race-flagged (exact-arm ambiguous_race); 2x2 without them "
                f"both/neither/live_only/sim_only = {txt} (McNemar p = {_g(fin(mrf.p_exact))})"
            )
        cl = stats.clustered_mcnemar(
            live, sim, blocks, b=int(boot_spec.b), seed=stats.bootstrap_seed(int(boot_spec.seed), "fill_rate_bias")
        )
        cluster_info = {"p_value": fin(cl.p_primary), "k": cl.n_clusters, "method": cl.primary_method}
        clustered_part = {
            "method": cl.primary_method,
            "estimate": fin(cl.estimate),
            "ci_lo": fin(cl.ci_lo),
            "ci_hi": fin(cl.ci_hi),
            "p_value": fin(cl.p_primary),
            "n_clusters": int(cl.n_clusters),
            "n_clusters_discordant": int(cl.n_clusters_discordant),
            "design_effect": fin(cl.design_effect),
            "n_effective": fin(cl.n_effective),
        }
        warn.append(
            f"clusters: {cl.n_clusters} ({kind} blocks), with discordance {cl.n_clusters_discordant}, design effect "
            f"{_g(cl.design_effect)}, effective n {_g(cl.n_effective)}; primary discordance test {cl.primary_method}"
        )
        tests.append(
            _Test(
                "discordance_clustered",
                "primary",
                "all",
                "all",
                "fill_rate_bias",
                cl.estimate,
                cl.p_primary,
                cl.ci_lo,
                cl.ci_hi,
                n,
                f"{cl.primary_method}; clusters={cl.n_clusters}; discordant clusters={cl.n_clusters_discordant}; "
                f"design_effect={_g(cl.design_effect)}; n_effective={_g(cl.n_effective)}",
            )
        )
        if lo90 is not None and hi90 is not None:
            equiv["fill_rate_equivalent"] = bool(stats.tost_from_ci((lo90, hi90), equiv["fill_rate_margin"]))
            # M22: the p-value from the same replicates as the 90% CI behind the flag (one method)
            equiv["tost_p"] = fin(stats.tost_p_bootstrap(reps, equiv["fill_rate_margin"]))
        tests.append(
            _Test(
                "tost_fill_rate",
                "secondary",
                "all",
                "all",
                "fill_rate_bias",
                est,
                equiv["tost_p"],
                lo90,
                hi90,
                n,
                f"equivalence margin {equiv['fill_rate_margin']}: equivalent={equiv['fill_rate_equivalent']}",
            )
        )
        if bias["relative_overfill"] is not None:
            tests.append(
                _Test(
                    "relative_overfill", "secondary", "all", "all", "relative_overfill", bias["relative_overfill"], n=n
                )
            )
        qty = np.array([float(o["qty"]) for o in orders])
        sq = np.array([float(o["sim_filled_qty"]) for o in orders])
        lq = np.array([float(o["live_filled_qty"]) for o in orders])
        qest, qreps = bt.run("qty_bias", _ratio(sq - lq, qty))
        qlo, qhi = bt.ci(qreps)
        bias.update(qty_bias=fin(qest), qty_bias_ci_lo=qlo, qty_bias_ci_hi=qhi)
        tests.append(
            _Test(
                "qty_bias",
                "secondary",
                "all",
                "all",
                "qty_bias",
                qest,
                stats.bootstrap_p_two_sided(qreps, 0.0),
                qlo,
                qhi,
                n,
                adjust=True,
            )
        )
        for key, kind_name, out in (
            ("queue_decile", "queue_decile", by_decile),
            ("liquidity_bucket", "liquidity_bucket", by_liq),
        ):
            vals = sorted({o.get(key) for o in orders if o.get(key) is not None})
            for v in vals:
                sub = [cl for o, cl in zip(orders, cls, strict=True) if o.get(key) == v]
                t2 = {k: sum(x == k for x in sub) for k in CLASSES}
                m = stats.mcnemar(t2["live_only"], t2["sim_only"])
                row = {
                    ("decile" if key == "queue_decile" else "bucket"): int(v),
                    **t2,
                    "p_exact": fin(m.p_exact),
                    "p_adj": None,
                }
                out.append(row)
                tests.append(
                    _Test(
                        f"mcnemar_{kind_name}",
                        "secondary",
                        kind_name,
                        str(v),
                        "chi2_cc",
                        m.chi2_cc,
                        m.p_exact,
                        n=len(sub),
                        note="p = exact",
                        adjust=True,
                    )
                )

    # ---- time to fill ------------------------------------------------------------------------------------------
    horizons = [int(h) for h in spec.ttf_horizons_ns]
    joint = [
        i
        for i, o in enumerate(orders)
        if o["live_filled"] and o["sim_filled"] and o.get("ttf_live_ns") is not None and o.get("ttf_sim_ns") is not None
    ]
    dlog = np.full(n, np.nan)
    for i in joint:
        o = orders[i]
        dlog[i] = math.log(max(int(o["ttf_sim_ns"]), US)) - math.log(max(int(o["ttf_live_ns"]), US))
    ttf: dict[str, Any] = {
        "n_joint": len(joint),
        "median_log_ratio": None,
        "ci_lo": None,
        "ci_hi": None,
        "wilcoxon_p": None,
        "paired_perm_p": None,
        "logrank_p": None,
        "gray_p": None,
        "cif_fill": {"live": {}, "sim": {}},
    }
    if joint:
        mest, mreps = bt.run(
            "ttf_median_log_ratio",
            lambda idx: float(np.nanmedian(dlog[idx])) if np.isfinite(dlog[idx]).any() else float("nan"),
        )
        mlo, mhi = bt.ci(mreps)
        w = stats.wilcoxon_signed_rank(dlog[joint])
        ttf.update(median_log_ratio=fin(mest), ci_lo=mlo, ci_hi=mhi, wilcoxon_p=fin(w.pvalue))
        tests.append(
            _Test(
                "ttf_wilcoxon",
                "secondary",
                "all",
                "all",
                "median_log_ratio",
                mest,
                w.pvalue,
                mlo,
                mhi,
                len(joint),
                "jointly filled orders",
                adjust=True,
            )
        )
    if computable:
        cap = float(
            max(
                [max(horizons, default=0)]
                + [float(o.get(f"{a}_event_ns") or 0) for o in orders for a in ("live", "sim")]
            )
        )
        ev_l = np.array([_event(o, "live", cap) for o in orders], dtype=float).reshape(-1, 2)
        ev_s = np.array([_event(o, "sim", cap) for o in orders], dtype=float).reshape(-1, 2)
        cif_l = _cif(ev_l[:, 0], ev_l[:, 1].astype(int), horizons)
        cif_s = _cif(ev_s[:, 0], ev_s[:, 1].astype(int), horizons)
        ttf["cif_fill"] = {
            "live": {str(h): fin(v) for h, v in zip(horizons, cif_l, strict=True)},
            "sim": {str(h): fin(v) for h, v in zip(horizons, cif_s, strict=True)},
        }

        def dstat(xl: np.ndarray, xs: np.ndarray) -> float:
            a = _cif(xl[:, 0], xl[:, 1].astype(int), horizons)
            s = _cif(xs[:, 0], xs[:, 1].astype(int), horizons)
            return float(np.nansum(np.abs(s - a)))

        perm = stats.paired_permutation(
            ev_l, ev_s, dstat, int(spec.perm_b), stats.stable_seed(int(boot_spec.seed), [b"permutation", "ttf_cif"])
        )
        ttf["paired_perm_p"] = fin(perm.p_value)
        tests.append(
            _Test(
                "ttf_cif_permutation",
                "secondary",
                "all",
                "all",
                "sum_abs_cif_diff",
                perm.statistic,
                perm.p_value,
                n=n,
                note="paired permutation of arm labels",
                adjust=True,
            )
        )
        lr = stats.logrank(ev_l[:, 0], ev_l[:, 1] == 1, ev_s[:, 0], ev_s[:, 1] == 1)
        ttf["logrank_p"] = fin(lr.p_value)
        tests.append(
            _Test(
                "ttf_logrank",
                "secondary",
                "all",
                "all",
                "chi2",
                lr.statistic,
                lr.p_value,
                n=n,
                note="descriptive: assumes independent samples",
                adjust=True,
            )
        )
        gt = stats.gray_test(
            np.concatenate([ev_l[:, 0], ev_s[:, 0]]),
            np.concatenate([ev_l[:, 1], ev_s[:, 1]]).astype(int),
            np.concatenate([np.zeros(n, dtype=int), np.ones(n, dtype=int)]),
            1,
        )
        ttf["gray_p"] = fin(gt.p_value)
        tests.append(
            _Test(
                "ttf_gray",
                "secondary",
                "all",
                "all",
                "chi2",
                gt.statistic,
                gt.p_value,
                n=n,
                note="descriptive: assumes independent samples",
                adjust=True,
            )
        )

    # ---- markouts ----------------------------------------------------------------------------------------------
    all_orders = orders + list(extra or [])
    bt_mk = bt if not extra else _Boot(block_labels(all_orders, kind)[0], boot_spec.b, boot_spec.seed)
    mk_rows, diff_rows, disc_rows = _markouts(
        all_orders,
        cls + ["extra"] * len(extra or []),
        markouts or [],
        spec,
        bt_mk,
        tests,
        equiv,
        computable,
        taker or set(),
    )

    # ---- multiplicity ------------------------------------------------------------------------------------------
    prim = [t for t in tests if t.family == "primary" and fin(t.p_value) is not None]
    if prim:
        adj = stats.holm(np.array([float(t.p_value) for t in prim]))  # type: ignore[arg-type]
        for t, a in zip(prim, adj, strict=True):
            t.p_adj, t.adj_method = float(a), "holm"
    sec = [t for t in tests if t.family == "secondary" and t.adjust and fin(t.p_value) is not None]
    method = spec.fdr.method
    q = float(spec.fdr.q)
    n_rej = 0
    if sec:
        p = np.array([float(t.p_value) for t in sec])  # type: ignore[arg-type]
        adj = stats.benjamini_yekutieli(p, q) if method == "by" else stats.benjamini_hochberg(p, q)
        for t, a in zip(sec, adj, strict=True):
            t.p_adj, t.adj_method = float(a), method
        n_rej = int(np.sum(np.asarray(adj) <= q))
    for t in tests:
        if t.test_id == "mcnemar_queue_decile":
            next(r for r in by_decile if str(r["decile"]) == t.stratum)["p_adj"] = fin(t.p_adj)
        elif t.test_id == "mcnemar_liquidity_bucket":
            next(r for r in by_liq if str(r["bucket"]) == t.stratum)["p_adj"] = fin(t.p_adj)
        elif t.test_id == "markout_diff":
            for r in diff_rows:
                if f"{r['ref']}:{r['horizon_ns']}" == t.stratum:
                    r["p_adj"] = fin(t.p_adj)
    fdr = {"method": method, "q": q, "n_tests": len(sec), "n_rejected": n_rej}

    parts = {
        "n_matched": n,
        "two_by_two": two,
        "bias": bias,
        "mcnemar": mcn,
        "by_queue_decile": by_decile,
        "by_liquidity": by_liq,
        "ttf": ttf,
        "markouts": mk_rows,
        "markout_diff": diff_rows,
        "sim_only_vs_live_only_markout": disc_rows,
        "equivalence": equiv,
        "fdr": fdr,
        "verdict": verdict(bias, mcn, n, spec, equiv, fills_only, cluster_info),
        "discordance_clustered": clustered_part,
    }
    return Assembly(parts=parts, tests=[t.row() for t in tests], warnings=warn)


def _markouts(
    orders: list[Row],
    cls: list[str],
    markouts: list[Row],
    spec: Any,
    bt: _Boot,
    tests: list[_Test],
    equiv: dict[str, Any],
    computable: bool,
    taker: set[tuple[str, str, int]],
) -> tuple[list[Row], list[Row], list[Row]]:
    pos = {str(o["order_key"]): i for i, o in enumerate(orders)}
    n = len(orders)
    refs = [str(r) for r in spec.markout_refs]
    horizons = [int(h) for h in spec.markout_horizons_ns]
    first_ref = "mid" if "mid" in refs else (refs[0] if refs else "mid")
    sums: dict[tuple[str, str, int], tuple[np.ndarray, np.ndarray]] = {}
    tsums: dict[tuple[str, str, int], tuple[np.ndarray, np.ndarray]] = {}
    xsums: dict[tuple[str, str, str, int], tuple[np.ndarray, np.ndarray]] = {}
    first: dict[tuple[str, str, int], np.ndarray] = {}
    first_seq: dict[tuple[str, str, int], np.ndarray] = {}
    for m in markouts:
        i = pos.get(str(m["order_key"]))
        v = fin(m.get("mo_ticks"))
        if i is None or v is None:
            continue
        key = (str(m["arm"]), str(m["ref"]), int(m["horizon_ns"]))
        seq = int(m["fill_seq"])
        is_taker = (str(m["order_key"]), str(m["arm"]), seq) in taker
        target = tsums if is_taker else sums
        if key not in target:
            target[key] = (np.zeros(n), np.zeros(n))
        s, c = target[key]
        s[i] += v
        c[i] += 1
        if is_taker:
            continue
        for col in EXTRA_MARKOUTS:
            xv = fin(m.get(col))
            if xv is None:
                continue
            xk = (col, *key)
            if xk not in xsums:
                xsums[xk] = (np.zeros(n), np.zeros(n))
            xs_, xc_ = xsums[xk]
            xs_[i] += xv
            xc_[i] += 1
        if key not in first:
            first[key] = np.full(n, np.nan)
            first_seq[key] = np.full(n, np.iinfo(np.int64).max)
        if seq < first_seq[key][i]:
            first_seq[key][i] = seq
            first[key][i] = v
    mk_rows: list[Row] = []
    for h in horizons:
        for ref in refs:
            for arm in ("live", "sim"):
                key = (arm, ref, h)
                if key not in sums:
                    mk_rows.append(
                        {
                            "horizon_ns": h,
                            "ref": ref,
                            "arm": arm,
                            "n": 0,
                            "mean_ticks": None,
                            "ci_lo": None,
                            "ci_hi": None,
                        }
                    )
                    continue
                s, cnt = sums[key]
                est, reps = bt.run(f"markout_mean:{arm}:{ref}:{h}", _ratio(s, cnt))
                lo, hi = bt.ci(reps)
                mk_rows.append(
                    {
                        "horizon_ns": h,
                        "ref": ref,
                        "arm": arm,
                        "n": int(cnt.sum()),
                        "mean_ticks": fin(est),
                        "ci_lo": lo,
                        "ci_hi": hi,
                    }
                )
                tests.append(
                    _Test(
                        "markout_mean",
                        "secondary",
                        f"{arm}",
                        f"{ref}:{h}",
                        "mean_ticks",
                        est,
                        None,
                        lo,
                        hi,
                        int(cnt.sum()),
                        "maker fills",
                    )
                )
                for col, (tid, stat_name, note) in EXTRA_MARKOUTS.items():
                    xk = (col, *key)
                    if xk not in xsums:
                        continue
                    xs_, xc_ = xsums[xk]
                    xest, xreps = bt.run(f"{tid}:{arm}:{ref}:{h}", _ratio(xs_, xc_))
                    xlo, xhi = bt.ci(xreps)
                    tests.append(
                        _Test(
                            tid,
                            "secondary",
                            f"{arm}",
                            f"{ref}:{h}",
                            stat_name,
                            xest,
                            None,
                            xlo,
                            xhi,
                            int(xc_.sum()),
                            note,
                        )
                    )
                if key in tsums:
                    ts_, tc_ = tsums[key]
                    test, treps = bt.run(f"markout_mean_taker:{arm}:{ref}:{h}", _ratio(ts_, tc_))
                    tlo, thi = bt.ci(treps)
                    tests.append(
                        _Test(
                            "markout_mean_taker",
                            "secondary",
                            f"{arm}",
                            f"{ref}:{h}",
                            "mean_ticks",
                            test,
                            None,
                            tlo,
                            thi,
                            int(tc_.sum()),
                            "taker fills (book crossed the order)",
                        )
                    )
    diff_rows: list[Row] = []
    for h in horizons:
        for ref in refs:
            fl, fs = first.get(("live", ref, h)), first.get(("sim", ref, h))
            d = np.full(n, np.nan) if fl is None or fs is None else fs - fl
            ok = np.isfinite(d)
            num, den = np.where(ok, d, 0.0), ok.astype(float)
            tid = f"markout_diff:{ref}:{h}"
            if not ok.any():
                diff_rows.append(
                    {
                        "horizon_ns": h,
                        "ref": ref,
                        "n": 0,
                        "mean_diff_ticks": None,
                        "ci_lo": None,
                        "ci_hi": None,
                        "p_adj": None,
                    }
                )
                continue
            est, reps = bt.run(tid, _ratio(num, den))
            lo, hi = bt.ci(reps)
            p = stats.bootstrap_p_two_sided(reps, 0.0)
            primary = h == ONE_S and ref == first_ref
            diff_rows.append(
                {
                    "horizon_ns": h,
                    "ref": ref,
                    "n": int(ok.sum()),
                    "mean_diff_ticks": fin(est),
                    "ci_lo": lo,
                    "ci_hi": hi,
                    "p_adj": None,
                }
            )
            tests.append(
                _Test(
                    "markout_diff",
                    "primary" if primary else "secondary",
                    "ref:horizon",
                    f"{ref}:{h}",
                    "mean_diff_ticks",
                    est,
                    p,
                    lo,
                    hi,
                    int(ok.sum()),
                    "first fill, sim minus live",
                    adjust=not primary,
                )
            )
            if h == int(equiv["markout_horizon_ns"]) and ref == first_ref:
                lo90, hi90 = bt.ci(reps, 0.10)
                if lo90 is not None and hi90 is not None:
                    m = equiv["markout_margin_ticks"]
                    equiv["markout_equivalent"] = bool(stats.tost_from_ci((lo90, hi90), m))
                    equiv["markout_tost_p"] = fin(stats.tost_p_bootstrap(reps, m))
                    tests.append(
                        _Test(
                            "tost_markout",
                            "secondary",
                            "ref:horizon",
                            f"{ref}:{h}",
                            "mean_diff_ticks",
                            est,
                            equiv["markout_tost_p"],
                            lo90,
                            hi90,
                            int(ok.sum()),
                            f"equivalence margin {m} ticks: equivalent={equiv['markout_equivalent']}",
                        )
                    )
    disc_rows: list[Row] = []
    if computable:
        so = np.array([c == "sim_only" for c in cls])
        lo_ = np.array([c == "live_only" for c in cls])
        for h in horizons:
            fs, fl = first.get(("sim", first_ref, h)), first.get(("live", first_ref, h))
            vs = np.where(so & np.isfinite(fs), fs, np.nan) if fs is not None else np.full(n, np.nan)
            vl = np.where(lo_ & np.isfinite(fl), fl, np.nan) if fl is not None else np.full(n, np.nan)
            ms, ml = np.isfinite(vs), np.isfinite(vl)
            if not ms.any() and not ml.any():
                continue
            ns_, cs_ = np.where(ms, vs, 0.0), ms.astype(float)
            nl_, cl_ = np.where(ml, vl, 0.0), ml.astype(float)

            def stat(idx: np.ndarray, ns_=ns_, cs_=cs_, nl_=nl_, cl_=cl_) -> float:
                a, b = cs_[idx].sum(), cl_[idx].sum()
                if a == 0 or b == 0:
                    return float("nan")
                return float(ns_[idx].sum() / a - nl_[idx].sum() / b)

            sm = float(ns_.sum() / cs_.sum()) if cs_.sum() else None
            lm = float(nl_.sum() / cl_.sum()) if cl_.sum() else None
            est, reps = bt.run(f"sim_only_vs_live_only_markout:{first_ref}:{h}", stat)
            lo, hi = bt.ci(reps)
            disc_rows.append(
                {"horizon_ns": h, "sim_only_mean": sm, "live_only_mean": lm, "diff": fin(est), "ci_lo": lo, "ci_hi": hi}
            )
            if fin(est) is not None:
                tests.append(
                    _Test(
                        "sim_only_vs_live_only_markout",
                        "secondary",
                        "ref:horizon",
                        f"{first_ref}:{h}",
                        "diff_ticks",
                        est,
                        stats.bootstrap_p_two_sided(reps, 0.0),
                        lo,
                        hi,
                        int(ms.sum() + ml.sum()),
                        adjust=True,
                    )
                )
    return mk_rows, diff_rows, disc_rows


def _g(x: float | None) -> str:
    return "n/a" if fin(x) is None else f"{float(x):.4g}"  # type: ignore[arg-type]


def _pp(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}"


def _pp_abs(x: float | None) -> str:
    return "n/a" if x is None else f"{abs(x) * 100:.1f}"


def duration_text(ns: int) -> str:
    """`60 s`, `1.5 s`, `500 ms`, `250 us`, `10 ns`."""
    ns = int(ns)
    for unit, scale in (("s", 10**9), ("ms", 10**6), ("us", 10**3)):
        if abs(ns) >= scale:
            return f"{ns / scale:g} {unit}"
    return f"{ns} ns"


def gates_configured(gates: Any) -> bool:
    """True when a scorecard gate was requested (`max_abs_fill_rate_bias` set or `require_equivalence`)."""
    if gates is None:
        return False
    get = gates.get if isinstance(gates, dict) else lambda k: getattr(gates, k, None)
    return get("max_abs_fill_rate_bias") is not None or bool(get("require_equivalence"))


def headline(
    bias: dict[str, Any],
    mcn: dict[str, Any],
    n: int,
    fills_only: bool = False,
    clusters: dict[str, Any] | None = None,
    *,
    window_ns: int | None = None,
    equiv: dict[str, Any] | None = None,
    gates: bool | None = None,
) -> str:
    """Scorecard headline (review F3, F6, F8; audit ruling A10). A significant bias reads `"sim
    {overfills|underfills} live by {x.x} pp fill rate within {H} (95% CI [{a}, {b}]); {tests}; {N} orders"` (the
    magnitude unsigned, the CI signed); otherwise `"no significant fill-rate difference within {H}: {+x.x} pp (95% CI
    [{a}, {b}]); {equivalence}; {tests}; {N} orders"` with the TOST verdict (`equivalent within ±{m} pp (TOST p =
    {t})` or `not shown equivalent within ...`). `{tests}` leads with the primary cluster-robust discordance test
    when it ran, the plain McNemar p second: `cluster-robust p = {p} ({k} clusters); plain McNemar p = {p} (assumes
    independent orders)`; without the clustered test it is `McNemar p = {p}`. `within {H}` names the evaluation
    window (ruling A9) when given; `; no gates configured` ends it when `gates` is False. Fills-only sources get a
    not-computable headline."""
    win = "" if window_ns is None else f" within {duration_text(window_ns)}"
    tail = "; no gates configured" if gates is False else ""
    if fills_only:
        return f"fill-rate bias and McNemar {NOT_COMPUTABLE} (never-filled orders missing); {n} orders{tail}"
    p = mcn.get("p_exact")
    ptxt = "n/a" if p is None else f"{p:.3g}"
    if clusters is not None and clusters.get("p_value") is not None:
        tests = (
            f"cluster-robust p = {clusters['p_value']:.3g} ({clusters['k']} clusters); plain McNemar p = {ptxt} "
            "(assumes independent orders)"
        )
    else:
        tests = f"McNemar p = {ptxt}"
    ci = f"95% CI [{_pp(bias['ci_lo'])}, {_pp(bias['ci_hi'])}]"
    word = {"sim_overfills": "overfills", "sim_underfills": "underfills"}.get(bias["direction"])
    if word is not None:
        return (
            f"sim {word} live by {_pp_abs(bias['fill_rate_bias'])} pp fill rate{win} ({ci}); {tests}; {n} orders{tail}"
        )
    eq = ""
    if equiv is not None:
        m = f"±{100 * float(equiv['fill_rate_margin']):g} pp"
        tost = equiv.get("tost_p")
        ttxt = "n/a" if tost is None else f"{tost:.3g}"
        eq = (
            f"; equivalent within {m} (TOST p = {ttxt})"
            if equiv.get("fill_rate_equivalent")
            else f"; not shown equivalent within {m} (TOST p = {ttxt})"
        )
    diff = _pp(bias["fill_rate_bias"])
    return f"no significant fill-rate difference{win}: {diff} pp ({ci}){eq}; {tests}; {n} orders{tail}"


def verdict(
    bias: dict[str, Any],
    mcn: dict[str, Any],
    n: int,
    spec: Any,
    equiv: dict[str, Any],
    fills_only: bool,
    clusters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Headline, signed bias and gate results."""
    failed: list[str] = []
    g = spec.gates
    if g.max_abs_fill_rate_bias is not None:
        fb = bias["fill_rate_bias"]
        if fb is None:
            failed.append(
                "max_abs_fill_rate_bias: " + (NOT_COMPUTABLE if fills_only else "not computable: no matched orders")
            )
        elif abs(fb) > float(g.max_abs_fill_rate_bias):
            failed.append("max_abs_fill_rate_bias")
    if g.require_equivalence and not equiv["fill_rate_equivalent"]:
        failed.append("require_equivalence" + (f": {NOT_COMPUTABLE}" if fills_only else ""))
    return {
        "headline": headline(
            bias,
            mcn,
            n,
            fills_only,
            clusters,
            window_ns=getattr(spec, "window_ns", None),
            equiv=equiv,
            gates=gates_configured(g),
        ),
        "signed_bias": bias["fill_rate_bias"],
        "gates_passed": not failed,
        "failed_gates": failed,
    }
