"""Order-level statistics of `compare`: intervals clustered by instrument and day, the discordant fills, and the
drift of a gap between two windows of orders (`compare --watch`).

Orders on the same instrument and day are correlated (one market, one queue, one strategy state), so `compare`
resamples instrument-by-day clusters, not orders:

- `clusters(keys)`: one cluster per (instrument, venue-local day). With at least `MIN_CLUSTERS` (5, the
  `queuetruth.stats.MIN_CI_BLOCKS` policy of short captures) the intervals are cluster bootstraps; with fewer the
  orders are counted as independent and every label says so.
- `share(ok, cl)`: a share (the backtest's fills that also happened live). Clustered: the percentile interval of
  the ratio `sum ok / sum n` over resampled clusters (`stats.block_counts`, `stats.ratio_replicates`,
  `stats.percentile_ci`, 1,000 resamples, seed `stats.bootstrap_seed(0, test_id)`). Independent: the 95% Wilson
  interval (`stats.wilson`, exact 0 and 1 edges).
- `discordance(backtest, live, cl)`: orders paired by key, each filled or not on each side. `b` = backtest-only
  fills, `c` = live-only fills. Clustered: `stats.clustered_mcnemar` (Obuchowski's cluster-adjusted McNemar, its
  block-bootstrap interval of `(b - c) / n`); independent: the exact McNemar test (`stats.mcnemar`).
- `Agg` and `drift(base, new)`: a per-order value summed per cluster (`n`, sum, sum of squares). The drift is the
  mean of the new window minus the mean of the baseline window. Clustered (both windows at least 5 clusters): each
  window resampled on its own (independent seeds) and the replicates subtracted; independent: a normal interval from
  the two variances of the mean. Treating the windows as independent is conservative when they share a cluster.
  `mean_ci(agg)` is one window's own mean with its interval, the same way.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from queuetruth.stats import (
    MIN_CI_BLOCKS,
    Z95,
    block_counts,
    bootstrap_seed,
    clustered_mcnemar,
    mcnemar,
    percentile_ci,
    ratio_replicates,
    wilson,
)

#: Fewest instrument-day clusters behind a clustered interval (with fewer, orders are counted as independent).
MIN_CLUSTERS = MIN_CI_BLOCKS
#: Bootstrap resamples of every clustered interval.
RESAMPLES = 1000


@dataclass(frozen=True)
class Clusters:
    """Dense cluster index per order (`labels`), the number of clusters `n` and whether they are enough."""

    labels: np.ndarray
    n: int

    @property
    def clustered(self) -> bool:
        return self.n >= MIN_CLUSTERS

    def label(self) -> str:
        """`clustered by instrument and day, 14 clusters`, or the independent label with the reason."""
        unit = "cluster" if self.n == 1 else "clusters"
        if self.clustered:
            return f"clustered by instrument and day, {self.n} {unit}"
        return f"orders counted as independent: {self.n} instrument-day {unit}, fewer than {MIN_CLUSTERS}"

    def doc(self) -> dict[str, Any]:
        return {
            "method": "instrument_day" if self.clustered else "independent",
            "n_clusters": self.n,
            "min_clusters": MIN_CLUSTERS,
            "label": self.label(),
        }


def clusters(keys: Sequence[Any]) -> Clusters:
    """Clusters of orders from one hashable key per order (here `(instrument, day)`)."""
    index: dict[Any, int] = {}
    labels = np.fromiter((index.setdefault(k, len(index)) for k in keys), dtype=np.int64, count=len(keys))
    return Clusters(labels, len(index))


def _subset(cl: Clusters, mask: np.ndarray) -> Clusters:
    return clusters([int(x) for x in cl.labels[mask]])


def share(ok: Sequence[bool], cl: Clusters, test_id: str) -> dict[str, Any]:
    """A share of orders with its 95% interval (module docstring)."""
    x = np.asarray(ok, dtype=bool)
    n, k = int(x.size), int(x.sum())
    if n == 0:
        return {"k": 0, "n": 0, "share": None, "ci_lo": None, "ci_hi": None, "interval": cl.doc()}
    if cl.clustered:
        num = np.bincount(cl.labels, weights=x.astype(float), minlength=cl.n)
        den = np.bincount(cl.labels, minlength=cl.n).astype(float)
        counts = block_counts(cl.n, RESAMPLES, bootstrap_seed(0, test_id)).astype(float)
        lo, hi = percentile_ci(ratio_replicates(counts, num, den))
    else:
        ci = wilson(k, n, edges="exact")
        assert ci is not None
        lo, hi = ci
    return {"k": k, "n": n, "share": k / n, "ci_lo": float(lo), "ci_hi": float(hi), "interval": cl.doc()}


def discordance(backtest: Sequence[bool], live: Sequence[bool], cl: Clusters, test_id: str) -> dict[str, Any]:
    """McNemar on the discordant pairs, clustered when there are enough clusters (module docstring). `estimate`
    is the backtest's fill rate minus live's over the paired orders, with its interval when clustered."""
    bt = np.asarray(backtest, dtype=bool)
    lv = np.asarray(live, dtype=bool)
    n = int(bt.size)
    b, c = int(np.sum(bt & ~lv)), int(np.sum(~bt & lv))
    doc: dict[str, Any] = {"n": n, "backtest_only": b, "live_only": c, "interval": cl.doc()}
    doc["estimate"] = (b - c) / n if n else None
    if cl.clustered and n:
        r = clustered_mcnemar(lv, bt, cl.labels, b=RESAMPLES, seed=bootstrap_seed(0, test_id))
        doc.update(
            {
                "p": r.p_primary,
                "test": r.primary_method,
                "ci_lo": r.ci_lo,
                "ci_hi": r.ci_hi,
                "p_mcnemar_exact": r.mcnemar.p_exact,
                "design_effect": r.design_effect,
            }
        )
    else:
        m = mcnemar(b, c)
        doc.update({"p": m.p_exact, "test": "mcnemar_exact", "ci_lo": None, "ci_hi": None})
        doc.update({"p_mcnemar_exact": m.p_exact, "design_effect": None})
    return doc


def pvalue(p: float | None) -> str:
    """`p = 0.0006`, `p < 0.0001`, `p = 1`."""
    if p is None or not math.isfinite(p):
        return "p n/a"
    if p < 1e-4:
        return "p < 0.0001"
    return f"p = {p:.2g}" if p < 0.01 else f"p = {p:.2f}".rstrip("0").rstrip(".")


def discordance_line(d: dict[str, Any], who: str = "the backtest") -> str:
    """The discordant fills in plain words first: `the backtest fills more orders than live: 31 backtest-only
    against 9 live-only, p = 0.0006, clustered by instrument and day, 14 clusters`."""
    b, c = d["backtest_only"], d["live_only"]
    label = d["interval"]["label"]
    test = "" if d["test"] in ("obuchowski", "mcnemar_exact") else f", {d['test'].replace('_', ' ')} p"
    short = "backtest" if who == "the backtest" else who.removeprefix("the ")
    if b == c == 0:
        return f"discordant fills: {who} and live fill the same orders (no order filled on one side only)."
    if b == c:
        what = f"{who} and live fill as many orders"
    else:
        what = f"{who} fills {'more' if b > c else 'fewer'} orders than live"
    return f"discordant fills: {what}: {b} {short}-only against {c} live-only, {pvalue(d['p'])}{test}, {label}."


# ---- drift between two windows of orders ------------------------------------------------------------------------


@dataclass
class Agg:
    """A per-order value aggregated per cluster: `{cluster key: [n, sum, sum of squares]}`. JSON-ready through
    `to_doc` and `from_doc` (the watch state)."""

    cells: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def of(cls, keys: Iterable[str], values: Iterable[float | None]) -> Agg:
        a = cls()
        for k, v in zip(keys, values, strict=True):
            if v is None or not math.isfinite(float(v)):
                continue
            c = a.cells.setdefault(str(k), [0.0, 0.0, 0.0])
            c[0] += 1
            c[1] += float(v)
            c[2] += float(v) * float(v)
        return a

    @classmethod
    def from_doc(cls, d: dict[str, Any] | None) -> Agg:
        return cls({str(k): [float(x) for x in v] for k, v in (d or {}).items()})

    def to_doc(self) -> dict[str, list[float]]:
        return {k: list(v) for k, v in sorted(self.cells.items())}

    @property
    def n(self) -> int:
        return int(sum(c[0] for c in self.cells.values()))

    @property
    def n_clusters(self) -> int:
        return sum(1 for c in self.cells.values() if c[0] > 0)

    def mean(self) -> float | None:
        n = self.n
        return None if n == 0 else sum(c[1] for c in self.cells.values()) / n

    def var_mean(self) -> float | None:
        """Variance of the mean with the orders counted as independent (None with fewer than 2 orders)."""
        n = self.n
        if n < 2:
            return None
        s = sum(c[1] for c in self.cells.values())
        ss = sum(c[2] for c in self.cells.values())
        return max(ss - s * s / n, 0.0) / (n - 1) / n

    def replicates(self, seed: int) -> np.ndarray:
        cells = [c for c in self.cells.values() if c[0] > 0]
        num = np.array([c[1] for c in cells], dtype=float)
        den = np.array([c[0] for c in cells], dtype=float)
        counts = block_counts(len(cells), RESAMPLES, seed).astype(float)
        return ratio_replicates(counts, num, den)


def _interval_doc(ks: list[int]) -> dict[str, Any]:
    k = min(ks)
    clustered = k >= MIN_CLUSTERS
    unit = "cluster" if k == 1 else "clusters"
    if clustered:
        label = f"clustered by instrument and day, {' and '.join(str(x) for x in ks)} clusters"
    else:
        where = " in the smaller window" if len(ks) > 1 else ""
        label = f"orders counted as independent: {k} instrument-day {unit}{where}, fewer than {MIN_CLUSTERS}"
    return {
        "method": "instrument_day" if clustered else "independent",
        "n_clusters": ks if len(ks) > 1 else ks[0],
        "min_clusters": MIN_CLUSTERS,
        "label": label,
    }


def mean_ci(agg: Agg, test_id: str) -> dict[str, Any]:
    """The mean of one window with its 95% interval (clustered with at least 5 clusters, else independent)."""
    m = agg.mean()
    out: dict[str, Any] = {"estimate": m, "n": agg.n, "ci_lo": None, "ci_hi": None}
    out["interval"] = _interval_doc([agg.n_clusters])
    if m is None:
        return out
    if agg.n_clusters >= MIN_CLUSTERS:
        lo, hi = percentile_ci(agg.replicates(bootstrap_seed(0, test_id)))
    else:
        v = agg.var_mean()
        if v is None:
            return out
        lo, hi = m - Z95 * math.sqrt(v), m + Z95 * math.sqrt(v)
    out["ci_lo"], out["ci_hi"] = float(lo), float(hi)
    return out


def drift(base: Agg, new: Agg, test_id: str) -> dict[str, Any]:
    """The new window's mean minus the baseline's, with its 95% interval (module docstring)."""
    mb, mn = base.mean(), new.mean()
    k = min(base.n_clusters, new.n_clusters)
    out: dict[str, Any] = {"baseline": mb, "new": mn, "n_baseline": base.n, "n_new": new.n}
    out.update({"drift": None, "ci_lo": None, "ci_hi": None})
    clustered = k >= MIN_CLUSTERS
    out["interval"] = _interval_doc([base.n_clusters, new.n_clusters])
    if mb is None or mn is None:
        return out
    out["drift"] = mn - mb
    if clustered:
        reps = new.replicates(bootstrap_seed(0, f"{test_id}/new")) - base.replicates(
            bootstrap_seed(0, f"{test_id}/baseline")
        )
        lo, hi = percentile_ci(reps)
    else:
        vb, vn = base.var_mean(), new.var_mean()
        if vb is None or vn is None:
            return out
        half = Z95 * math.sqrt(vb + vn)
        lo, hi = mn - mb - half, mn - mb + half
    out["ci_lo"], out["ci_hi"] = float(lo), float(hi)
    return out


__all__ = [
    "MIN_CLUSTERS",
    "RESAMPLES",
    "Agg",
    "Clusters",
    "clusters",
    "discordance",
    "discordance_line",
    "drift",
    "mean_ci",
    "pvalue",
    "share",
]
