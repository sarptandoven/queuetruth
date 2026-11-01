"""The whole loop in hftbacktest itself (2.4.4, in its own venv, `QT_HBT_PY`): `prepare`, `run`, `score`, and the
code `calibrate` writes running as the backtest's asset. Skipped without the venv."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from queuetruth_hftbacktest import Prepared, QtHbtError, calibrate, run, score

pytestmark = pytest.mark.requires_hftbacktest

NO_PARTIAL = (
    "from hftbacktest import BacktestAsset\n"
    "asset = BacktestAsset().power_prob_queue_model3(2.0).no_partial_fill_exchange()\n"
)


def test_run_and_score_in_hftbacktest(prepared: Prepared, hbt_py: Path, tmp_path: Path) -> None:
    out = run(prepared, python=hbt_py, code=NO_PARTIAL, out=tmp_path / "hbt.jsonl")
    summ = json.loads(Path(str(out) + ".summary.json").read_text())
    assert summ["submitted"] == prepared.n_orders and "fatal" not in summ
    s = score(prepared, out, out=tmp_path / "s")
    assert s.n_scored == s.n_orders == prepared.n_orders and not any("stopped" in w for w in s.warnings)
    assert s.hftbacktest["hftbacktest_version"] == "2.4.4"
    assert s.referee["simulator"] == {"name": "hftbacktest", "version": "2.4.4"}
    # every order is matched (no unknown order keys) and the fill rates are close to exact on this flow
    assert "unknown_order" not in s.referee["by_reason"]
    for h in s.horizons:
        assert h.n > 0 and abs(h.bias) < 0.05


def test_312_stop_is_reported_and_scored_up_to_it(prepared: Prepared, hbt_py: Path, tmp_path: Path) -> None:
    """With `partial_fill_exchange`, released 2.4.4 stops on this flow with error 14 (hftbacktest #312)."""
    code = NO_PARTIAL.replace("no_partial_fill_exchange", "partial_fill_exchange")
    with pytest.raises(QtHbtError) as e:
        run(prepared, python=hbt_py, code=code, out=tmp_path / "hbt.jsonl")
    assert e.value.code == "QT-USAGE-001" and e.value.details["fatal"]["rc"] == 14
    out = run(prepared, python=hbt_py, code=code, out=tmp_path / "hbt.jsonl", allow_stop=True)
    s = score(prepared, out, out=tmp_path / "s")
    assert 0 < s.n_scored < s.n_orders and any("stopped with error 14" in w for w in s.warnings)


def test_calibrated_code_runs_in_hftbacktest(synth: Path, prepared: Prepared, hbt_py: Path, tmp_path: Path) -> None:
    cal = calibrate(synth, "2026-06-15", "2026-06-16", out=tmp_path / "cal", n_orders=1500, seed=3, bootstrap_b=20,
                    families=["power3"])  # fmt: skip
    code = cal.code.replace("partial_fill_exchange", "no_partial_fill_exchange")  # #312 aside
    out = run(prepared, python=hbt_py, code=code, out=tmp_path / "hbt.jsonl")
    s = score(prepared, out, out=tmp_path / "s")
    assert s.n_scored == prepared.n_orders


def test_run_errors(prepared: Prepared, tmp_path: Path) -> None:
    with pytest.raises(QtHbtError) as e:
        run(prepared, python=tmp_path / "no-python", code=NO_PARTIAL)
    assert e.value.code == "QT-USAGE-001"
    with pytest.raises(QtHbtError) as e:
        run(prepared, python="python", code=NO_PARTIAL, snippet="x.py")
    assert e.value.code == "QT-CONFIG-001"
