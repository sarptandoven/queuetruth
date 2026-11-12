"""Pinned outputs of every interval estimator that now lives in `queuetruth.stats.intervals`, through the callers
that used to compute them on their own. `interval_pins.json` holds the outputs (as `float.hex`, so the comparison is
bitwise) computed before the estimators moved; never regenerate it: a difference means a published interval changed.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from queuetruth.eval import toxicity as TX
from queuetruth.eval import zoo as ZO
from queuetruth.forecast import calibration as FC
from queuetruth.forecast import inference as FI
from queuetruth.live.importers import freqtrade as FT
from queuetruth.model import card as MC
from queuetruth.model.conformal import clopper_pearson
from queuetruth.report.compute import wilson
from queuetruth.score import shortfall as SF
from queuetruth.stats import block_counts
from queuetruth.stats.calibration import reliability_table

PINS = json.loads((Path(__file__).parent / "interval_pins.json").read_text())


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


def test_interval_outputs_unchanged() -> None:
    got = outputs()
    assert set(got) == set(PINS)
    for key, want in PINS.items():
        assert got[key] == want, key
