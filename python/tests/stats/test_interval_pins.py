"""Pinned outputs of every interval estimator that now lives in `queuetruth.stats.intervals`, through the callers
that used to compute them on their own. Two checks, both bitwise:

- Same machine: every caller's output equals that of the code it replaced, kept below as `reference_outputs` (the
  pre-refactor implementations, copied verbatim). This holds on every platform CI runs.
- Recorded: `interval_pins.json` holds the outputs (as `float.hex`) computed before the estimators moved, on Linux
  x86_64; never regenerate it. The platform-stable keys (`STABLE`: pure Python arithmetic, elementwise numpy with
  correctly rounded operations, and the numpy Generator's draws) are compared on every platform. The others go
  through scipy's beta quantile (`clopper_pearson`) or numpy reductions and BLAS products, whose last bit can differ
  between platforms and libraries, so they are compared with the record only where it was made.
"""

from __future__ import annotations

import json
import math
import platform
import sys
from pathlib import Path

import numpy as np
from scipy.stats import beta as beta_dist

from queuetruth.eval import toxicity as TX
from queuetruth.eval import zoo as ZO
from queuetruth.forecast import calibration as FC
from queuetruth.forecast import inference as FI
from queuetruth.live.importers import freqtrade as FT
from queuetruth.model import card as MC
from queuetruth.model.conformal import clopper_pearson
from queuetruth.report.compute import wilson
from queuetruth.score import shortfall as SF
from queuetruth.stats import block_counts, bootstrap_seed, percentile_ci
from queuetruth.stats.calibration import Z975, reliability_table

PINS = json.loads((Path(__file__).parent / "interval_pins.json").read_text())
#: keys whose recorded bits hold on every platform (module docstring)
STABLE = ("report_wilson", "freqtrade_wilson", "forecast_wilson", "reliability_table", "chunks_equal_block_counts")
#: where `interval_pins.json` was recorded
RECORDED_ON = sys.platform == "linux" and platform.machine() in ("x86_64", "AMD64")


def h(x: object) -> object:
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        return [h(v) for v in x]
    if isinstance(x, dict):
        return {k: h(v) for k, v in x.items()}
    if isinstance(x, (int, np.integer)) and not isinstance(x, bool):
        return int(x)
    return float(x).hex()


def outputs() -> dict:
    """Every pinned output, computed by today's code."""
    KN = [(0, 10), (1, 10), (5, 10), (10, 10), (3, 7), (0, 1), (1, 1), (37, 1000), (999, 1000)]
    out = {}
    out["report_wilson"] = [h(wilson(k, n)) for k, n in KN] + [h(wilson(0, 0))]
    out["freqtrade_wilson"] = [h(FT._wilson(k, n)) for k, n in KN] + [FT._wilson(0, 0)]
    out["forecast_wilson"] = [h(FC._wilson(k / n, n)) for k, n in KN]
    out["clopper_pearson"] = [h(clopper_pearson(k, n)) for k, n in KN] + [list(clopper_pearson(0, 0))]
    rng = np.random.default_rng(11)
    p = rng.random(300)
    y = (rng.random(300) < p).astype(float)
    t = reliability_table(p, y, 10)
    out["reliability_table"] = {k: h(list(t[k])) for k in ("rate_lo", "rate_hi")}

    num = rng.random(40)
    den = rng.random(40) + (rng.random(40) < 0.2)
    den[[3, 7]] = 0.0
    blocks = rng.integers(0, 8, 40)
    counts = block_counts(8, 300, 5)
    out["toxicity_boot"] = h(TX._Boot(counts).stat(num, den, blocks)[0])
    out["zoo_ratio"] = h(list(ZO._Ratio(counts.astype(float))(num, den, blocks)))
    tm, tc = rng.random(40), rng.random(40) + 0.1
    out["card_bss"] = h(list(MC._bss_ci(tm, tc, blocks, 300, 9, 1_000_000_000)))
    cl = rng.integers(0, 6, 40)
    out["freqtrade_cluster"] = h(list(FT._cluster_ci(num, den, cl)[0]))
    orders = [{"session_date": 18000 + int(i % 3), "ts_entry": int(1e9) * int(i * 977)} for i in range(40)]
    cis, k, kind = SF.block_mean_cis(orders, {"a": num, "b": tm}, 300, "auto", 4, "t")
    out["shortfall"] = {"cis": h(cis), "k": k, "kind": kind}
    out["mean_cluster_ci"] = h(list(FI.mean_cluster_ci(num, cl, 300, 2, "x")))
    chunks = np.concatenate(list(FI.cluster_count_chunks(6, 300, 13)))
    out["chunks_equal_block_counts"] = bool((chunks == block_counts(6, 300, 13)).all())
    return out


# ---- the code each caller ran before the estimators moved (copied verbatim, renamed) ---------------------------


def _old_wilson_report(k: float, n: float, z: float = 1.959963984540054) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    phat = k / n
    den = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / den
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / den
    lo = 0.0 if k <= 0 else max(0.0, centre - half)
    hi = 1.0 if k >= n else min(1.0, centre + half)
    return (lo, hi)


