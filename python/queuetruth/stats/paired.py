"""Paired tests (SPEC 5.7.5): McNemar on discordant counts, exact binomial, Wilcoxon signed-rank, paired permutation.

McNemar: `chi2_cc = (|b - c| - 1)^2 / (b + c)` (0 if `b + c = 0`), `p_chi2 = chi2.sf(chi2_cc, 1)`,
`p_exact = min(1, 2 * sum_{i <= min(b, c)} C(b + c, i) / 2^(b + c))` (exact rational arithmetic up to
`b + c = 2000`, scipy's binomial CDF above), `odds_ratio = c / b` and its Wald CI
`exp(ln(c / b) +- 1.96 sqrt(1/b + 1/c))` (null if `b` or `c` is 0).
Paired permutation: per permutation each order's two arms are swapped with probability 1/2 (`rng.random(n) < 0.5`,
`rng = numpy.random.default_rng(seed)`); `p = (1 + #{D* >= D}) / (1 + B)` where `>=` allows a relative tolerance of
1e-12 so that permutations reproducing the observed statistic up to summation order count as ties.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable
from fractions import Fraction

import numpy as np
from scipy import stats as sps

from queuetruth.stats._types import McNemarResult, TestResult, WilcoxonResult

EXACT_N_MAX = 2000
WALD_Z = 1.96


def _sum_lower_tail_half(m: int, n: int) -> Fraction:
    """`sum_{i=0}^{m} C(n, i) / 2^n` as an exact fraction."""
    term = 1
    total = 1
    for i in range(m):
        term = term * (n - i) // (i + 1)
        total += term
    return Fraction(total, 1 << n)


def binom_exact_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """Exact two-sided binomial p-value; for `p = 0.5`: `min(1, 2 * P(X <= min(k, n - k)))`, else scipy's binomtest."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must lie in [0, 1], got {p}")
    if n == 0:
        return 1.0
    if p == 0.5:
        m = min(k, n - k)
        if n <= EXACT_N_MAX:
            return float(min(Fraction(1), 2 * _sum_lower_tail_half(m, n)))
        return float(min(1.0, 2.0 * float(sps.binom.cdf(m, n, 0.5))))
    return float(sps.binomtest(k, n, p).pvalue)


def mcnemar(b: int, c: int) -> McNemarResult:
    """McNemar test: chi2_cc, p_chi2, p_exact, odds_ratio, Wald CI on the log odds ratio."""
    b, c = int(b), int(c)
    if b < 0 or c < 0:
        raise ValueError(f"discordant counts must be >= 0, got b={b}, c={c}")
    n = b + c
    chi2_cc = 0.0 if n == 0 else (abs(b - c) - 1) ** 2 / n
    p_chi2 = float(sps.chi2.sf(chi2_cc, 1))
    p_exact = binom_exact_two_sided(min(b, c), n, 0.5)
    odds_ratio = None if b == 0 or c == 0 else c / b
    or_ci = None
    if b > 0 and c > 0:
        lor = math.log(c / b)
        half = WALD_Z * math.sqrt(1.0 / b + 1.0 / c)
        or_ci = (math.exp(lor - half), math.exp(lor + half))
    return McNemarResult(b=b, c=c, chi2_cc=chi2_cc, p_chi2=p_chi2, p_exact=p_exact, odds_ratio=odds_ratio, or_ci=or_ci)


def wilcoxon_signed_rank(d: np.ndarray, zero_method: str = "wilcox") -> WilcoxonResult:
    """Two-sided Wilcoxon signed-rank test, `scipy.stats.wilcoxon(d, zero_method, method="auto")`; NaN if no data."""
    x = np.asarray(d, dtype=float).ravel()
    if x.size == 0 or (zero_method == "wilcox" and not np.any(x != 0)):
        return WilcoxonResult(statistic=float("nan"), pvalue=float("nan"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = sps.wilcoxon(x, zero_method=zero_method, alternative="two-sided", method="auto")
    return WilcoxonResult(statistic=float(r.statistic), pvalue=float(r.pvalue))


def paired_permutation(
    x_live: np.ndarray, x_sim: np.ndarray, stat: Callable[..., float], b: int, seed: int
) -> TestResult:
    """Paired permutation test: `stat(x_live, x_sim)` vs `b` arm-swapped resamples (rows = orders)."""
    xl = np.asarray(x_live)
    xs = np.asarray(x_sim)
    if xl.shape != xs.shape:
        raise ValueError(f"x_live and x_sim must have the same shape, got {xl.shape} and {xs.shape}")
    observed = float(stat(xl, xs))
    if not np.isfinite(observed):
        return TestResult(statistic=observed, p_value=float("nan"))
    rng = np.random.default_rng(seed)
    n = xl.shape[0] if xl.ndim else 0
    tol = 1e-12 * max(1.0, abs(observed))
    hits = 0
    for _ in range(int(b)):
        swap = (rng.random(n) < 0.5).reshape((n,) + (1,) * (xl.ndim - 1))
        d = float(stat(np.where(swap, xs, xl), np.where(swap, xl, xs)))
        if d >= observed - tol:
            hits += 1
    return TestResult(statistic=observed, p_value=(1 + hits) / (1 + int(b)))
