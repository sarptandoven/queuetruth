"""`asset = qthbt.configure(asset, calibration)` (plan section 4.3), fed by a fit on your data (`qt.fit`) or by the
atlas when you have none (`qt.atlas.calibration`), on a stand-in asset that records the builder calls and, with
`QT_HBT_PY`, on hftbacktest 2.4.4's own `BacktestAsset` inside a full replay. Synthetic data and an invented atlas."""

from __future__ import annotations

import copy
import json
import subprocess
import warnings
from pathlib import Path
from typing import Any

import pytest

import queuetruth_hftbacktest as qthbt
from queuetruth_hftbacktest import CalibrationRefused, CalibrationWarning, QtHbtError
from queuetruth_hftbacktest.calibrate import FamilyFit, HbtCalibration

SRC = Path(__file__).resolve().parents[1] / "src"
#: The synthetic atlas data API of a checkout (invented numbers; tests/fixtures/atlas_synth/make_fixture.py).
ATLAS = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "atlas_synth"


class Asset:
    """Records the builder calls of an hftbacktest `BacktestAsset` (each returns the asset, as hftbacktest's do)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        if not name.endswith(("_queue_model", "_queue_model2", "_queue_model3", "_exchange")):
            raise AttributeError(name)

        def call(*args: Any) -> Asset:
            self.calls.append((name, args))
            return self

        return call


@pytest.fixture(scope="module")
def site() -> Path:
    if not ATLAS.is_dir():
        pytest.skip("needs the synthetic atlas of a checkout (tests/fixtures/atlas_synth)")
    return ATLAS


def _configure(asset: Any, cal: Any, **kw: Any) -> tuple[Any, list[str]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = qthbt.configure(asset, cal, **kw)
    return out, [str(w.message) for w in caught if isinstance(w.message, CalibrationWarning)]


def test_atlas_setting_in_one_line(site: Path) -> None:
    import queuetruth as qt

    cal = qt.atlas.calibration("hftbacktest", source=site)
    asset, notes = _configure(Asset(), cal)
    assert asset.calls == [("power_prob_queue_model3", (3.0,)), ("no_partial_fill_exchange", ())]
    assert len(notes) == 1 and notes[0].startswith(
        "from the Fill-Realism Atlas (Synthetic venue, 3 days from 2026-11-06"
    )
    assert "tier ALL, within 1min), not your data: no clear difference" in notes[0]
    t1, _ = _configure(Asset(), qt.atlas.calibration("hftbacktest", tier="T1", source=site))
    assert t1.calls == [("log_prob_queue_model2", ()), ("no_partial_fill_exchange", ())]
    by_symbol, _ = _configure(Asset(), qt.atlas.calibration("hftbacktest", symbol="ZZZA", source=site))
    assert by_symbol.calls == t1.calls


def test_atlas_model_without_a_library_setting_is_refused_even_with_force(site: Path) -> None:
    import queuetruth as qt

    cal = qt.atlas.calibration("hftbacktest", tier="T5_rest", source=site)
    assert not cal.passed and cal.settings == {}
    for force in (False, True):
        with pytest.raises(CalibrationRefused) as e:
            qthbt.configure(Asset(), cal, force=force)
        assert e.value.code == "QT-CALIB-001" and e.value.reasons == {"power3": ["no_library_setting"]}


def test_saved_document_and_path(site: Path, tmp_path: Path) -> None:
    import queuetruth as qt

    p = qt.atlas.calibration("hftbacktest", source=site).save(tmp_path / "fit.json")
    a, _ = _configure(Asset(), p)
    b, _ = _configure(Asset(), str(tmp_path))
    c, _ = _configure(Asset(), json.loads(p.read_text()))
    assert a.calls == b.calls == c.calls == [("power_prob_queue_model3", (3.0,)), ("no_partial_fill_exchange", ())]
    assert qthbt.settings(p)["queue_model_args"] == [3.0]


def test_refused_fit_needs_force_and_old_documents_still_read() -> None:
    doc = {
        "schema": "qt.fit", "backtester": "hftbacktest", "family": "power3", "source": "order_level",
        "answer": "Do not use n = 2.6 ...", "param": {"name": "n", "value": 2.6},
        "verdict": {"passed": False, "reasons": [{"code": "not_beating_default", "text": "..."}], "remedies": []},
    }  # fmt: skip
    with pytest.raises(CalibrationRefused) as e:
        qthbt.configure(Asset(), doc)
    assert e.value.reasons == {"power3": ["not_beating_default"]}
    asset, notes = _configure(Asset(), doc, force=True)  # no `settings`: read from the family and the value
    assert asset.calls[0] == ("power_prob_queue_model3", (2.6,))
    assert notes == ["force: using a NOT VALIDATED setting (not_beating_default)"]
    passed = copy.deepcopy(doc) | {"verdict": {"passed": True, "reasons": [], "remedies": []}}
    assert _configure(Asset(), passed)[1] == []


def test_plugin_calibration_and_errors(tmp_path: Path) -> None:
    fit = FamilyFit("power2", "power_prob_queue_model2", 0.8, None, 100, "exportable", [], {}, {}, tmp_path)
    cal = HbtCalibration([fit], fit, False, 60_000_000_000, tmp_path, tmp_path)
    assert _configure(Asset(), cal)[0].calls[0] == ("power_prob_queue_model2", (0.8,))
    with pytest.raises(CalibrationRefused):
        qthbt.configure(Asset(), HbtCalibration([fit], None, False, 60_000_000_000, tmp_path, tmp_path))
    with pytest.raises(QtHbtError, match="expected an hftbacktest BacktestAsset") as e:
        qthbt.configure(object(), cal)
    assert e.value.code == "QT-CONFIG-001"
    nt = {"schema": "qt.fit", "backtester": "nautilus", "verdict": {"passed": True}}
    with pytest.raises(QtHbtError, match="nautilus setting, not an hftbacktest one"):
        qthbt.configure(Asset(), nt)
    with pytest.raises(QtHbtError, match=r"expected a qt\.fit"):
        qthbt.configure(Asset(), 3)
    with pytest.raises(QtHbtError) as e:
        qthbt.configure(Asset(), tmp_path / "missing.json")
    assert e.value.code == "QT-INPUT-001"


def test_fit_on_your_data_in_one_line(synth: Path, tmp_path: Path) -> None:
    """On the synthetic dataset the power3 fit does not beat n = 2 (tests of `qt.fit`): `configure` refuses it, and
    applies it only with force."""
    import queuetruth as qt

    res = qt.fit("hftbacktest", data=synth, out=tmp_path / "fit")
    n = res.doc["param"]["value"]
    assert res.settings == {"queue_model": "power_prob_queue_model3", "queue_model_args": [n],
                            "exchange": "no_partial_fill_exchange"}  # fmt: skip
    if res.passed:
        assert _configure(Asset(), res)[0].calls[0] == ("power_prob_queue_model3", (n,))
    else:
        with pytest.raises(CalibrationRefused):
            qthbt.configure(Asset(), res)
        assert _configure(Asset(), res, force=True)[0].calls[0] == ("power_prob_queue_model3", (n,))
    assert qthbt.settings(tmp_path / "fit", force=True)["queue_model_args"] == [n]


# ---- hftbacktest 2.4.4 itself -------------------------------------------------------------------------------------


@pytest.mark.requires_hftbacktest
def test_configure_runs_in_the_hftbacktest_venv(prepared: Any, hbt_py: Path, site: Path, tmp_path: Path) -> None:
    """The hftbacktest venv has neither queuetruth nor pyarrow: `configure` reads the saved document there, sets
    hftbacktest's own `BacktestAsset`, and the replay and the score run on it."""
    import queuetruth as qt

    from queuetruth_hftbacktest import run, score

    fit = qt.atlas.calibration("hftbacktest", source=site).save(tmp_path / "atlas-fit.json")
    probe = subprocess.run(
        [str(hbt_py), "-c", "import queuetruth"], capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert probe.returncode != 0  # queuetruth is not importable there: the line runs on the standard library alone
    code = (
        "import sys, warnings\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "warnings.simplefilter('ignore')\n"
        "import queuetruth_hftbacktest as qthbt\n"
        "from hftbacktest import BacktestAsset\n"
        f"asset = qthbt.configure(BacktestAsset(), {str(fit)!r})\n"
    )
    out = run(prepared, python=hbt_py, code=code, out=tmp_path / "hbt.jsonl")
    s = score(prepared, out, out=tmp_path / "s")
    assert s.n_scored == prepared.n_orders