def _old_wilson_freqtrade(k: int, n: int) -> tuple[float, float] | None:
    if n == 0:
        return None
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _old_wilson_forecast(rate: float, n: int) -> tuple[float, float]:
    z2 = Z975 * Z975
    centre = (rate + z2 / (2 * n)) / (1 + z2 / n)
    half = Z975 * np.sqrt(rate * (1 - rate) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return float(centre - half), float(centre + half)


def _old_clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    a = (1.0 - level) / 2.0
    lo = 0.0 if k == 0 else float(beta_dist.ppf(a, k, n - k + 1))
    hi = 1.0 if k == n else float(beta_dist.ppf(1.0 - a, k + 1, n - k))
    return lo, hi


def _old_reliability(rate: np.ndarray, count: np.ndarray) -> dict[str, np.ndarray]:
    z2 = Z975 * Z975
    centre = (rate + z2 / (2 * count)) / (1 + z2 / count)
    half = Z975 * np.sqrt(rate * (1 - rate) / count + z2 / (4 * count * count)) / (1 + z2 / count)
    return {"rate_lo": centre - half, "rate_hi": centre + half}


def _none(x: float) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


def _old_toxicity(counts: np.ndarray, num: np.ndarray, den: np.ndarray, blocks: np.ndarray) -> dict:
    counts = counts.astype(float)
    nb = counts.shape[1]
    num = np.where(den > 0, num, 0.0)
    ns = np.bincount(blocks, weights=num, minlength=nb)
    ds = np.bincount(blocks, weights=den, minlength=nb)
    value = float(ns.sum()) / float(ds.sum())
    rn = counts @ ns
    rd = counts @ ds
    reps = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
    lo, hi = percentile_ci(reps, 0.05)
    return {"value": _none(value), "ci_lo": _none(lo), "ci_hi": _none(hi), "n": int((den > 0).sum())}


def _old_zoo(counts: np.ndarray, num: np.ndarray, den: np.ndarray, blocks: np.ndarray) -> tuple[float, ...]:
    nb = counts.shape[1]
    ns = np.bincount(blocks, weights=num, minlength=nb)
    ds = np.bincount(blocks, weights=den, minlength=nb)
    total = float(ds.sum())
    value = float(ns.sum()) / total if total > 0 else float("nan")
    rn = (counts * ns[None, :]).sum(axis=1)
    rd = (counts * ds[None, :]).sum(axis=1)
    reps = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
    lo, hi = percentile_ci(reps, 0.05)
    return value, lo, hi


def _old_bss(tm: np.ndarray, tc: np.ndarray, blocks: np.ndarray, b: int, seed: int, tau: int) -> tuple:
    n_blocks = int(blocks.max(initial=-1)) + 1
    sm = np.bincount(blocks, weights=tm, minlength=n_blocks)
    sc = np.bincount(blocks, weights=tc, minlength=n_blocks)
    counts = block_counts(n_blocks, b, bootstrap_seed(seed, f"bss_vs_climatology:{tau}")).astype(np.float64)
    bm, bc = counts @ sm, counts @ sc
    reps = np.where(bc > 0, 1.0 - bm / np.where(bc > 0, bc, 1.0), np.nan)
    lo, hi = percentile_ci(reps, 0.05)
    return _none(lo), _none(hi)


def _old_cluster(num: np.ndarray, den: np.ndarray, cl: np.ndarray) -> tuple[float, float] | None:
    ids, inv = np.unique(cl, return_inverse=True)
    k = int(ids.size)
    n_k = np.bincount(inv, weights=num.astype(float), minlength=k)
    d_k = np.bincount(inv, weights=den.astype(float), minlength=k)
    rng = np.random.default_rng(0)
    draws = rng.integers(0, k, size=(FT.AUDIT_BOOTSTRAP_DRAWS, k))
    nn, dd = n_k[draws].sum(axis=1), d_k[draws].sum(axis=1)
    ratio = np.divide(nn, dd, out=np.full(nn.shape, np.nan), where=dd > 0)
    ratio = ratio[np.isfinite(ratio)]
    if ratio.size == 0:
        return None
    lo, hi = np.percentile(ratio, [2.5, 97.5])
    return (float(lo), float(hi))


def _old_shortfall(orders: list, values: dict, b: int, kind: str, seed: int, test_id: str) -> tuple:
    labels, kind_ = SF.block_labels(list(orders), kind)
    _, inv = np.unique(labels, return_inverse=True)
    k = int(inv.max()) + 1
    counts = block_counts(k, b, bootstrap_seed(seed, test_id)).astype(float)
    den = counts @ np.bincount(inv, minlength=k).astype(float)
    out = {}
    with np.errstate(all="ignore"):
        for c, vals in values.items():
            x = np.asarray(vals, dtype=float)
            reps = (counts @ np.bincount(inv, weights=x, minlength=k)) / den
            a, z = percentile_ci(reps, 0.05)
            out[c] = (a if math.isfinite(a) else None, z if math.isfinite(z) else None)
    return out, k, kind_


def _old_chunks(n_clusters: int, b: int, seed: int):
    rng = np.random.default_rng(seed)
    step = max(1, 4_000_000 // max(n_clusters, 1))
    done = 0
    while done < b:
        c = min(step, b - done)
        draws = rng.integers(0, n_clusters, size=(c, n_clusters))
        flat = (np.arange(c, dtype=np.int64)[:, None] * n_clusters + draws).ravel()
        yield np.bincount(flat, minlength=c * n_clusters).reshape(c, n_clusters).astype(float)
        done += c


def _old_mean_cluster_ci(values: np.ndarray, clusters: np.ndarray, b: int, seed: int, test_id: str) -> tuple:
    v = np.asarray(values, dtype=float)
    cl = np.asarray(clusters, dtype=np.int64)
    g = int(cl.max()) + 1
    s = np.bincount(cl, weights=v, minlength=g)
    size = np.bincount(cl, minlength=g).astype(float)
    keep = size > 0
    s, size = s[keep], size[keep]
    est = float(s.sum() / size.sum())
    reps = [(c @ s) / (c @ size) for c in _old_chunks(int(size.size), b, bootstrap_seed(seed, test_id))]
    lo, hi = percentile_ci(np.concatenate(reps), 0.05)
    return est, lo, hi


def reference_outputs() -> dict:
    """`outputs()` computed by the pre-refactor code, on this machine."""
    KN = [(0, 10), (1, 10), (5, 10), (10, 10), (3, 7), (0, 1), (1, 1), (37, 1000), (999, 1000)]
    out = {}
    out["report_wilson"] = [h(_old_wilson_report(k, n)) for k, n in KN] + [h(_old_wilson_report(0, 0))]
    out["freqtrade_wilson"] = [h(_old_wilson_freqtrade(k, n)) for k, n in KN] + [_old_wilson_freqtrade(0, 0)]
    out["forecast_wilson"] = [h(_old_wilson_forecast(k / n, n)) for k, n in KN]
    out["clopper_pearson"] = [h(_old_clopper_pearson(k, n)) for k, n in KN] + [list(_old_clopper_pearson(0, 0))]
    rng = np.random.default_rng(11)
    p = rng.random(300)
    y = (rng.random(300) < p).astype(float)
    t = reliability_table(p, y, 10)  # its bins and rates are unchanged; only the bounds moved
    old = _old_reliability(t["observed_rate"], t["count"].astype(float))
    out["reliability_table"] = {k: h(list(old[k])) for k in ("rate_lo", "rate_hi")}

    num = rng.random(40)
    den = rng.random(40) + (rng.random(40) < 0.2)
    den[[3, 7]] = 0.0
    blocks = rng.integers(0, 8, 40)
    counts = block_counts(8, 300, 5)
    out["toxicity_boot"] = h(_old_toxicity(counts, num, den, blocks))
    out["zoo_ratio"] = h(list(_old_zoo(counts.astype(float), num, den, blocks)))
    tm, tc = rng.random(40), rng.random(40) + 0.1
    out["card_bss"] = h(list(_old_bss(tm, tc, blocks, 300, 9, 1_000_000_000)))
    cl = rng.integers(0, 6, 40)
    out["freqtrade_cluster"] = h(list(_old_cluster(num, den, cl)))
    orders = [{"session_date": 18000 + int(i % 3), "ts_entry": int(1e9) * int(i * 977)} for i in range(40)]
    cis, k, kind = _old_shortfall(orders, {"a": num, "b": tm}, 300, "auto", 4, "t")
    out["shortfall"] = {"cis": h(cis), "k": k, "kind": kind}
    out["mean_cluster_ci"] = h(list(_old_mean_cluster_ci(num, cl, 300, 2, "x")))
    chunks = np.concatenate(list(_old_chunks(6, 300, 13)))
    out["chunks_equal_block_counts"] = bool((chunks == block_counts(6, 300, 13)).all())
    return out


def test_interval_outputs_equal_the_old_code_on_this_machine() -> None:
    got, want = outputs(), reference_outputs()
    assert set(got) == set(want) == set(PINS)
    for key in want:
        assert got[key] == want[key], key


def test_interval_outputs_unchanged() -> None:
    got = outputs()
    assert set(got) == set(PINS)
    for key, want in PINS.items():
        if key in STABLE or RECORDED_ON:
            assert got[key] == want, key


def test_callers_use_the_shared_estimators() -> None:
    """Every routed caller now reaches `queuetruth.stats.intervals`; the pins above prove the numbers did not move."""
    from queuetruth import stats
    from queuetruth.stats import intervals

    assert FI.cluster_count_chunks.__module__ == FI.__name__  # kept, as a thin wrapper of block_counts_chunks
    assert stats.percentile_ci is intervals.percentile_ci
    assert TX.percentile_ci is intervals.percentile_ci
    assert FT.wilson is intervals.wilson
    assert SF.stats.ratio_replicates is intervals.ratio_replicates
