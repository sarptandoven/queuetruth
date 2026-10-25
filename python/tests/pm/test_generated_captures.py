"""The pipeline on longer captures from the fixture generator (exact FIFO books in the documented formats, written to
a temporary directory, never committed): calibration against official queue positions where the true process is
known, the bounds and brackets over hundreds of orders, and (integration) a 130-minute capture.

The numbers asserted here are the ones docs/how-to/prediction-market-making.md quotes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from queuetruth.pm import fixtures
from queuetruth.pm.calibrate import LevelIndex, _fill_batch, calibrate, resting_at
from queuetruth.pm.ingest import ingest
from queuetruth.pm.models import ModelFit
from queuetruth.pm.queue import _batch_starts
from queuetruth.pm.score import score
from queuetruth.pm.workdir import legs, load


def run_generated(root: Path, n_events: int, seed: int = 101) -> dict[str, Any]:
    """Writes a generated Kalshi capture of `n_events` exchange events under `root` and runs ingest, calibrate and
    score with their defaults."""
    for rel, data in fixtures.kalshi(seed=seed, n_events=n_events).items():
        p = root / "cap" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    ing = ingest(
        {
            "venue": "kalshi",
            "inputs": [{"path": str(root / "cap" / "kalshi" / f)} for f in ("ws.jsonl", "rest.jsonl")],
            "out": str(root / "in"),
        }
    )
    cal = calibrate({"pm": {"path": str(root / "in")}, "out": str(root / "cal")})
    sc = score({"pm": {"path": str(root / "in")}, "calibration": {"path": str(root / "cal")}, "out": str(root / "sc")})
    return {"ingest": ing, "calibration": cal, "score": sc, "root": root}


def fit(models: list[ModelFit], model_id: str) -> ModelFit:
    return next(m for m in models if m.model_id == model_id)


@pytest.fixture(scope="module")
def nineteen_minutes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return run_generated(tmp_path_factory.mktemp("pm_gen_8000"), 8000)


def test_calibration_on_a_nineteen_minute_capture(nineteen_minutes: dict[str, Any]) -> None:
    ing, c = nineteen_minutes["ingest"], nineteen_minutes["calibration"]
    assert (ing.live["n_orders"], ing.queue_samples, ing.book.verdict) == (358, 1487, "clean")
    (prog,) = nineteen_minutes["score"].rewards["programs"]
    assert round((prog["scored_to_ns"] - prog["scored_from_ns"]) / 60e9) == 19  # minutes of book
    assert (c.loss, c.n_orders, c.n_samples, c.n_train_orders, c.n_test_orders) == ("rmse", 358, 1487, 179, 179)
    assert c.bounds_coverage == 1.0  # every official position lies between the two queue bounds
    assert c.best == "power[n=0.22313;atomic]" and c.best_beats_bounds_midpoint is True
    best, mid = fit(c.models, c.best), fit(c.models, "bounds_midpoint")
    lo, hi = fit(c.models, "cancels_ahead"), fit(c.models, "risk_adverse")
    assert [round(m.test_loss, 2) for m in (best, mid, lo, hi)] == [8.35, 8.80, 12.48, 14.63]
    assert abs(best.test_bias) < 0.5 < 5.0 < -lo.test_bias and hi.test_bias > 5.0  # the bounds are biased by design
    assert c.fill_truth.bound_violations == 0
    assert (c.trust.verdict, c.n_train_orders_with_truth, c.n_test_orders_with_truth) == ("usable", 166, 169)
    # the 64 test orders that never filled live: the longest-queue bound fills none, the shortest the most
    assert c.n_unfilled_test_orders == 64
    assert (hi.unfilled_filled, best.unfilled_filled, lo.unfilled_filled) == (0, 3, 5)


def test_fill_truth_is_the_exact_queue_on_a_nineteen_minute_capture(nineteen_minutes: dict[str, Any]) -> None:
    """Rule 2 against the simulator's queue: every order whose first filling batch did not complete it gets the exact
    queue ahead (45 of them; counting a later taker's fill in the first batch used to skip 22 as completed)."""
    root = nineteen_minutes["root"]
    truth = {
        f"kalshi:default:{o['order_id']}": o
        for o in json.loads((root / "cap" / "kalshi" / "truth.json").read_text())["own"]
    }
    d = load(root / "in")
    gl = legs(d)
    rows = d.book(1)
    starts = np.asarray(_batch_starts(rows))
    index = LevelIndex(gl)
    got = []
    for i, g in enumerate(gl):
        hit = _fill_batch(g, rows, starts, 1_000_000_000) if g.fills else None
        if hit is None or hit[2] >= resting_at(g, g.fills[0][0] - 1):
            continue
        ahead = hit[1] - hit[2] - index.own_ahead(i, g.fills[0][0] - 1)
        got.append(ahead == truth[g.order_key]["fills"][0]["ahead_before"])
    assert len(got) == nineteen_minutes["calibration"].fill_truth.n_orders == 45 and all(got)


def test_scores_bracket_the_live_fills_over_hundreds_of_orders(nineteen_minutes: dict[str, Any]) -> None:
    """On a consistent FIFO feed no live fill comes before the optimistic bound's or after the pessimistic bound's,
    and the live trading PnL lies inside the per-order trading bound."""
    s = nineteen_minutes["score"]
    assert s.n_orders == 358 and [v.bracket_violations for v in s.sim_vs_live] == [0] * 5
    test = s.sim_vs_live[-1]  # the fitted model on the 179 orders it was not fitted on
    assert (test.orders, test.n_orders, s.calibration_trust.verdict) == ("test", 179, "usable")
    tb = s.band["trading_bound"]
    assert s.live is not None and tb.lower <= s.live.trading <= tb.upper
    assert s.band["total"].lower <= s.band["total"].model <= s.band["total"].upper


def test_the_how_to_quotes_these_runs() -> None:
    page = (Path(__file__).resolve().parents[3] / "docs" / "how-to" / "prediction-market-making.md").read_text()
    text = " ".join(page.split())
    for quote in (
        "A 19-minute capture** (8,000 exchange events, 358 of your orders, 1,487 official positions)",
        "`power[n=0.22313;atomic]` has a test RMSE of 8.35 contracts against 8.80 for the bounds midpoint and 12.48 "
        "and 14.63 for the two bounds",
        "A 130-minute capture** (50,000 events, 2,276 orders",
        "test RMSE 7.67 against 8.25 contracts and mean absolute error 2.76 against 3.39",
        "`power2[n=54.5982;atomic]`, whose queue is 3.38 contracts short on the test orders and whose test MAE (2.87)",
    ):
        assert quote in text, quote


@pytest.mark.integration
def test_a_two_hour_capture_and_the_atomic_variants(tmp_path: Path) -> None:
    """50,000 exchange events (130 minutes): the run behind the how-to's atomic-variant and loss numbers."""
    r = run_generated(tmp_path, 50_000)
    ing, c = r["ingest"], r["calibration"]
    (prog,) = r["score"].rewards["programs"]
    assert round((prog["scored_to_ns"] - prog["scored_from_ns"]) / 60e9) == 130
    assert (ing.live["n_orders"], ing.queue_samples, ing.book.records) == (2276, 10412, 62450)
    assert c.bounds_coverage == 1.0 and c.best_beats_bounds_midpoint is True
    plain = min((m for m in c.models if not m.atomic and m.family != "bounds_midpoint"), key=lambda m: m.test_loss)
    best = fit(c.models, c.best)
    assert best.atomic and best.test_loss < plain.test_loss and best.test_mae < plain.test_mae
    assert [round(m, 2) for m in (best.test_loss, plain.test_loss, best.test_mae, plain.test_mae)] == [
        7.67,
        8.25,
        2.76,
        3.39,
    ]
    # ranked by MAE, the fit commits ambiguous cancels to one side: a short queue (early fills) that is worse on the
    # test orders even by MAE
    by_mae = calibrate({"pm": {"path": str(tmp_path / "in")}, "out": str(tmp_path / "cal_mae"), "loss": "mae"})
    m = fit(by_mae.models, by_mae.best)
    assert (by_mae.best, round(m.test_bias, 2), round(m.test_mae, 2)) == ("power2[n=54.5982;atomic]", -3.38, 2.87)
    assert (c.best, round(best.test_bias, 2)) == ("power[n=0.0183156;atomic]", -0.03)
    # ln n = -4, the grid's lower end: refused until a wider grid shows where the fit settles
    assert (c.trust.verdict, c.trust.reasons) == ("refused", ["at_grid_edge"])
