"""O-MOD-5: `rcll`, `ipcw_logloss` and IPCW Brier equal hand-computed values on a 6-order example (SPEC 5.5.7).

Grid tau_1 = 1 s, tau_2 = 2 s. Orders (e in s, cause): A 0.5 fill, B 1.5 cancel, C 0.8 censor, D 3.0 censor,
E 2.0 fill, F 1.0 censor. KM of censoring (censorings at 0.8, 1.0, 3.0; risk sets 5, 4, 1):
G(0.8) = 4/5, G(1.0) = 4/5 * 3/4 = 0.6, G(3.0) = 0. Left limits used: G(0.5-) = 1, G(1-) = 0.8, G(1.5-) = G(2-) = 0.6.
At 1 s: A event (w 1), C censored before (w 0), B D E survivors and F censored exactly at 1 s = survivor (w 1/0.8).
At 2 s: A, B, E events (w 1, 1/0.6, 1/0.6), D survivor (w 1/0.6), C and F censored before (w 0).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from queuetruth.model import (
    auc_at,
    censoring_km,
    cif_from_hazards,
    ibs,
    ipcw_brier,
    ipcw_logloss,
    rcll,
)

NS = 1_000_000_000
GRID = [1 * NS, 2 * NS]
E = np.array([0.5, 1.5, 0.8, 3.0, 2.0, 1.0]) * NS
E = E.astype(np.int64)
CAUSE = np.array(["fill", "cancel", "censor", "censor", "fill", "censor"])
HF = np.array([[0.2, 0.3], [0.1, 0.2], [0.5, 0.5], [0.1, 0.2], [0.3, 0.4], [0.25, 0.25]])
HC = np.array([[0.1, 0.1], [0.3, 0.4], [0.1, 0.1], [0.1, 0.2], [0.2, 0.1], [0.25, 0.25]])


def test_o_mod_5_censoring_km() -> None:
    g = censoring_km(E, CAUSE)
    assert g.left(np.array([0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 4.0]) * NS) == pytest.approx([1, 1, 0.8, 0.6, 0.6, 0.6, 0])


def test_o_mod_5_rcll() -> None:
    # A: h_f(1) = 0.2; B: h_c(2) S(1) = 0.4 * 0.6; C: S(0) = 1; D: S(2) = 0.8 * 0.6; E: h_f(2) S(1) = 0.4 * 0.5;
    # F: censored at tau_1 -> S(1) = 0.5.
    expected = -np.mean(np.log([0.2, 0.24, 1.0, 0.48, 0.2, 0.5]))
    assert rcll(HF, HC, E, CAUSE, GRID) == pytest.approx(expected, rel=1e-12)


def test_o_mod_5_cifs() -> None:
    ff, fc, s = cif_from_hazards(HF, HC)
    assert ff[:, 1] == pytest.approx([0.41, 0.22, 0.7, 0.26, 0.5, 0.375])
    assert fc[:, 1] == pytest.approx([0.17, 0.54, 0.14, 0.26, 0.25, 0.375])
    assert s[:, 1] == pytest.approx([0.42, 0.24, 0.16, 0.48, 0.25, 0.25])


def test_o_mod_5_ipcw_brier() -> None:
    ff, _, _ = cif_from_hazards(HF, HC)
    bs1 = (0.64 + 1.25 * 0.01 + 0.0 + 1.25 * 0.01 + 1.25 * 0.09 + 1.25 * 0.0625) / 6
    bs2 = (0.59**2 + 0.22**2 / 0.6 + 0.0 + 0.26**2 / 0.6 + 0.5**2 / 0.6 + 0.0) / 6
    assert ipcw_brier(ff[:, 0], E, CAUSE, GRID[0]) == pytest.approx(bs1, rel=1e-12)
    assert ipcw_brier(ff[:, 1], E, CAUSE, GRID[1]) == pytest.approx(bs2, rel=1e-12)
    assert ibs([bs1, bs2], GRID) == pytest.approx((bs1 * 1 + bs2 * 1) / 2, rel=1e-12)


def test_o_mod_5_ipcw_logloss() -> None:
    ff, fc, _ = cif_from_hazards(HF, HC)
    ll1 = (
        -(
            math.log(0.2)
            + 1.25 * math.log(0.6)
            + 0.0
            + 1.25 * math.log(0.8)
            + 1.25 * math.log(0.5)
            + 1.25 * math.log(0.5)
        )
        / 6
    )
    ll2 = -(math.log(0.41) + math.log(0.54) / 0.6 + 0.0 + math.log(0.48) / 0.6 + math.log(0.5) / 0.6 + 0.0) / 6
    assert ipcw_logloss(ff[:, 0], fc[:, 0], E, CAUSE, GRID[0]) == pytest.approx(ll1, rel=1e-12)
    assert ipcw_logloss(ff[:, 1], fc[:, 1], E, CAUSE, GRID[1]) == pytest.approx(ll2, rel=1e-12)


def test_scoring_accepts_integer_cause_codes_and_floors() -> None:
    codes = np.array([1, 2, 0, 0, 1, 0])
    ff, _, _ = cif_from_hazards(HF, HC)
    assert ipcw_brier(ff[:, 1], E, codes, GRID[1]) == ipcw_brier(ff[:, 1], E, CAUSE, GRID[1])
    # A zero probability for an observed fill is floored at 1e-12, not -inf.
    z = np.zeros(6)
    assert math.isfinite(ipcw_logloss(z, z, E, CAUSE, GRID[0]))
    # Rows censored before tau carry weight 0.
    e = np.array([1, 2, 3, 10]) * NS
    w = ipcw_brier(np.zeros(4), e, np.array(["censor", "censor", "censor", "censor"]), 5 * NS)
    assert w == 0.0
    # G floored at 0.05: 30 censorings before tau leave G(tau-) = 1/31; the lone survivor gets weight 20.
    e2 = np.append(np.arange(1, 31), 100) * NS
    c2 = np.array(["censor"] * 31)
    assert censoring_km(e2, c2).left(50 * NS) == pytest.approx(1 / 31)
    assert ipcw_brier(np.full(31, 0.5), e2, c2, 50 * NS) == pytest.approx(20 * 0.25 / 31)


def test_auc_hand_example() -> None:
    # At 2 s: cases A (risk 0.41, w 1) and E (0.5, w 1/0.6); controls B (cancel, 0.22, w 1/0.6), D (0.26, w 1/0.6).
    ff, _, _ = cif_from_hazards(HF, HC)
    assert auc_at(E, CAUSE, ff[:, 1], GRID[1]) == pytest.approx(1.0)
    risk = np.array([0.24, 0.22, 0.0, 0.26, 0.5, 0.0])  # A ties B? no: A 0.24 beats B 0.22, loses to D 0.26
    wa, we, wb, wd = 1.0, 1 / 0.6, 1 / 0.6, 1 / 0.6
    num = wa * wb * 1 + wa * wd * 0 + we * wb + we * wd
    den = (wa + we) * (wb + wd)
    assert auc_at(E, CAUSE, risk, GRID[1]) == pytest.approx(num / den)
