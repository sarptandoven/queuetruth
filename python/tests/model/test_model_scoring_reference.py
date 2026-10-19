"""O-MOD-5 against the shared scoring-rule reference values (`survival_scoring` section of
`scoring_rules.json`, produced by the reference prep; values copied below so the test has no external path).

The example has `h_max = tau_J = 5 s` and one order censored exactly at the cap. Convention: the capped order
survives the last interval (it stays in interval J's risk set and scores `S(tau_J)`), so `rcll` equals the
fixture's `rcll_alt_cap_censor_counts_as_survival`. `ipcw_logloss` / IPCW Brier at 1 s and 2 s equal the fixture;
at `tau_J` the fixture's values follow the literal reading (capped row weight 0); under the fixed reading the capped
row is a survivor with weight `1 / G(5 s-) = 1 / 0.4`, adding exactly its own term to the literal value.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from queuetruth.model import censoring_km, cif_from_hazards, ibs, ipcw_brier, ipcw_logloss, rcll

NS = 1_000_000_000
GRID = [1 * NS, 2 * NS, 5 * NS]
EVENT_S = [0.0, 1.0, 1.5, 3.0, 0.7, 5.0]
CAUSE = ["fill", "cancel", "fill", "censor", "censor", "censor"]
HF = [[0.3, 0.2, 0.1], [0.1, 0.1, 0.1], [0.2, 0.4, 0.2], [0.05, 0.05, 0.1], [0.1, 0.2, 0.3], [0.04, 0.04, 0.05]]
HC = [[0.1, 0.1, 0.2], [0.25, 0.2, 0.2], [0.05, 0.1, 0.1], [0.05, 0.1, 0.1], [0.1, 0.1, 0.1], [0.02, 0.02, 0.04]]
REF = {
    "rcll_literal": 0.6976450370605898,
    "rcll_fixed": 0.7133634836391299,
    "ipcw_logloss": {1: 0.5842480409184029, 2: 0.5721587319952511, 5: 0.4008405694730885},
    "ipcw_brier_fill": {1: 1487 / 16000, 2: 7017397 / 60000000, 5: 9129809 / 96000000},
}
REPO_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "reference" / "scoring_rules.json"


def _inputs() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    e = np.array([round(s * NS) for s in EVENT_S], dtype=np.int64)
    return e, np.array(CAUSE), np.array(HF), np.array(HC)


def test_o_mod_5_reference_rcll_fixed_reading() -> None:
    e, c, hf, hc = _inputs()
    assert rcll(hf, hc, e, c, GRID) == pytest.approx(REF["rcll_fixed"], rel=1e-12)
    assert rcll(hf, hc, e, c, GRID) != pytest.approx(REF["rcll_literal"], rel=1e-6)


def test_o_mod_5_reference_ipcw() -> None:
    e, c, hf, hc = _inputs()
    ff, fc, s = cif_from_hazards(hf, hc)
    g = censoring_km(e, c)
    assert g.left(5 * NS) == pytest.approx(0.4)
    for j, tau_s in enumerate((1, 2)):
        tau = tau_s * NS
        assert ipcw_logloss(ff[:, j], fc[:, j], e, c, tau) == pytest.approx(REF["ipcw_logloss"][tau_s], rel=1e-12)
        assert ipcw_brier(ff[:, j], e, c, tau) == pytest.approx(REF["ipcw_brier_fill"][tau_s], abs=1e-15)
    # tau_J: literal value plus the capped survivor's own term (weight 1 / 0.4, n = 6).
    s5 = 1.0 - 0.12178 - 0.074144
    assert s[5, 2] == pytest.approx(0.804076)
    ll5 = REF["ipcw_logloss"][5] - math.log(s5) / 0.4 / 6
    bs5 = REF["ipcw_brier_fill"][5] + (0.12178**2) / 0.4 / 6
    assert ipcw_logloss(ff[:, 2], fc[:, 2], e, c, 5 * NS) == pytest.approx(ll5, rel=1e-12)
    assert ipcw_brier(ff[:, 2], e, c, 5 * NS) == pytest.approx(bs5, rel=1e-12)
    bs = [REF["ipcw_brier_fill"][1], REF["ipcw_brier_fill"][2], bs5]
    assert ibs(bs, GRID) == pytest.approx((bs[0] * 1 + bs[1] * 1 + bs[2] * 3) / 5, rel=1e-12)


@pytest.mark.skipif(not REPO_FIXTURE.exists(), reason=f"missing {REPO_FIXTURE.name} in tests/fixtures/reference")
def test_o_mod_5_reference_matches_repo_fixture() -> None:
    """The committed fixture's survival_scoring values equal the copies above."""
    d = json.loads(REPO_FIXTURE.read_text())["survival_scoring"]
    assert d["rcll"] == REF["rcll_literal"]
    assert d["rcll_alt_cap_censor_counts_as_survival"] == REF["rcll_fixed"]
    assert [r["event_s"] for r in d["rows"]] == EVENT_S
    assert [r["cause"] for r in d["rows"]] == CAUSE
    assert [r["h_fill"] for r in d["rows"]] == HF
    assert [r["h_cancel"] for r in d["rows"]] == HC
    for k, v in REF["ipcw_logloss"].items():
        assert d["ipcw_logloss"][f"{float(k)}"] == v
