"""Open conflict 3 (E5 / E29, SPEC 5.5.7): the `bss_vs_climatology` CI uses the agreed bootstrap draw
procedure: `queuetruth.stats.block_labels` blocks, `bootstrap_seed(seed, "bss_vs_climatology:<tau>")`, one
`integers(0, n_blocks, (B, n_blocks))` draw (the `day_block_bootstrap` stream) and linear percentile quantiles,
not multinomial block counts."""

from __future__ import annotations

import numpy as np
import pytest

from queuetruth import stats
from queuetruth.model.card import _bss_ci


@pytest.mark.parametrize("seed, tau", [(0, 1_000_000_000), (42, 10_000_000_000)])
def test_bss_ci_equals_day_block_bootstrap_on_the_agreed_stream(seed: int, tau: int) -> None:
    rng = np.random.default_rng(7)
    n = 300
    blocks = rng.integers(0, 9, n)
    tm = rng.uniform(0.0, 0.3, n)
    tc = rng.uniform(0.05, 0.4, n)
    b = 400

    def stat(idx: np.ndarray) -> float:
        den = float(np.sum(tc[idx]))
        return 1.0 - float(np.sum(tm[idx])) / den if den > 0 else float("nan")

    want = stats.day_block_bootstrap(blocks, stat, b, stats.bootstrap_seed(seed, f"bss_vs_climatology:{tau}"))
    lo, hi = _bss_ci(tm, tc, blocks, b, seed, tau)
    assert lo == pytest.approx(want.lo, abs=1e-12) and hi == pytest.approx(want.hi, abs=1e-12)
    # the old multinomial draw gives a different interval on the same seed
    old = np.random.default_rng(stats.bootstrap_seed(seed, f"bss_vs_climatology:{tau}"))
    counts = old.multinomial(9, np.full(9, 1 / 9), size=b).astype(float)
    sm, sc = np.bincount(blocks, tm, 9), np.bincount(blocks, tc, 9)
    old_lo, _ = np.percentile(1.0 - (counts @ sm) / (counts @ sc), [2.5, 97.5])
    assert old_lo != pytest.approx(lo, abs=1e-12)
