"""`fit_hazard` and `qt.hazard_model_card` (SPEC 5.5.7-5.5.8).

Non-gate tests inject small reference implementations of the `queuetruth.stats` functions the card uses
(`model.reference_stats`); the `integration` tests (gate-1) use the real `queuetruth.stats` functions, including
O-MOD-3.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from model import reference_stats
from model.hazard_synth import NS, make_features
from queuetruth.errors import QtError
from queuetruth.model import HazardModel, fit_hazard
from queuetruth.model.climatology import aj_cif_fill
from queuetruth.model.hazard import pickle_sha256
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.schemas.models import HazardModelCard

FAST = {"max_iter": 60, "min_samples_leaf": 40, "max_leaf_nodes": 15}


@pytest.fixture(scope="module")
def features_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("feat") / "features.parquet"
    write_table(make_features(21, orders_per_day=500), p)
    return p


def _spec(features_path: Path, out: Path | None, **kw: object) -> dict:
    return {
        "features": {"path": str(features_path)},
        "out": None if out is None else str(out),
        "gbdt": FAST,
        "bootstrap_b": 300,
        **kw,
    }


def _zoo_table(features_path: Path) -> pa.Table:
    """Zoo predictions for every landmark-0 shadow; the exact truth is the shadow's own fill by h (its label)."""
    f = pq.read_table(features_path)
    lm0 = f.filter(pa.compute.equal(f.column("landmark_ns"), 0))
    sids = lm0.column("order_ref").to_pylist()
    ev = lm0.column("event_ns").to_pylist()
    cause = lm0.column("cause").cast(pa.utf8()).to_pylist()
    rows: dict[str, list] = {k: [] for k in SCHEMAS["qt.zoo_predictions"].names}
    for model_id, p in (("front_of_queue", 0.9), ("never_fills", 0.0)):
        for sid, e, c in zip(sids, ev, cause, strict=True):
            for h in (NS, 10 * NS):
                rows["model_id"].append(model_id)
                rows["shadow_id"].append(sid)
                rows["qty"].append(1)
                rows["horizon_ns"].append(h)
                rows["p_fill"].append(p)
                rows["e_fill_qty"].append(p)
                for k in ("model_ts_first_fill", "truth_ts_first_fill", "queue_ahead_at_live", "level_qty_at_live"):
                    rows[k].append(None)
                y = c == "fill" and e <= h
                rows["truth_filled"].append(y)
                rows["truth_filled_qty"].append(int(y))
                rows["censored"].append(False)
                rows["visible_level_qty_at_live"].append(0)
                rows["out_of_view_at_live"].append(False)
                rows["instrument_id"].append(1)
                rows["session_date"].append(dt.date(1970, 1, 1))
                rows["ts_live"].append(0)
                rows["trade_vol_60s"].append(0)
    schema = SCHEMAS["qt.zoo_predictions"]
    return pa.Table.from_arrays([pa.array(rows[f.name]).cast(f.type) for f in schema], schema=schema)


def _never_cancel_life(zoo: pa.Table) -> pa.Table:
    """`qt.zoo_shadows` for the zoo table's shadows: never-cancel, no administrative end."""
    sids = sorted(set(zoo.column("shadow_id").to_pylist()))
    n = len(sids)
    ts = pa.timestamp("ns", tz="UTC")
    cols = {
        "shadow_id": pa.array(sids, pa.utf8()),
        "ts_cancel_planned": pa.array([None] * n, pa.int64()).cast(ts),
        "ts_admin_end": pa.array([None] * n, pa.int64()).cast(ts),
        "instrument_id": pa.array([1] * n, pa.uint32()),
        "session_date": pa.array([dt.date(1970, 1, 1)] * n, pa.date32()),
        "ts_live": pa.array([0] * n, pa.int64()).cast(ts),
        "session_close": pa.array([10**13] * n, pa.int64()).cast(ts),
        "exact_end_state": pa.array([None] * n, pa.utf8()),
        "never_cancel": pa.array([True] * n, pa.bool_()),
    }
    schema = SCHEMAS["qt.zoo_shadows"]
    return pa.Table.from_arrays([cols[f.name].cast(f.type) for f in schema], schema=schema)


