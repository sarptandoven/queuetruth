"""Every number and terminal line that docs/how-to/prediction-market-making.md shows, from the same default runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pm._helpers import REPO

from queuetruth.pm.cli import render

PAGE = (REPO / "docs" / "how-to" / "prediction-market-making.md").read_text(encoding="utf-8")


def shown(lines: list[str]) -> None:
    """Each line appears verbatim in one of the page's text blocks."""
    for line in lines:
        assert line in PAGE, line


def test_fixtures_command_line(tmp_path: Path) -> None:
    from queuetruth.pm.cli import _dispatch

    doc = _dispatch("fixtures", {"out": str(tmp_path / "pmfix")})
    doc["out"] = "pmfix"  # as the page runs it
    shown(render("fixtures", doc, False).splitlines())


def test_kalshi_walkthrough(kalshi_run: Any) -> None:
    ing = render("ingest", kalshi_run.ingest.model_dump(mode="json"), False).splitlines()
    assert ing == [
        "pm ingest (kalshi): 1 market(s), book 719 rows, verdict clean",
        "own orders: 21 orders, 16 fills",
        "queue entry points: 26 exact, 16 aligned, 0 unaligned, 0 restated",
        "official queue positions: 76",
    ]
    shown(ing)
    cal = render("calibrate", kalshi_run.calibration.model_dump(mode="json"), False).splitlines()
    shown([*cal[:4], *(x for x in cal if x.split()[0] in ("risk_adverse", "bounds_midpoint", "cancels_ahead"))])
    shown([next(x for x in cal if x.startswith("best beats")), next(x for x in cal if x.startswith("verdict:"))])
    c = kalshi_run.calibration
    assert (c.best, c.best_beats_bounds_midpoint, c.n_train_orders, c.n_test_orders, c.loss) == (
        "power[n=7.38906;atomic]",
        False,
        10,
        11,
        "rmse",
    )
    assert (c.n_train_orders_with_truth, c.n_test_orders_with_truth) == (8, 10)  # "8 train and 10 test orders"
    assert "at_grid_edge" not in c.trust.reasons
    assert any(m.zoo_id == "hbt_power3[exch=partial;mode=corrected;n=2.117]" for m in c.models)
    sc = render("score", kalshi_run.score.model_dump(mode="json"), False).splitlines()
    shown(sc[:5])
    shown([next(x for x in sc if "sim vs live front_of_queue" in x)])
    s = kalshi_run.score
    assert round(s.live.trading, 2) == 13.80
    tb = s.band["trading_bound"]
    assert (round(tb.lower, 2), round(tb.upper, 2)) == (3.80, 25.30)
    foq = s.sim_vs_live[2]
    assert (foq.model_id, foq.sim_only, foq.live_only, round(foq.fill_rate_bias * 100, 1)) == (
        "front_of_queue",
        4,
        0,
        19.0,
    )


def test_polymarket_walkthrough(poly_run: Any) -> None:
    ing = render("ingest", poly_run.ingest.model_dump(mode="json"), False).splitlines()
    assert ing == [
        "pm ingest (polymarket): 1 market(s), book 498 rows, verdict clean",
        "own orders: 17 orders, 23 fills",
        "queue entry points: 0 exact, 43 aligned, 0 unaligned, 0 restated",
        "chain: 122 maker fills in 65 matches, 77 filled orders",
    ]
    shown(ing)
    assert poly_run.calibration.best == "cancels_ahead" and poly_run.calibration.fill_truth.n_chain == 15
    cal = render("calibrate", poly_run.calibration.model_dump(mode="json"), False).splitlines()
    shown([cal[0], next(x for x in cal if x.startswith("verdict:"))])
    assert "fill_truth_selection" in poly_run.calibration.trust.flags
    sc = render("score", poly_run.score.model_dump(mode="json"), False).splitlines()
    shown([*sc[:2], next(x for x in sc if x.startswith("live:")), next(x for x in sc if "outside the scenario" in x)])
    s = poly_run.score
    assert (s.scenarios["model"].filled_qty, s.live.filled_qty) == (400.0, 360.75)
    assert (s.scenarios["model"].position, s.live.position) == (50.0, 89.25)
    assert s.sim_vs_live[0].sim_only == 1 and s.mark["will-the-demo-event-happen"]["value"] == 0.0
    import pyarrow.parquet as pq

    rows = pq.read_table(poly_run.root / "sc" / "pm_orders.parquet").to_pylist()
    live = {r["order_key"]: r["filled_qty"] for r in rows if r["scenario"] == "live"}
    sim_only = [r for r in rows if r["scenario"] == "model" and r["filled_qty"] > 0 and live[r["order_key"]] == 0]
    assert [chr(r["side"]) for r in sim_only] == ["A"]  # "an ask that never filled live"
    tb = s.band["trading_bound"]
    assert (round(tb.lower, 2), round(tb.upper, 2), round(s.live.trading, 2)) == (-54.25, 14.50, -37.09)
    assert not s.band["total"].lower <= s.live.total <= s.band["total"].upper  # outside the scenario band


def test_fixtures_are_about_a_minute_of_trading(kalshi_run: Any, poly_run: Any) -> None:
    for run in (kalshi_run, poly_run):
        (prog,) = run.score.rewards["programs"]
        assert 50 <= (prog["scored_to_ns"] - prog["scored_from_ns"]) / 1e9 <= 70
    assert "about a minute each" in PAGE


@pytest.mark.parametrize("needle", ["--flat-taker", "--poly-category", "--actual-rewards", "--member non-direct"])
def test_documented_flags_exist(needle: str) -> None:
    from queuetruth.pm.cli import parser

    flag = needle.split()[0]
    sub = parser()._subparsers._group_actions[0].choices["score"]  # type: ignore[union-attr]
    assert any(flag in a.option_strings for a in sub._actions) and needle in PAGE
