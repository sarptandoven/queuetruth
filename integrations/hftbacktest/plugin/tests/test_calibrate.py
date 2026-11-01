"""`calibrate` on the synthetic dataset: the per-family fits are queuetruth's `calibrate alloc`, the winner rule, the
code is queuetruth's `export hftbacktest` snippet, and a refused fit yields no code unless forced."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
import queuetruth as qt

from queuetruth_hftbacktest import CalibrationRefused, FamilyFit, QtHbtError, calibrate, select, window

# On the synthetic dataset, these orders and windows give an exportable power3 fit (power2 and power are refused);
# swapping the days refuses all three (checked below).
EXPORTABLE = {"train": "2026-06-15", "test": "2026-06-16", "n_orders": 1500, "seed": 3, "bootstrap_b": 20}


@pytest.fixture(scope="module")
def cal(synth: Path, tmp_path_factory: pytest.TempPathFactory):
    return calibrate(synth, out=tmp_path_factory.mktemp("cal") / "cal", **EXPORTABLE)


@pytest.fixture(scope="module")
def refused(synth: Path, tmp_path_factory: pytest.TempPathFactory):
    return calibrate(synth, "2026-06-16", "2026-06-15", out=tmp_path_factory.mktemp("ref") / "cal", seed=2,
                     n_orders=1500, bootstrap_b=20)  # fmt: skip


def test_window_forms() -> None:
    assert window("2026-06-15") == {"dates": ["2026-06-15"]}
    assert window("2026-06-15, 2026-06-16") == {"dates": ["2026-06-15", "2026-06-16"]}
    assert window(["2026-06-15"]) == {"dates": ["2026-06-15"]}
    assert window("blocks:0-3/5") == {"blocks": {"index": [0, 1, 2, 3], "n_blocks": 5}}
    assert window("blocks:4/5") == {"blocks": {"index": [4], "n_blocks": 5}}
    assert window(None) == {}
    assert window({"dates": ["2026-06-15"]}) == {"dates": ["2026-06-15"]}
    for bad in ("15/06/2026", "blocks:3-1/5", "blocks:0-5/5", ""):
        with pytest.raises(QtHbtError) as e:
            window(bad)
        assert e.value.code == "QT-CONFIG-001"


def _fit(fam: str, verdict: str, ll: float | None, n: float | None = 1.0) -> FamilyFit:
    return FamilyFit(fam, "b", n, None, 100, verdict, [] if verdict == "exportable" else ["not_beating_default"],
                     {"logloss": {"60": ll}}, {}, Path("x"))  # fmt: skip


def test_select_rule() -> None:
    a, b, c = _fit("power3", "exportable", 0.2), _fit("power2", "exportable", 0.1), _fit("power", "refused", 0.01)
    assert select([a, b, c], 60) == (b, False)  # smallest held-out log loss among the exportable
    assert select([a, c], 60, force=True) == (a, False)  # force changes nothing when one is exportable
    assert select([c], 60) == (None, False)
    assert select([c, _fit("power2", "refused", 0.001)], 60, force=True)[0].family == "power2"
    assert select([c], 60, force=True) == (c, True)
    tie = [_fit("power3", "exportable", 0.1), _fit("power2", "exportable", 0.1)]
    assert select(tie, 60)[0].family == "power3"  # ties go to the earlier family
    assert select([_fit("power3", "exportable", None), b], 60)[0] is b  # no log loss ranks last
    assert select([_fit("power3", "exportable", 0.1, n=None)], 60) == (None, False)


def test_fits_are_queuetruths_calibrations(cal, synth: Path) -> None:
    assert [f.family for f in cal.fits] == ["power3", "power2", "power"]
    for f in cal.fits:
        doc = json.loads(f.path.read_text())
        assert doc["family"] == f.family and doc["estimate"] == f.n and doc["trust"]["verdict"] == f.verdict
        assert doc["spec"]["train"] == {"dates": ["2026-06-15"], "blocks": None}
        assert doc["spec"]["exchange"] == "partial"
    assert [f.verdict for f in cal.fits] == ["exportable", "refused", "refused"]


def test_the_winner_and_its_code(cal) -> None:
    assert cal.best is cal.fits[0] and cal.best.builder == "power_prob_queue_model3" and not cal.forced
    code = cal.code
    assert f".power_prob_queue_model3({format(cal.best.n, '.6g')})" in code
    assert ".partial_fill_exchange()" in code and "NOT VALIDATED" not in code
    assert "asset = (" in code
    compile(code, "hbt_queue_model.py", "exec")
    assert cal.code_path is not None and cal.code_path.read_text() == code
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        direct = qt.export({"target": "hftbacktest", "calibration": {"path": str(cal.best.path)}}).text
    assert code == direct  # the code is queuetruth's own export, unchanged
    doc = json.loads((cal.out / "hbt_calibration.json").read_text())
    assert doc["schema"] == "qthbt.calibration" and doc["best"] == "power3" and doc["code"] == "hbt_queue_model.py"
    text = cal.text()
    assert text.startswith(f"use .power_prob_queue_model3({format(cal.best.n, '.6g')})")
    assert "refused (not_beating_default)" in text


def test_refused_everywhere_gives_no_code(refused, synth: Path, tmp_path: Path) -> None:
    assert refused.best is None and all(not f.exportable for f in refused.fits)
    with pytest.raises(CalibrationRefused) as e:
        _ = refused.code
    assert e.value.code == "QT-CALIB-001" and e.value.exit_code == 1
    assert set(e.value.reasons) == {"power3", "power2", "power"}
    assert not (refused.out / "hbt_queue_model.py").exists()
    assert refused.text().startswith("no fitted queue model passes")
    forced = calibrate(synth, "2026-06-16", "2026-06-15", out=tmp_path / "f", orders=refused.shadows, seed=2,
                       bootstrap_b=20, force=True)  # fmt: skip
    assert forced.forced and forced.best is not None
    assert "# !! NOT VALIDATED" in forced.code and forced.text().startswith("NOT VALIDATED")


def test_given_orders_and_one_family(synth: Path, shadows: Path, tmp_path: Path) -> None:
    r = calibrate(synth, "2026-06-15", "2026-06-16", out=tmp_path, orders=shadows, families=["power2"],
                  exchange="no_partial", bootstrap_b=10)  # fmt: skip
    assert [f.family for f in r.fits] == ["power2"] and r.shadows == shadows
    assert json.loads(r.fits[0].path.read_text())["spec"]["exchange"] == "no_partial"
    assert not (tmp_path / "shadows.parquet").exists()


def test_argument_errors(synth: Path, tmp_path: Path) -> None:
    for kw, code in (
        ({"families": ["power4"]}, "QT-CONFIG-001"),
        ({"exchange": "full"}, "QT-CONFIG-001"),
        ({"orders": tmp_path / "none.parquet"}, "QT-INPUT-001"),
    ):
        with pytest.raises(QtHbtError) as e:
            calibrate(synth, "2026-06-15", out=tmp_path / "o", **kw)
        assert e.value.code == code
    with pytest.raises(QtHbtError) as e:
        calibrate(tmp_path, "2026-06-15", out=tmp_path / "o")
    assert e.value.code == "QT-INPUT-001"
