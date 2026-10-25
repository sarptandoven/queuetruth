"""`pm score` (`queuetruth.pm.score`): the reward-aware PnL band on the fixtures, its identities and bounds, the
sim-versus-live comparison, hypothetical quotes, marks, fees and the reward integral."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from pm._helpers import FIX, depth, l2_dataset, resync

from queuetruth.errors import QtError
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.score import OwnStep, integrate, score

T = 1_792_764_000_000_000_000
S = 1_000_000_000


def check_identities(doc: Any) -> None:
    for name, p in [*doc.scenarios.items(), ("live", doc.live)]:
        if p is None:
            continue
        assert p.trading == pytest.approx(p.spread_capture - p.adverse_selection), name
        assert p.total == pytest.approx(p.trading - p.fees + p.rewards + p.rebates), name
        assert p.rewards_lower <= p.rewards + 1e-12 and p.rewards <= p.rewards_upper + 1e-12, name
    for name, b in doc.band.items():
        if b.lower is not None:
            assert b.lower <= b.model + 1e-9 and b.model <= b.upper + 1e-9, name


@pytest.mark.parametrize("venue", ["kalshi", "polymarket"])
def test_live_score_identities_bounds_and_brackets(venue: str, kalshi_run: Any, poly_run: Any) -> None:
    run = kalshi_run if venue == "kalshi" else poly_run
    doc = run.score
    assert (doc.source, doc.model_id, doc.n_orders) == ("live", run.calibration.best, len(run.truth["own"]))
    check_identities(doc)
    tb = doc.band["trading_bound"]
    assert tb.lower <= doc.live.trading <= tb.upper  # the per-order bound holds the true (live) trading PnL
    nb = doc.band["net_trading_bound"]
    assert nb.lower <= doc.live.trading - doc.live.fees <= nb.upper
    assert [s.bracket_violations for s in doc.sim_vs_live] == [0] * 5
    assert [(s.model_id, s.orders) for s in doc.sim_vs_live] == [
        (doc.model_id, "all"),
        ("trade_through_only", "all"),
        ("front_of_queue", "all"),
        (doc.model_id, "train"),  # the calibration was fitted on this capture: its orders, in-sample
        (doc.model_id, "test"),
    ]
    train, test = doc.sim_vs_live[3], doc.sim_vs_live[4]
    assert train.n_orders + test.n_orders == doc.sim_vs_live[0].n_orders
    assert (train.n_orders, test.n_orders) == (run.calibration.n_train_orders, run.calibration.n_test_orders)
    pess, opt = doc.sim_vs_live[1], doc.sim_vs_live[2]
    assert pess.sim_only == 0 and opt.live_only == 0  # the bounds bracket every live fill
    assert doc.live.fills == sum(len(o["fills"]) for o in run.truth["own"])
    # the fixtures are too short to trust a fit: the score says it scores a refused calibration, and nothing else
    assert (doc.model_source, doc.calibration_trust) == ("calibration", run.calibration.trust)
    assert run.calibration.trust.verdict == "refused" and "few_orders" in run.calibration.trust.reasons
    assert len(doc.markouts) == 3 * 4 and [w.split(" (")[0] for w in doc.warnings] == [
        "model: the calibration's verdict is refused"
    ]
    lo, hi = doc.trading_pnl_ci
    assert lo <= doc.scenarios["model"].trading - doc.scenarios["model"].fees <= hi
    orders = pq.read_table(run.root / "sc" / "pm_orders.parquet").to_pylist()
    for scen in ("pessimistic", "model", "optimistic", "live"):
        rows = [o for o in orders if o["scenario"] == scen]
        part = doc.live if scen == "live" else doc.scenarios[scen]
        assert sum(o["trading_pnl"] for o in rows) == pytest.approx(part.trading)
        assert sum(o["fees"] for o in rows) == pytest.approx(part.fees)
    fills = pq.read_table(run.root / "sc" / "pm_fills.parquet").to_pylist()
    assert sum(1 for f in fills if f["scenario"] == "live") == doc.live.fills


def test_live_orders_are_scored_over_their_real_life(kalshi_run: Any, tmp_path: Path) -> None:
    """The window only bounds the sim-versus-live comparison: the live arm, its rewards and the replay of a cancelled
    order cover the order's real life. The fixture's orders rest 4 to 12 s and are cancelled; with a 3 s window the
    old cap cut the live fills from 16 to 8 and the live trading PnL from $13.80 to $9.04."""
    cal = {"path": str(kalshi_run.root / "cal")}
    short = score({"pm": {"path": str(kalshi_run.pm)}, "calibration": cal, "window_ns": 3 * S, "out": str(tmp_path)})
    ref = kalshi_run.score
    assert short.live.fills == ref.live.fills == 16 and round(short.live.trading, 2) == 13.80
    assert short.live.trading == pytest.approx(ref.live.trading) and short.live.fees == pytest.approx(ref.live.fees)
    assert short.rewards["by_scenario"]["live"] == pytest.approx(ref.rewards["by_scenario"]["live"])
    rows = pq.read_table(tmp_path / "pm_orders.parquet").to_pylist()
    live = {r["order_key"]: r for r in rows if r["scenario"] == "live"}
    long_lived = [r for r in live.values() if r["end"] == "ended" and r["ts_end"].value - r["ts_live"].value > 3 * S]
    assert long_lived  # orders that rested past the window and were then cancelled
    for r in rows:
        lv = live[r["order_key"]]
        if r["scenario"] != "live" and lv["end"] == "ended" and r["end"] == "cancelled":
            assert r["ts_end"].value == lv["ts_end"].value  # the replay rests until the live cancel


def test_kalshi_lip_rewards_fees_and_mark(kalshi_run: Any) -> None:
    doc = kalshi_run.score
    (prog,) = doc.rewards["programs"]
    assert (prog["program_id"], prog["period_reward"], prog["target_size"], prog["total_snapshots"]) == (
        "lip-demo-1",
        "50",
        30_000,
        86_400,
    )
    by = doc.rewards["by_scenario"]
    assert by["live"]["point"] > 0 and by["model"]["point"] > 0
    # resting longer (fewer fills) earns more: the pessimistic bound rests the most
    assert by["pessimistic"]["point"] >= by["model"]["point"] >= by["optimistic"]["point"]
    assert doc.mark == {"KXDEMO-26OCT23-T50": {"value": 1.0, "source": "settlement"}}
    # every live fill carried the venue's fee_cost: the live fees are their sum
    fees = sum(
        float(m["msg"]["msg"]["fee_cost"])
        for m in map(json.loads, (FIX / "kalshi" / "ws.jsonl").read_text().splitlines())
        if isinstance(m.get("msg"), dict) and m["msg"].get("type") == "fill"
    )
    assert doc.live.fees == pytest.approx(fees)


def test_polymarket_rewards_rebates_and_settlement(poly_run: Any) -> None:
    doc = poly_run.score
    (prog,) = doc.rewards["programs"]
    assert (prog["day"], prog["daily_reward"], prog["max_spread_cents"], prog["min_size"]) == (
        "2026-10-23",
        "50",
        3.0,
        20_000_000,
    )
    live = doc.rewards["by_scenario"]["live"]
    assert 0 < live["lower"] <= live["point"] <= live["upper"]
    assert doc.live.rebates > 0 and doc.live.fees == 0  # makers pay nothing and earn a share of the taker fees
    assert doc.mark == {"will-the-demo-event-happen": {"value": 0.0, "source": "settlement"}}


def test_hypothetical_quotes(kalshi_run: Any, tmp_path: Path) -> None:
    doc = score(
        {
            "pm": {"path": str(kalshi_run.pm)},
            "quotes": {"path": str(FIX / "quotes.csv")},
            "model": "power3[n=1]",
            "out": str(tmp_path / "q"),
        }
    )
    assert (doc.source, doc.n_orders, doc.live, doc.sim_vs_live, doc.model_id) == (
        "quotes",
        20,
        None,
        [],
        "power3[n=1]",
    )
    check_identities(doc)
    assert doc.scenarios["optimistic"].fills >= doc.scenarios["model"].fills >= doc.scenarios["pessimistic"].fills


def test_settle_override_unsettled_marks_and_fee_options(kalshi_run: Any, tmp_path: Path) -> None:
    base = {"pm": {"path": str(kalshi_run.pm)}, "model": "risk_adverse", "bootstrap_b": 0}
    flipped = score({**base, "settle": {"KXDEMO-26OCT23-T50": "0"}, "out": str(tmp_path / "s0")})
    assert flipped.mark["KXDEMO-26OCT23-T50"] == {"value": 0.0, "source": "override"}
    one = score({**base, "out": str(tmp_path / "s1")})
    pos = one.live.position  # contracts long YES at the end
    assert one.live.trading - flipped.live.trading == pytest.approx(pos * 1.0)
    root = tmp_path / "unsettled"
    shutil.copytree(kalshi_run.pm, root)
    mk = json.loads((root / "markets.json").read_text())
    mk["markets"][0]["settlement"] = None
    del mk["markets"][0]["kalshi_fee"]
    (root / "markets.json").write_text(json.dumps(mk))
    doc = score({**base, "pm": {"path": str(root)}, "out": str(tmp_path / "s2")})
    assert doc.mark["KXDEMO-26OCT23-T50"]["source"] == "last_mid"
    assert any("marked to the last mid" in w for w in doc.warnings)
    assert any("no series fee metadata" in w for w in doc.warnings)
    flat = score({**base, "flat_taker": "0.02", "flat_maker": "0.01", "out": str(tmp_path / "s3")})
    assert flat.scenarios["model"].fees == pytest.approx(0.01 * flat.scenarios["model"].filled_qty)
    assert any("override it" in w for w in flat.warnings)
    with pytest.raises(QtError) as e:
        score({**base, "flat_taker": "0.02", "out": str(tmp_path / "s4")})
    assert e.value.code == "QT-CONFIG-001"


def test_score_spec_errors(kalshi_run: Any, tmp_path: Path) -> None:
    base = {"pm": {"path": str(kalshi_run.pm)}, "out": str(tmp_path / "o")}
    for bad, code in [
        ({"model": "front_of_queue"}, "QT-CONFIG-001"),
        ({"model": "power3[n=1]", "calibration": {"path": str(kalshi_run.root / "cal")}}, "QT-USAGE-001"),
        ({"calibration": {"path": str(tmp_path / "none")}}, "QT-INPUT-001"),
        ({"settle": {"KXDEMO-26OCT23-T50": "1.5"}}, "QT-CONFIG-001"),
        ({"flat_taker": "-1"}, "QT-CONFIG-001"),
        ({"window_ns": 0}, "QT-CONFIG-001"),
    ]:
        with pytest.raises(QtError) as e:
            score({**base, **bad})
        assert e.value.code == code, bad


@pytest.mark.parametrize(
    ("csv_text", "code"),
    [
        (
            "quote_id,instrument,side,price,qty,ts_submit\nq,KXDEMO-26OCT23-T50,buy,0.44,1,1\nq,KXDEMO-26OCT23-T50,buy,0.44,1,1\n",
            "QT-INPUT-003",
        ),
        ("quote_id,instrument,side,price,qty,ts_submit\nq,KXDEMO-26OCT23-T50,hold,0.44,1,1\n", "QT-INPUT-002"),
        ("quote_id,instrument,side,price,qty,ts_submit\nq,KXDEMO-26OCT23-T50,buy,0.44001,1,1\n", "QT-INPUT-004"),
        ("quote_id,instrument,side,price,qty,ts_submit\nq,KXDEMO-26OCT23-T50,buy,0.44,0.001,1\n", "QT-INPUT-004"),
        ("quote_id,instrument,side,price,qty,ts_submit\nq,NOPE,buy,0.44,1,1\n", "QT-INPUT-002"),
        ("quote_id,side,price,qty\nq,buy,0.44,1\n", "QT-INPUT-002"),
        (
            "quote_id,instrument,side,price,qty,ts_submit,latency_ns\nq,KXDEMO-26OCT23-T50,buy,0.44,1,1,-5\n",
            "QT-INPUT-002",
        ),
    ],
)
def test_bad_quotes(kalshi_run: Any, tmp_path: Path, csv_text: str, code: str) -> None:
    p = tmp_path / "q.csv"
    p.write_text(csv_text, encoding="utf-8")
    with pytest.raises(QtError) as e:
        score(
            {"pm": {"path": str(kalshi_run.pm)}, "quotes": {"path": str(p)}, "model": "log", "out": str(tmp_path / "o")}
        )
    assert e.value.code == code


def test_reward_integral_by_hand(tmp_path: Path) -> None:
    """integrate(): value x seconds, piecewise constant between book batches and own steps, inside the window."""
    rows = l2_dataset(
        tmp_path, [resync(T, [(BID, 4900, 10), (ASK, 5100, 10)]), depth(T + 10 * S, [(BID, 4900, 10, 12)])]
    )
    steps = [OwnStep(T + 2 * S, BID, 4900, "o", 4), OwnStep(T + 7 * S, BID, 4900, "o", -4)]
    seen: list[tuple] = []

    def ev(bids, asks, own):
        seen.append((bids[0], tuple(own[BID].items())))
        return (1.0 if own[BID] else 0.0, float(bids[0][1]))

    v = integrate(rows, steps, True, ev, T, T + 10 * S)
    assert v[0] == pytest.approx(5.0)  # own size rested 5 s
    assert v[1] == pytest.approx(10 * 2 + 14 * 5 + 10 * 3)  # the book's best bid size, own size added for 5 s
    w = integrate(rows, steps, False, ev, T + 3 * S, T + 6 * S)
    assert w[0] == pytest.approx(3.0) and w[1] == pytest.approx(30.0)  # the published book already holds it
    assert integrate(rows, [], True, ev, T + 20 * S, T + 21 * S)[0] == pytest.approx(0.0)