def test_fit_hazard_end_to_end(monkeypatch: pytest.MonkeyPatch, features_path: Path, tmp_path: Path) -> None:
    reference_stats.install(monkeypatch)
    zoo_path = tmp_path / "zoo.parquet"
    write_table(_zoo_table(features_path), zoo_path)
    out = tmp_path / "fit"
    # open conflict 2 (OC-7): zoo predictions without their planned-life table are refused
    with pytest.raises(QtError) as ei:
        fit_hazard(_spec(features_path, out, zoo_predictions={"path": str(zoo_path)}))
    assert ei.value.code == "QT-INPUT-001"
    write_table(_never_cancel_life(_zoo_table(features_path)), tmp_path / "zoo_shadows.parquet")
    card = fit_hazard(_spec(features_path, out, zoo_predictions={"path": str(zoo_path)}))
    doc = json.loads((out / "model.json").read_text())
    assert HazardModelCard.model_validate(doc) == card
    assert doc["schema"] == "qt.hazard_model_card" and doc["format_version"] == 1
    assert card.model_id.startswith("hazard_gbdt:") and len(card.model_id) == len("hazard_gbdt:") + 16
    assert [f.name for f in card.files] == ["predictions.parquet", "model.pkl"]
    for f in card.files:
        assert f.sha256 == sha256_file(out / f.name)
    pred = pq.read_table(out / "predictions.parquet")
    assert pred.schema.equals(SCHEMAS["qt.hazard_predictions"], check_metadata=True)
    assert card.files[0].rows == pred.num_rows
    assert card.split.scheme == "days" and card.n_orders.test > 0
    assert "out" not in card.spec
    s = card.scores_test
    keys = [str(t) for t in card.grid_ns]
    assert list(s.brier) == keys and list(s.ipcw_logloss) == keys
    assert s.rcll is not None and s.rcll > 0 and s.ibs is not None
    for h in (str(NS), str(10 * NS)):
        b = s.bss_vs_climatology[h]
        assert b.value is not None and b.ci_lo is not None and b.ci_lo < b.value < b.ci_hi
        assert b.value > 0
        assert s.auc[h] is not None and 0.5 < s.auc[h] <= 1
        assert s.calib_slope[h] is not None and s.hl_p[h] is not None and s.spiegelhalter_z[h] is not None
    assert s.c_index_uno is not None and 0.5 < s.c_index_uno <= 1
    zoo = card.zoo_comparison
    assert zoo is not None and set(zoo) == {"front_of_queue", "never_fills", card.model_id}
    assert set(zoo["never_fills"].brier) == {str(NS), str(10 * NS)}
    assert zoo[card.model_id].brier[str(10 * NS)] < zoo["front_of_queue"].brier[str(10 * NS)]
    # F6: the card states the comparison's estimand and population; F2: hl_p_et (8 df) next to hl_p
    assert any(w.startswith("zoo_comparison: Brier against the zoo's exact never-cancel truth") for w in card.warnings)
    assert any("planned life of qt.zoo_shadows" in w for w in card.warnings)  # the sibling zoo_shadows.parquet
    assert s.hl_p_et is not None and all(s.hl_p_et[h] is not None for h in (str(NS), str(10 * NS)))
    # The saved model reproduces the stored predictions.
    m = HazardModel.load(str(out), sha256=pickle_sha256(card))  # the digest fit_hazard recorded
    assert m.card == card
    assert m.predict(pq.read_table(features_path).slice(0, 5)).num_rows == 5 * len(card.grid_ns)


