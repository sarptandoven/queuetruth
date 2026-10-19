"""Uno C-index and time-dependent AUC (improper, discrimination only) against brute-force definitions."""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.metrics import roc_auc_score

from queuetruth import stats


def _brute_uno(t, e, r, tau, c):
    g = stats.kaplan_meier(t, c)
    num = den = 0.0
    for i in range(t.size):
        if not e[i] or t[i] >= tau:
            continue
        w = 1.0 / g.left(t[i], before=1.0) ** 2
        for j in range(t.size):
            if t[i] < t[j]:
                den += w
                num += w * (1.0 if r[i] > r[j] else 0.5 if r[i] == r[j] else 0.0)
    return num / den


def test_uno_c_index_matches_brute_force() -> None:
    rng = np.random.default_rng(2)
    for _ in range(5):
        n = 60
        t = np.round(rng.exponential(2.0, n), 1)
        e = rng.random(n) < 0.6
        r = np.round(-t + rng.normal(0, 1, n), 1)
        got = stats.uno_c_index(t, e, r, tau=4.0)
        assert got == pytest.approx(_brute_uno(t, e, r, 4.0, ~e), rel=1e-12)


def test_auc_at_without_censoring_equals_roc_auc() -> None:
    rng = np.random.default_rng(3)
    t = rng.exponential(2.0, 200)
    r = -t + rng.normal(0, 1.5, 200)
    e = np.ones(200, bool)
    tau = 2.0
    assert stats.auc_at(t, e, r, tau) == pytest.approx(roc_auc_score(t <= tau, r), rel=1e-12)


def test_auc_at_competing_events_are_controls() -> None:
    t = np.array([1.0, 2.0, 3.0, 1.5])
    e = np.array([True, False, False, False])
    censor = np.array([False, False, True, False])  # rows 1 and 3: competing events; row 2 censored after tau
    r = np.array([0.9, 0.5, 0.1, 0.95])
    # case {0}; controls {1, 3 (competing by tau), 2 (still event-free at tau)} -> 2 of 3 pairs concordant
    assert stats.auc_at(t, e, r, 2.5, censor=censor) == pytest.approx(2 / 3)
    # without the competing-risk controls only the late row is a control
    assert stats.auc_at(t[[0, 2]], e[[0, 2]], r[[0, 2]], 2.5, censor=censor[[0, 2]]) == 1.0
