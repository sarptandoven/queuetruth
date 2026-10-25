"""`pm calibrate` (`queuetruth.pm.calibrate`): official queue positions, the queue truth at own fills (book and
chain), the bounds and the fit, on the fixtures and by hand."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pm._helpers import depth, l2_dataset, resync, trade

from queuetruth.errors import QtError
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.calibrate import (
    MIN_ORDERS,
    _fill_batch,
    calibrate,
    models_for,
    own_ahead,
    own_fills_between,
    resting_at,
)
from queuetruth.pm.models import PmCalibrationSpec
from queuetruth.pm.queue import Model, _batch_starts
from queuetruth.pm.score import score
from queuetruth.pm.workdir import Leg, legs, load

T = 1_792_764_000_000_000_000
MS = 1_000_000


def leg(key: str, entry: int, qty: int, fills=(), end=None, amends=(), seq: int = 1, side: int = BID) -> Leg:
    return Leg(key, key, 1, side, 4900, qty, seq, False, entry, "exact", end, list(amends), list(fills))


def test_resting_own_ahead_and_own_fills_between() -> None:
    a = leg("a", 100, 10, fills=[(150, 4, 4900)], amends=[(170, 8)], end=200)
    assert [resting_at(a, t) for t in (99, 100, 150, 170, 199, 200)] == [0, 10, 6, 4, 4, 0]
    b = leg("b", 120, 5, seq=2)
    c = leg("c", 110, 3, side=ASK)
    all_legs = [a, b, c]
    assert own_ahead(1, all_legs, 160) == 6  # a joined earlier at the same level; c is on the other side
    assert own_ahead(0, all_legs, 160) == 0
    assert own_fills_between(1, all_legs, 100, 151) == 4 and own_fills_between(1, all_legs, 151, 300) == 0


def test_models_for_spans_the_grid_and_refuses_unknown_families() -> None:
    sp = PmCalibrationSpec(
        pm={"path": "x"}, families=["power3", "log", "cancels_ahead"], grid_lo=-1, grid_hi=1, grid_n=3
    )
    ids = [m.id for m in models_for(sp)]
    assert ids[:2] == ["cancels_ahead", "risk_adverse"] and len(ids) == 2 + 2 * 3 + 2
    assert ids[2:8] == [
        "power3[n=0.367879]",
        "power3[n=1]",
        "power3[n=2.71828]",
        "power3[n=0.367879;atomic]",
        "power3[n=1;atomic]",
        "power3[n=2.71828;atomic]",
    ]
    assert ids[8:] == ["log", "log[atomic]"]  # the bounds have no atomic variant
    plain = [m.id for m in models_for(sp.model_copy(update={"atomic": False}))]
    assert plain == ["cancels_ahead", "risk_adverse", "power3[n=0.367879]", "power3[n=1]", "power3[n=2.71828]", "log"]
    with pytest.raises(QtError) as e:
        models_for(PmCalibrationSpec(pm={"path": "x"}, families=["magic"]))
    assert e.value.code == "QT-CONFIG-001"


@pytest.mark.parametrize("qty", [1000, 600])
def test_fill_truth_counts_only_the_fills_of_the_first_filling_batch(tmp_path: Path, qty: int) -> None:
    """Rule 2 regression: 5 contracts ahead, the order fills 3 at +100 ms and 3 more at +600 ms from a second taker.
    The first batch's prints (8) minus the order's fill in that batch (3) is the queue ahead (5). Counting both fills
    gave 2, and with a 6-contract order wrongly read the first batch as completing it."""
    rows = l2_dataset(
        tmp_path,
        [
            resync(T, [(BID, 4900, 500), (ASK, 5100, 1000)]),  # own-free: the 5 contracts ahead of the order
            depth(T + 100 * MS, [(BID, 4900, 500, 0)], [trade(T + 100 * MS, 4900, 800, ASK)]),
            depth(T + 600 * MS, [], [trade(T + 600 * MS, 4900, 300, ASK)]),
        ],
    )
    g = leg("o", T + 10 * MS, qty, fills=[(T + 100 * MS, 300, 4900), (T + 600 * MS, 300, 4900)])
    bi, prints, mine = _fill_batch(g, rows, np.asarray(_batch_starts(rows)), 1_000_000_000)
    assert (bi, prints, mine) == (1, 800.0, 300.0)
    assert mine < resting_at(g, g.fills[0][0] - 1)  # not completed by the first batch: the truth is exact
    assert prints - mine - own_ahead(0, [g], g.fills[0][0] - 1) == 500.0


def test_kalshi_fit_on_official_queue_positions(kalshi_run: Any) -> None:
    c = kalshi_run.calibration
    assert (c.fit_on, c.units, c.venue) == ("queue_samples", "contracts", "kalshi")
    assert (c.n_orders, c.n_samples, c.n_train_orders, c.n_test_orders) == (21, 76, 10, 11)
    assert c.bounds_coverage == 1.0  # every official position lies between cancels_ahead and risk_adverse
    assert c.fill_truth.bound_violations == 0 and c.fill_truth.n_chain == 0
    best = c.models[0]
    assert best.model_id == c.best and best.train_loss == min(m.train_loss for m in c.models)
    assert {m.family for m in c.models} >= {
        "power3",
        "power2",
        "power",
        "log",
        "log2",
        "cancels_ahead",
        "risk_adverse",
        "bounds_midpoint",
    }
    assert all(m.n_test_samples > 0 and m.test_loss is not None and m.test_mae is not None for m in c.models)
    doc = json.loads((kalshi_run.root / "cal" / "pm_calibration.json").read_text())
    assert doc["schema"] == "qt.pm_calibration" and doc["best"] == c.best
    assert c.pm_run_id == kalshi_run.ingest.run_id
    # trust: 21 orders cannot pin down a queue model, and the fit loses to the bounds midpoint out of sample
    assert (c.n_train_orders_with_truth, c.n_test_orders_with_truth) == (8, 10) and 10 < MIN_ORDERS
    assert (c.trust.verdict, c.trust.reasons) == ("refused", ["few_orders", "not_beating_midpoint"])
    assert c.warnings[-1] == "trust: verdict: refused (few_orders, not_beating_midpoint)"
    assert any(w.startswith("trust: few_orders: 8 train and 10 test order(s) have queue truth") for w in c.warnings)
    # the split: orders by (first entry, key); the test half starts at test_from
    gl = legs(load(kalshi_run.pm))
    first = {}
    for g in gl:
        first[g.order_key] = min(first.get(g.order_key, g.entry_ts), g.entry_ts)
    order = sorted(first, key=lambda k: (first[k], k))
    assert c.test_from == (first[order[10]], order[10])
    # every unfilled test order: the shortest-queue bound fills at least as many of them as any model, the longest
    # none (an exact FIFO feed: it is never ahead of the truth)
    by = {m.model_id: m for m in c.models}
    assert by["risk_adverse"].unfilled_filled == 0 and by["bounds_midpoint"].unfilled_filled is None
    assert all(
        m.unfilled_filled <= by["cancels_ahead"].unfilled_filled for m in c.models if m.unfilled_filled is not None
    )


def test_model_fits_name_their_zoo_spelling(kalshi_run: Any) -> None:
    for m in kalshi_run.calibration.models:
        if m.atomic or m.family in ("cancels_ahead", "bounds_midpoint"):
            assert m.zoo_id is None, m.model_id
        else:
            assert m.zoo_id is not None and m.zoo_id.startswith("hbt_") and "exch=partial;mode=corrected" in m.zoo_id
            assert Model.parse(m.zoo_id) == Model.parse(m.model_id)  # the two spellings name one model


def test_polymarket_fit_on_the_chain_queue_truth(poly_run: Any) -> None:
    c = poly_run.calibration
    assert (c.fit_on, c.units, c.n_samples, c.bounds_coverage) == ("fill_truth", "shares", 0, None)
    ft = c.fill_truth
    assert ft.n_orders == ft.n_chain == 15 and ft.bound_violations == 0
    assert 0.0 < ft.fraction_ahead_true < 1.0
    assert all(m.fill_truth_mae is not None and m.n_fill_truth > 0 for m in c.models)
    # filled orders only: the fit leans to short queues, and here it picks the lower queue bound itself
    assert c.best == "cancels_ahead" and "fill_truth_selection" in c.trust.flags
    assert c.trust.reasons == ["few_orders", "best_is_bound", "not_beating_midpoint"]
    assert any(w.startswith("fill truth: the fit uses only orders that filled") for w in c.warnings)


def test_kalshi_book_fill_truth_equals_the_simulated_queue(kalshi_run: Any) -> None:
    """Rule 2 by hand: at-price prints of the first filling batch minus the order's fill in it (minus own size
    ahead) is the true queue ahead whenever that fill did not complete the order."""
    d = load(kalshi_run.pm)
    gl = legs(d)
    rows = d.book(1)
    starts = np.asarray(_batch_starts(rows))
    truth = {f"kalshi:default:{o['order_id']}": o for o in kalshi_run.truth["own"]}
    checked = 0
    for i, g in enumerate(gl):
        if not g.fills:
            continue
        _bi, prints, mine = _fill_batch(g, rows, starts, 1_000_000_000)
        assert mine == truth[g.order_key]["fills"][0]["qty"]  # the fill of that batch only
        if mine >= g.qty:
            continue  # completed by its first filling batch: an upper bound only
        f_true = prints - mine - own_ahead(i, gl, g.fills[0][0] - 1)
        assert f_true == truth[g.order_key]["fills"][0]["ahead_before"]
        checked += 1
    assert checked == kalshi_run.calibration.fill_truth.n_orders == 2


def test_without_queue_positions_kalshi_falls_back_to_the_fill_truth(kalshi_run: Any, tmp_path: Path) -> None:
    root = tmp_path / "pm"
    shutil.copytree(kalshi_run.pm, root)
    (root / "pm" / "queue_samples.parquet").unlink()
    c = calibrate({"pm": {"path": str(root)}, "out": str(tmp_path / "cal"), "bootstrap_b": 0})
    assert c.fit_on == "fill_truth" and c.fill_truth.n_orders == 2 and c.n_samples == 0
    assert any("completed by their first filling batch" in w for w in c.warnings)
    assert all(m.test_loss_ci == (None, None) for m in c.models)  # bootstrap_b = 0
    # both orders with fill truth are test orders: nothing is fitted, and the run says so
    assert c.best is None and (c.n_train_orders_with_truth, c.n_test_orders_with_truth) == (0, 2)
    assert c.trust.reasons[0] == "no_estimate" and "fill_truth_selection" in c.trust.flags
    assert any(w.startswith("calibration: nothing fitted: no training order has queue truth") for w in c.warnings)
    with pytest.raises(QtError) as e:
        score({"pm": {"path": str(root)}, "calibration": {"path": str(tmp_path / "cal")}, "out": str(tmp_path / "s")})
    assert e.value.code == "QT-INPUT-002" and "fitted no model" in e.value.message and "--model" in (e.value.hint or "")


def test_calibration_is_deterministic(kalshi_run: Any, tmp_path: Path) -> None:
    again = calibrate({"pm": {"path": str(kalshi_run.pm)}, "out": str(tmp_path / "again")})
    assert again.run_id == kalshi_run.calibration.run_id
    assert again.model_dump() == kalshi_run.calibration.model_dump()


def test_calibrate_errors(kalshi_run: Any, tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        calibrate({"pm": {"path": str(kalshi_run.pm)}})
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        calibrate({"pm": {"path": str(tmp_path / "nowhere")}, "out": str(tmp_path / "o")})
    assert e.value.code == "QT-INPUT-001"
    with pytest.raises(QtError) as e:
        calibrate({"pm": {"path": str(kalshi_run.pm)}, "out": str(tmp_path / "o"), "train_fraction": 1.0})
    assert e.value.code == "QT-CONFIG-001" and "train_fraction" in e.value.message
    root = tmp_path / "noown"  # an ingest of market data only: no live log
    shutil.copytree(kalshi_run.pm, root)
    shutil.rmtree(root / "live")
    summary = json.loads((root / "pm_ingest.json").read_text())
    summary["live"] = None
    summary["files"] = [f for f in summary["files"] if not f["name"].startswith("live/")]
    (root / "pm_ingest.json").write_text(json.dumps(summary))
    with pytest.raises(QtError) as e:
        calibrate({"pm": {"path": str(root)}, "out": str(tmp_path / "o")})
    assert e.value.code == "QT-INPUT-002" and "no own orders" in e.value.message
