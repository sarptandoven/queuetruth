"""The one line of plan section 4.3, `engine.add_venue(..., **qtn.venue_options(calibration))`, fed by a fit on your
data (`qt.fit("nautilus", ...)`) or by the atlas when you have none (`qt.atlas.calibration("nautilus", ...)`).

The document tests need neither queuetruth nor nautilus_trader. The end-to-end tests need queuetruth with its engine
(and the synthetic fixture tests/fixtures/synth_small of a checkout) and run the venue in a real NautilusTrader
backtest; each part skips with the reason when it is missing."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import warnings
from pathlib import Path
from typing import Any

import pytest

import queuetruth_nautilus as qtn
from queuetruth_nautilus import CalibrationRefused, CalibrationWarning, QtNautilusError

REPO = Path(__file__).resolve().parents[3]
SYNTH = REPO / "tests" / "fixtures" / "synth_small"
#: The synthetic atlas data API of a checkout (invented numbers; tests/fixtures/atlas_synth/make_fixture.py).
ATLAS = REPO / "tests" / "fixtures" / "atlas_synth"
HAVE_NT = importlib.util.find_spec("nautilus_trader") is not None
HAVE_QT = importlib.util.find_spec("queuetruth") is not None and importlib.util.find_spec("queuetruth._core")
needs_nt = pytest.mark.skipif(not HAVE_NT, reason="nautilus_trader is not installed")
needs_qt = pytest.mark.skipif(not HAVE_QT, reason="queuetruth with its engine is not installed")

#: A `qt.fit` document of `qt.atlas.calibration("nautilus", tier="T1_top100")` (invented numbers).
ATLAS_DOC: dict[str, Any] = {
    "schema": "qt.fit",
    "answer": "Use p = 0.5 ...",
    "backtester": "nautilus",
    "family": "nt_queue_l2_prob_touch",
    "source": "atlas",
    "command": "atlas",
    "run_id": None,
    "param": {"name": "p", "setting": "", "value": 0.5, "ci": None, "ci_kind": None, "default": 1.0, "n_events": 0},
    "split": None,
    "held_out": [],
    "verdict": {"passed": True, "reasons": [], "remedies": []},
    "snippet": None,
    "settings": {"prob_fill_on_limit": 0.5, "queue_position": True, "book_type": "L2_MBP", "trade_execution": True},
    "atlas": {
        "atlas_root": "cd" * 32,
        "venue": "iex",
        "venue_name": "Synthetic venue",
        "window": "3 days from 2026-11-06 to 2026-11-10",
        "tier": "T1_top100",
        "horizon": "1min",
        "mode": "faithful",
        "verdict": "too_pessimistic",
        "verdict_text": "still clearly off: fills too few orders",
    },
    "files": {"fit": None, "calibration": None, "shadows": None, "snippet": None},
}


class Result:
    """Stands in for `queuetruth.fit.FitResult` (the plugin reads `doc`, `calibration` and `out` only)."""

    def __init__(self, doc: dict[str, Any], calibration: Any = None, out: Any = None) -> None:
        self.doc, self.calibration, self.out = doc, calibration, out


# ---- documents (no queuetruth, no nautilus_trader) ----------------------------------------------------------------


def test_atlas_setting_becomes_a_nautilus_calibration() -> None:
    cal = qtn.as_calibration(Result(ATLAS_DOC))
    assert (cal.prob_fill_on_limit, cal.queue_position, cal.source_format) == (0.5, True, "atlas")
    assert cal.trust.ok and cal.trust.source == "atlas" and cal.latency is None
    assert cal.run_id == "atlas-cdcdcdcdcdcd" and cal.atlas is not None and cal.atlas["tier"] == "T1_top100"
    assert cal.summary()["atlas"]["verdict"] == "too_pessimistic"
    assert qtn.fill_model_kwargs(Result(ATLAS_DOC)) == {"prob_fill_on_limit": 0.5, "prob_slippage": 0.0,
                                                         "random_seed": None}  # fmt: skip
    caveat = qtn.venue.caveats(cal, 2, fill=True, latency=False, random_seed=1)
    assert caveat[0].startswith("from the Fill-Realism Atlas (Synthetic venue, 3 days from 2026-11-06 to 2026-11-10, ")
    assert "not your data: still clearly off: fills too few orders" in caveat[0]


def test_qt_fit_documents_load_from_dict_and_file(tmp_path: Path) -> None:
    assert qtn.as_calibration(ATLAS_DOC).prob_fill_on_limit == 0.5
    p = tmp_path / "fit.json"
    p.write_text(json.dumps(ATLAS_DOC))
    assert qtn.load_calibration(tmp_path).prob_fill_on_limit == 0.5
    assert qtn.as_calibration(str(p)).queue_position is True


def test_a_setting_of_another_library_or_none_is_refused(tmp_path: Path) -> None:
    other = copy.deepcopy(ATLAS_DOC) | {"backtester": "hftbacktest"}
    with pytest.raises(QtNautilusError, match="hftbacktest setting, not a NautilusTrader one") as e:
        qtn.as_calibration(other)
    assert e.value.code == "QT-CONFIG-001"
    none = copy.deepcopy(ATLAS_DOC)
    none["settings"] = {}
    none["verdict"] = {"passed": False, "reasons": [{"code": "no_library_setting", "text": "none"}], "remedies": []}
    cal = qtn.as_calibration(none)
    assert not cal.trust.ok and cal.trust.reasons == ("no_library_setting",) and cal.prob_fill_on_limit is None
    with pytest.raises(QtNautilusError, match="expected a calibration"):
        qtn.as_calibration(3)


def test_a_fit_on_your_data_reads_its_calibration_result(tmp_path: Path, touch_doc: dict[str, Any]) -> None:
    fit = {"schema": "qt.fit", "backtester": "nautilus", "source": "order_level", "family": "nt_prob_touch",
           "files": {"calibration": None}}  # fmt: skip
    direct = qtn.from_document(touch_doc)
    (tmp_path / "calibration").mkdir()
    (tmp_path / "calibration" / "calibration.json").write_text(json.dumps(touch_doc))
    (tmp_path / "fit.json").write_text(json.dumps(fit))
    loaded = qtn.load_calibration(tmp_path)
    assert loaded.prob_fill_on_limit == direct.prob_fill_on_limit and loaded.run_id == direct.run_id
    with pytest.raises(QtNautilusError, match="names its calibration"):
        qtn.from_fit_document(fit, tmp_path / "elsewhere" / "fit.json")


# ---- the real engine ----------------------------------------------------------------------------------------------


@needs_nt
def test_atlas_setting_in_a_real_engine_warns_where_it_came_from() -> None:
    from ntkit import run_episodes

    doc = copy.deepcopy(ATLAS_DOC)
    doc["settings"] = {"prob_fill_on_limit": 1.0, "queue_position": False, "book_type": "L2_MBP",
                       "trade_execution": True}  # fmt: skip
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        opts = qtn.venue_options(Result(doc))
    assert any(isinstance(w.message, CalibrationWarning) and "Fill-Realism Atlas" in str(w.message) for w in caught)
    assert opts["trade_execution"] is True and opts["queue_position"] is False
    assert run_episodes(opts, 50, [(0, 100)]) == set(range(50))


@needs_nt
def test_refused_atlas_setting_needs_force() -> None:
    doc = copy.deepcopy(ATLAS_DOC)
    doc["verdict"] = {"passed": False, "reasons": [{"code": "no_library_setting", "text": "none"}], "remedies": []}
    with pytest.raises(CalibrationRefused):
        qtn.venue_options(Result(doc))


@needs_nt
@needs_qt
@pytest.mark.skipif(not ATLAS.is_dir(), reason="needs the synthetic atlas of a checkout (tests/fixtures/atlas_synth)")
def test_one_line_from_the_atlas() -> None:
    import queuetruth as qt
    from ntkit import run_episodes

    site = ATLAS
    cal = qt.atlas.calibration("nautilus", tier="T1", source=site)
    assert cal.source == "atlas" and cal.passed and cal.calibration is None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CalibrationWarning)
        opts = qtn.venue_options(cal, random_seed=3)
    assert opts["queue_position"] is True
    by_symbol = qt.atlas.calibration("nautilus", symbol="zzza", source=site)
    assert by_symbol.doc["atlas"]["tier"] == "T1_top100" and by_symbol.settings == cal.settings
    # queue tracking: 50 of the 100 shares ahead traded leaves the order queued; a second print reaches it and fills
    # it with probability p = 0.5
    assert run_episodes(opts, 50, [(0, 50)]) == set()
    filled = run_episodes(qtn.venue_options(cal, random_seed=3), 400, [(0, 50), (0, 60)])
    assert 0.3 < len(filled) / 400 < 0.7


@needs_nt
@needs_qt
@pytest.mark.skipif(not SYNTH.is_dir(), reason="needs the synthetic fixture of a checkout (tests/fixtures/synth_small)")
def test_one_line_from_a_fit_on_your_data(tmp_path: Path) -> None:
    import queuetruth as qt
    from ntkit import run_episodes

    res = qt.fit("nautilus", data=SYNTH, out=tmp_path / "fit")
    assert res.passed and res.source == "order_level"
    p = res.doc["param"]["value"]
    assert res.settings == {"prob_fill_on_limit": p, "queue_position": False, "book_type": "L2_MBP",
                            "trade_execution": True}  # fmt: skip
    opts = qtn.venue_options(res, random_seed=11)
    filled = run_episodes(opts, 400, [(0, 100)])
    assert abs(len(filled) / 400 - p) < 4 * math.sqrt(p * (1 - p) / 400) + 1e-9
    assert qtn.load_calibration(tmp_path / "fit").prob_fill_on_limit == qtn.as_calibration(res).prob_fill_on_limit
