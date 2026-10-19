"""Order statistics, Mann-Whitney U and bootstrap comparison for benchmark samples (stdlib only).

Semantics:
- percentiles use linear interpolation between order statistics (Hyndman-Fan type 7, numpy's default);
- `mann_whitney(x, y)` tests H1 "x tends to be larger than y" (one-sided); exact null distribution when there are
  no ties and m + n <= 60, else the normal approximation with tie correction and continuity correction;
- `compare()` works on *costs* (seconds per unit of work, lower is better), so a throughput metric is inverted first;
  `slowdown = median(cost_cur) / median(cost_base) - 1`;
- the bootstrap resamples both groups independently with `random.Random(seed)` (deterministic) and reports the
  percentile interval of the slowdown.
"""

from __future__ import annotations

import math
import random
import statistics
from functools import cache

__all__ = [
    "bootstrap_slowdown_ci",
    "compare",
    "mann_whitney",
    "min_attainable_p",
    "percentile",
    "summarize",
    "to_cost",
]


def percentile(xs: list[float], q: float) -> float:
    """Type-7 percentile of `xs` at `q` in [0, 1]."""
    if not xs:
        raise ValueError("percentile of empty sample")
    s = sorted(xs)
    h = (len(s) - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (h - lo) * (s[hi] - s[lo])


def summarize(xs: list[float]) -> dict:
    """n, median, mean, stdev, cv, p10, p90, min, max, mad (raw median absolute deviation)."""
    n = len(xs)
    if n == 0:
        return {
            "n": 0,
            "median": None,
            "mean": None,
            "stdev": None,
            "cv": None,
            "p10": None,
            "p90": None,
            "min": None,
            "max": None,
            "mad": None,
        }
    med = statistics.median(xs)
    mean = statistics.fmean(xs)
    sd = statistics.stdev(xs) if n > 1 else 0.0
    return {
        "n": n,
        "median": med,
        "mean": mean,
        "stdev": sd,
        "cv": (sd / mean) if mean else None,
        "p10": percentile(xs, 0.10),
        "p90": percentile(xs, 0.90),
        "min": min(xs),
        "max": max(xs),
        "mad": statistics.median([abs(v - med) for v in xs]),
    }


@cache
def _u_counts(m: int, n: int) -> tuple[int, ...]:
    """Number of orderings of m x's and n y's (no ties) giving U = #{(i, j): x_i > y_j} = u, for u = 0..m*n."""
    if m == 0 or n == 0:
        return (1,)
    a = _u_counts(m - 1, n)  # the largest element is an x: it beats all n y's
    b = _u_counts(m, n - 1)  # the largest element is a y: contributes nothing
    out = [0] * (m * n + 1)
    for u, c in enumerate(a):
        out[u + n] += c
    for u, c in enumerate(b):
        out[u] += c
    return tuple(out)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def mann_whitney(x: list[float], y: list[float]) -> dict:
    """One-sided Mann-Whitney U test of H1: x stochastically larger than y. Returns {u, p, method}."""
    m, n = len(x), len(y)
    if m == 0 or n == 0:
        raise ValueError("mann_whitney needs two non-empty samples")
    u = 0.0
    for xi in x:
        for yj in y:
            u += 1.0 if xi > yj else (0.5 if xi == yj else 0.0)
    pooled = list(x) + list(y)
    ties = len(set(pooled)) != len(pooled)
    if not ties and m + n <= 60:
        counts = _u_counts(m, n)
        total = math.comb(m + n, m)
        k = round(u)
        p = sum(counts[k:]) / total
        return {"u": u, "p": p, "method": "exact"}
    big_n = m + n
    tie_term = 0.0
    for v in set(pooled):
        t = pooled.count(v)
        tie_term += t**3 - t
    var = m * n / 12.0 * ((big_n + 1) - tie_term / (big_n * (big_n - 1)))
    if var <= 0:
        return {"u": u, "p": 1.0, "method": "normal"}
    z = (u - m * n / 2.0 - 0.5) / math.sqrt(var)
    return {"u": u, "p": 1.0 - _phi(z), "method": "normal"}


def min_attainable_p(m: int, n: int) -> float:
    """Smallest one-sided exact p-value for sample sizes m, n (complete separation): 1 / C(m+n, m)."""
    return 1.0 / math.comb(m + n, m)


def bootstrap_slowdown_ci(
    base_cost: list[float], cur_cost: list[float], b: int = 2000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile bootstrap interval of median(cur)/median(base) - 1, groups resampled independently."""
    rng = random.Random(seed)
    m, n = len(base_cost), len(cur_cost)
    vals = []
    for _ in range(b):
        mb = statistics.median([base_cost[rng.randrange(m)] for _ in range(m)])
        mc = statistics.median([cur_cost[rng.randrange(n)] for _ in range(n)])
        vals.append(mc / mb - 1.0)
    return (percentile(vals, alpha / 2), percentile(vals, 1 - alpha / 2))


def to_cost(values: list[float], direction: str) -> list[float]:
    """Map metric samples to costs (lower is better)."""
    if direction == "higher_is_better":
        return [1.0 / v for v in values if v > 0]
    if direction == "lower_is_better":
        return list(values)
    raise ValueError(f"unknown direction {direction!r}")


def compare(
    base: list[float],
    cur: list[float],
    direction: str,
    threshold: float = 0.20,
    alpha: float = 0.05,
    b: int = 2000,
    seed: int = 0,
) -> dict:
    """Classify cur vs base as regression | suspect | ok | improved | insufficient (rules in the body below)."""
    bc, cc = to_cost(base, direction), to_cost(cur, direction)
    out: dict = {"n_base": len(bc), "n_cur": len(cc), "threshold": threshold, "alpha": alpha}
    if len(bc) < 2 or len(cc) < 2:
        out.update(
            status="insufficient",
            slowdown=None,
            ci=None,
            p_slower=None,
            p_faster=None,
            min_attainable_p=None,
            reason="fewer than 2 samples in a group",
        )
        return out
    slowdown = statistics.median(cc) / statistics.median(bc) - 1.0
    p_slower = mann_whitney(cc, bc)["p"]
    p_faster = mann_whitney(bc, cc)["p"]
    pmin = min_attainable_p(len(bc), len(cc))
    ci = bootstrap_slowdown_ci(bc, cc, b=b, seed=seed, alpha=alpha)
    out.update(slowdown=slowdown, ci=list(ci), p_slower=p_slower, p_faster=p_faster, min_attainable_p=pmin)
    if slowdown > threshold:
        if pmin > alpha:
            out.update(status="insufficient", reason=f"min attainable p {pmin:.3f} > alpha; add repetitions")
        elif p_slower <= alpha:
            out.update(status="regression", reason=None)
        else:
            out.update(status="suspect", reason="slowdown above threshold but not significant")
    elif slowdown < -threshold and p_faster <= alpha:
        out.update(status="improved", reason="consider re-baselining")
    else:
        out.update(status="ok", reason=None)
    return out