def test_fit_hazard_deterministic_outputs(monkeypatch: pytest.MonkeyPatch, features_path: Path, tmp_path: Path) -> None:
    reference_stats.install(monkeypatch)
    a = fit_hazard(_spec(features_path, tmp_path / "a"))
    b = fit_hazard(_spec(features_path, tmp_path / "b"))
    c = fit_hazard(_spec(features_path, None))
    assert a == b
    for name in ("model.json", "model.pkl", "predictions.parquet"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert c.files == [] and c.scores_test == a.scores_test and c.model_id == a.model_id


def test_fit_hazard_input_errors(tmp_path: Path, features_path: Path) -> None:
    with pytest.raises(QtError) as err:
        fit_hazard({"features": {"path": str(tmp_path / "missing.parquet")}, "out": None})
    assert err.value.code == "QT-INPUT-001"
    bad = tmp_path / "bad.parquet"
    pq.write_table(pa.table({"x": [1]}), bad)
    with pytest.raises(QtError) as err:
        fit_hazard({"features": {"path": str(bad)}, "out": None})
    assert err.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as err:
        fit_hazard({"features": {"upload_id": "u1"}, "out": None})
    assert err.value.code == "QT-CONFIG-001"


def test_climatology_matches_reference_aj(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evaluation of an AJ step function on the grid (0 before the first event, value at the last time <= tau)."""
    reference_stats.install(monkeypatch)
    e = np.array([1, 2, 2, 4, 6, 9]) * NS
    c = np.array([1, 2, 0, 1, 0, 1])
    got = aj_cif_fill(e, c, np.array([NS // 2, NS, 3 * NS, 4 * NS, 10 * NS]))
    # S drops: t=1: 1/6 fill; t=2: cancel 1/5 of S=5/6; censor; t=4: fill 1/3 of S=2/3; t=9: fill 1/1 of S=4/9.
    assert got == pytest.approx([0, 1 / 6, 1 / 6, 1 / 6 + 2 / 9, 1 / 6 + 2 / 9 + 4 / 9])


# ---- gate-1 (real queuetruth.stats) ------------------------------------------------------------------------------


@pytest.mark.integration
def test_o_mod_3_beats_climatology(tmp_path: Path) -> None:
    """O-MOD-3: fill hazard driven by imbalance and queue share; BSS vs climatology lower CI > 0 at 1 s and 10 s."""
    p = tmp_path / "features.parquet"
    write_table(make_features(31, orders_per_day=800), p)
    card = fit_hazard({"features": {"path": str(p)}, "out": str(tmp_path / "fit"), "seed": 3})
    for h in (str(NS), str(10 * NS)):
        b = card.scores_test.bss_vs_climatology[h]
        assert b.ci_lo is not None and b.ci_lo > 0, (h, b)
    s = card.scores_test
    assert all(s.calib_slope[k] is not None for k in (str(NS), str(10 * NS)))
    assert all(s.hl_p[k] is not None for k in (str(NS), str(10 * NS)))


@pytest.mark.integration
def test_stats_aalen_johansen_agrees_with_reference() -> None:
    """The climatology's use of `queuetruth.stats.aalen_johansen` gives the same grid values as the reference AJ."""
    from queuetruth.stats import aalen_johansen

    rng = np.random.default_rng(5)
    e = rng.permutation(np.arange(1, 401)) * (NS // 10)
    c = rng.integers(0, 3, size=400)
    grid = np.array([NS, 5 * NS, 20 * NS, 60 * NS])
    ref = reference_stats.aalen_johansen(e, c)[1]
    real = aalen_johansen(e.astype(float), c, causes=(1, 2))[1]
    for tau in grid:
        i_ref = np.searchsorted(ref.time, tau, side="right") - 1
        i_real = np.searchsorted(np.asarray(real.time), tau, side="right") - 1
        assert np.asarray(real.value)[i_real] == pytest.approx(ref.value[i_ref], abs=1e-12)


def test_score_test_weights_calibration_by_ipcw(
    monkeypatch: pytest.MonkeyPatch, features_path: Path, tmp_path: Path
) -> None:
    """F2 wiring: the card's calibration diagnostics get the IPCW weights of SPEC 5.5.7 (0 before tau)."""
    import queuetruth.model.card as card_mod
    from queuetruth.model.scoring import ipcw_calibration

    reference_stats.install(monkeypatch)
    seen: list[np.ndarray] = []

    def spy(p: np.ndarray, y: np.ndarray, w: np.ndarray) -> dict[str, float]:
        seen.append(np.asarray(w))
        return ipcw_calibration(p, y, w)

    monkeypatch.setattr(card_mod, "ipcw_calibration", spy)
    card = fit_hazard(_spec(features_path, None))
    assert len(seen) == len(card.grid_ns)
    w_last = seen[-1]
    assert (w_last == 0).any() and (w_last > 1).any()  # censored-before rows dropped, survivors up-weighted
