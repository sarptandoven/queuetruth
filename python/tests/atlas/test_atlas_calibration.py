"""`qt.atlas.calibration` and `qt.atlas.touch_tables`: the atlas feeding the one line of each framework (plan section
4.3), on the synthetic atlas data API of tests/fixtures/atlas_synth (invented numbers, no market data)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import queuetruth as qt
from queuetruth.errors import QtError
from queuetruth.fit import FitResult

REPO = Path(__file__).resolve().parents[3]
ATLAS = REPO / "tests" / "fixtures" / "atlas_synth"
DATA = REPO / "tests" / "fixtures" / "synth_small"


def test_nautilus_setting_is_a_fit_result_with_its_source() -> None:
    cal = qt.atlas.calibration("nautilus", source=ATLAS)
    assert isinstance(cal, FitResult) and cal.source == "atlas" and cal.passed and cal.calibration is None
    d = cal.to_dict()
    assert d["schema"] == "qt.fit" and d["family"] == "nt_prob_touch" and d["param"]["value"] == 1.0
    assert d["settings"] == {"prob_fill_on_limit": 1.0, "queue_position": False, "book_type": "L2_MBP",
                             "trade_execution": True}  # fmt: skip
    a = d["atlas"]
    assert (a["tier"], a["horizon"], a["days"], a["atlas_root"]) == ("ALL", "1min", 3, "cd" * 32)
    assert a["verdict"] == "no_clear_difference" and a["fill_rate_bias_pp"] == {"value": 0.1, "lo": -0.2, "hi": 0.4}
    assert cal.summary.startswith("Use p = 1 for NautilusTrader prob_fill_on_limit: the atlas's closest setting for ")
    assert "within 1min its fill rate is off by +0.1 pp (95% interval -0.2 pp to +0.4 pp)" in cal.summary
    assert cal.summary.endswith("From the atlas, not your data: fit on your own data to confirm.")
    text = cal.text()
    assert text.splitlines()[0] == cal.summary and "settings: prob_fill_on_limit=1.0" in text
    assert "source: Fill-Realism Atlas, Synthetic venue, 3 days from 2026-11-06 to 2026-11-10" in text


def test_tier_by_short_name_and_by_symbol() -> None:
    t1 = qt.atlas.calibration("nautilus", tier="T1", source=ATLAS)
    assert t1.doc["atlas"]["tier"] == "T1_top100" and t1.settings["queue_position"] is True
    assert t1.doc["family"] == "nt_queue_l2_prob_touch" and t1.doc["param"]["value"] == 0.5
    assert "still clearly too pessimistic" in t1.summary
    sym = qt.atlas.calibration("nautilus", symbol="zzza", source=ATLAS)
    assert sym.settings == t1.settings and sym.doc["atlas"]["symbol"] == "ZZZA"
    assert any("a single symbol is too noisy" in w for w in sym.doc["warnings"])
    clash = qt.atlas.calibration("nautilus", tier="ALL", symbol="ZZZA", source=ATLAS)
    assert any("ZZZA is in tier T1_top100, not ALL" in w for w in clash.doc["warnings"])


def test_hftbacktest_settings_and_a_model_no_release_reproduces() -> None:
    assert qt.atlas.calibration("hftbacktest", source=ATLAS).settings == {
        "queue_model": "power_prob_queue_model3", "queue_model_args": [3.0], "exchange": "no_partial_fill_exchange"
    }  # fmt: skip
    log2 = qt.atlas.calibration("hftbacktest", tier="T1", source=ATLAS)
    assert log2.settings["queue_model"] == "log_prob_queue_model2" and log2.doc["param"]["value"] is None
    assert log2.summary.startswith("Use hftbacktest Log2 queue model: ")
    none = qt.atlas.calibration("hftbacktest", tier="T5_rest", source=ATLAS)
    assert not none.passed and none.settings == {} and none.snippet is None
    assert none.doc["verdict"]["reasons"][0]["code"] == "no_library_setting"
    assert "no released library setting reproduces it" in none.summary
    assert "verdict: DO NOT USE: no_library_setting" in none.text()


def test_errors_and_consent(tmp_path: Path) -> None:
    with pytest.raises(QtError, match="no one-line setting for 'backtrader'") as e:
        qt.atlas.calibration("backtrader", source=ATLAS)
    assert e.value.code == "QT-USAGE-001"
    with pytest.raises(QtError, match="no Databento setting for tier 'ALL'"):
        qt.atlas.calibration("databento", source=ATLAS)
    with pytest.raises(QtError, match="no NautilusTrader setting for tier 'T5_rest'"):
        qt.atlas.calibration("nautilus", tier="T5_rest", source=ATLAS)
    with pytest.raises(QtError, match="QQQQ is not in the atlas"):
        qt.atlas.calibration("nautilus", symbol="QQQQ", source=ATLAS)
    asked: list[str] = []

    def no(url: str) -> bool:
        asked.append(url)
        return False

    def never(url: str) -> bytes:
        raise AssertionError(f"downloaded {url}")

    with pytest.raises(QtError, match="nothing was downloaded"):
        qt.atlas.calibration("nautilus", source="https://atlas.invalid/qt-test-calibration/v1/", confirm=no,
                             fetch=never)  # fmt: skip
    assert asked == ["https://atlas.invalid/qt-test-calibration/v1/manifest.json"]


def test_save_and_reload(tmp_path: Path) -> None:
    cal = qt.atlas.calibration("hftbacktest", source=ATLAS)
    p = cal.save(tmp_path / "fit.json")
    assert json.loads(p.read_text()) == cal.doc


@pytest.mark.requires_core
def test_a_fit_on_your_data_carries_the_same_settings(tmp_path: Path) -> None:
    res = qt.fit("nautilus", data=DATA, out=tmp_path / "fit")
    p = res.doc["param"]["value"]
    assert res.source == "order_level" and res.settings == {
        "prob_fill_on_limit": p, "queue_position": False, "book_type": "L2_MBP", "trade_execution": True
    }  # fmt: skip
    assert json.loads((tmp_path / "fit" / "fit.json").read_text())["settings"] == res.settings


# ---- touch tables -------------------------------------------------------------------------------------------------


def test_touch_tables_from_the_atlas() -> None:
    t = qt.atlas.touch_tables(ATLAS)
    assert t.row(tier="T1_top100", side="buy", bar="1m").p_real_given_rule.value == 0.35  # type: ignore[union-attr]


def test_an_atlas_without_touch_tables(tmp_path: Path) -> None:
    from atlas.test_atlas_lookup import _write_api

    site = _write_api(tmp_path / "site")
    with pytest.raises(QtError, match="has no touch tables yet") as e:
        qt.atlas.touch_tables(site)
    assert e.value.code == "QT-INPUT-001"


def test_backtrader_and_lean_lines_read_the_atlas_without_a_table(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from backtesters import lean_standin

    f = qt.backtrader.QueueFiller(tier="T1", atlas=ATLAS, bar="1m")
    assert f.touch.tier == "T1_top100" and f.touch.probability("1m", "buy") == 0.35
    monkeypatch.setitem(sys.modules, "AlgorithmImports", lean_standin)
    monkeypatch.setattr("queuetruth.lean._CLASS", [])
    m = qt.lean.QueueFillModel(tier="T1", atlas=ATLAS, bar="5m")
    assert m._qt_touch.probability("5m", "sell") == 0.52
    with pytest.raises(QtError, match="nothing was downloaded"):
        qt.backtrader.QueueFiller(tier="T1", atlas="https://atlas.invalid/qt-test-touch/v1/")
