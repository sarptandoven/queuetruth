"""The scorecard: exact PnL after fees on a hand-computed episode, markouts and adverse selection, fill rates by
order class, execution cost against the decision-time mid, diagnostics (and the overlap sweep against every pair),
the running account, outputs and determinism."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from queuetruth.sandbox import Sandbox
from queuetruth.sandbox.orders import account
from queuetruth.sandbox.scorecard import overlap_counts, scorecard_digest
from sandbox.sbx_helpers import SYNTH, Tape, at, base_spec, px, random_agent, write_dataset

pytestmark = pytest.mark.requires_core
S = 1_000_000_000

FEES = {"maker_per_unit": -0.005, "taker_per_unit": 0.01, "taker_bps": 1.0}


@pytest.fixture
def round_trip(tmp_path: Path) -> Path:
    """Bid 100.00 x 5 and ask 100.05 x 5; asks 100.07 x 3 (13 s) and 100.10 x 5 (13.5 s); at 14 s a buyer takes the
    whole 100.05 level and the 100.07 level; clock ticks every second to 40 s. Multiplier 10."""
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    s1 = t.add(at(1), "A", px(5), 5)
    t.noop(at(11))
    s2 = t.add(at(13), "A", px(7), 3)
    t.add(at(13.5), "A", px(10), 5)
    t.trade(at(14), "B", [(s1, 5), (s2, 3)])
    for k in range(15, 41):
        t.noop(at(k))
    return write_dataset(tmp_path / "ds", t, multiplier=10.0)


def _round_trip(ds: Path, out: Path | None = None):
    with Sandbox.open(
        base_spec(ds, start=at(10), end=at(35), latency="1ms", fees=FEES, markout_horizons=["1s", "10s"])
    ) as sb:
        sb.submit("buy", 2, type="market")
        seen = [sb.advance(to=at(12))]
        sb.submit("sell", 2, price="100.06")
        seen += [sb.advance(to=at(20)), sb.advance(to=at(30))]
        return sb.finish(out=out), seen


def test_round_trip_pnl_fees_and_markouts_by_hand(round_trip: Path, tmp_path: Path) -> None:
    card, seen = _round_trip(round_trip)
    buy, sell = card.fills
    assert (buy.order_id, buy.price, buy.qty, buy.liquidity, buy.reason, buy.ts_event) == (
        "o1",
        px(5),
        2,
        "taker",
        "crossed_at_live",
        at(10) + 1_000_000,
    )
    assert (sell.order_id, sell.price, sell.qty, sell.liquidity, sell.reason, sell.ts_event) == (
        "o2",
        px(6),
        2,
        "maker",
        "trade_through",
        at(14),
    )
    p = card.pnl
    # cash = (-2 x 100.05 + 2 x 100.06) x 10 = 0.2; fees: taker 2 x 0.01 + 2001.0 x 1 bp = 0.2201, maker -0.01
    assert (p.position_end, p.bought_qty, p.sold_qty) == (0, 2, 2)
    assert p.cash == 0.2 and p.pnl_gross == 0.2
    assert p.fees_taker == 0.2201 and p.fees_maker == -0.01 and p.fees == 0.2101
    assert p.pnl_net == -0.0101 and p.pnl_net_at_touch == -0.0101
    assert p.notional_traded == 4002.2 and (p.mark_source, p.mark_price) == ("mid", 100.05)
    assert buy.fee == 0.2201 and sell.fee == -0.01
    rows = {(m.horizon_ns, m.liquidity): m for m in card.markouts}
    # buy at 100.05, mid 100.025 one second later (-2.5 ticks), 100.05 ten seconds later (0); the sell at 100.06
    # sees a mid of 100.05 after both horizons (+1 tick)
    assert rows[(S, "taker")].mean_ticks == -2.5 and rows[(S, "taker")].total_ccy == pytest.approx(-0.5)
    assert rows[(10 * S, "taker")].mean_ticks == 0.0
    assert rows[(S, "maker")].mean_ticks == 1.0 and rows[(S, "maker")].total_ccy == pytest.approx(0.2)
    assert rows[(S, "all")].n == 2 and rows[(S, "all")].mean_ticks == -0.75 and rows[(S, "all")].n_adverse == 1
    adv = {a.horizon_ns: a for a in card.adverse_selection}
    assert adv[S].cost_ccy == pytest.approx(-0.2) and adv[S].adverse_fraction == 0.0 and adv[S].n_maker_fills == 1
    c = card.execution_cost
    assert (c.n_fills, c.edge_taker_ccy, c.edge_maker_ccy) == (2, -0.5, 0.7)
    assert c.edge_ccy == pytest.approx(0.2) and c.mean_edge_ticks == 0.5
    rates = {r.order_class: r for r in card.fill_rates}
    assert (rates["market"].n_orders, rates["market"].rate_full) == (1, 1.0)
    assert (rates["passive_limit"].n_live, rates["passive_limit"].median_ttf_ns) == (1, S)
    assert rates["marketable_limit"].n_orders == 0 and rates["marketable_limit"].rate_any is None
    assert (rates["all"].n_orders, rates["all"].filled_qty, rates["all"].rate_qty) == (2, 4, 1.0)
    # CME rules expect every file to start with a snapshot; the hand-made data has none (files_without_snapshot)
    assert card.consistency.equal and card.data_quality == "warnings"
    assert "data quality verdict of the replayed partition: warnings" in card.warnings
    # the in-session markouts are the scorecard's, as soon as their horizon was visible
    in_session = {(m.order_id, m.fill_seq, m.horizon_ns): m.mo_ticks for o in seen for m in o.markouts}
    assert in_session == {("o1", 1, S): -2.5, ("o1", 1, 10 * S): 0.0, ("o2", 1, S): 1.0, ("o2", 1, 10 * S): 1.0}


def test_open_position_is_marked_at_the_mid_and_at_the_touch(round_trip: Path) -> None:
    with Sandbox.open(base_spec(round_trip, start=at(10), end=at(35), latency="1ms")) as sb:
        sb.submit("buy", 2, type="market")
        sb.advance(to=at(16))
        card = sb.finish()
    p = card.pnl
    # long 2 bought at 100.05; at 16 s the book is 100.00 / 100.10: mid 100.05, bid 100.00
    assert p.position_end == 2 and p.mark_price == 100.05 and p.pnl_gross == 0.0
    assert p.pnl_net_at_touch == -1.0  # (100.00 - 100.05) x 2 x 10
    assert any("open position 2" in w for w in card.warnings)


def test_episode_without_orders(round_trip: Path) -> None:
    with Sandbox.open(base_spec(round_trip, start=at(10), end=at(35))) as sb:
        sb.advance(by="5s")
        card = sb.finish()
    assert card.orders == [] and card.fills == [] and card.data_quality == "not_replayed"
    assert card.pnl.pnl_net == 0.0 and card.pnl.position_end == 0
    assert all(r.n_orders == 0 and r.rate_any is None for r in card.fill_rates)
    assert card.consistency.equal and card.consistency.fills_reported == 0


def test_outputs_and_determinism(round_trip: Path, tmp_path: Path) -> None:
    a, _ = _round_trip(round_trip, tmp_path / "a")
    b, _ = _round_trip(round_trip, tmp_path / "b")
    da, db = a.model_dump(mode="json"), b.model_dump(mode="json")
    for d in (da, db):
        d.pop("timings")
        d.pop("files")
    assert da == db and a.run_id == b.run_id and a.certificate == b.certificate
    assert a.certificate.scorecard_sha256 == scorecard_digest(a.model_dump(mode="json"))
    on_disk = json.loads((tmp_path / "a" / "scorecard.json").read_text())
    assert on_disk == a.model_dump(mode="json")
    names = [f.name for f in a.files]
    assert names[:2] == ["order_log.json", "markouts.parquet"]
    assert {f"replay/{n}" for n in ("summary.json", "bbo.parquet", "trades.parquet", "shadow_fills.parquet")} <= set(
        names
    )
    mo = pq.read_table(tmp_path / "a" / "markouts.parquet")
    assert mo.schema.metadata[b"qt.schema"] == b"qt.score_markouts" and mo.num_rows == 4
    log = json.loads((tmp_path / "a" / "order_log.json").read_text())
    assert log["root"] == a.certificate.log_root


def test_in_session_markouts_equal_the_scorers(tmp_path: Path) -> None:
    """Markouts the agent saw during the session equal `compute_markouts` on the final replay's quotes (bitwise)."""
    with Sandbox.open(
        {
            "dataset": {"path": str(SYNTH)},
            "instrument_id": 2,
            "session_date": "2026-06-15",
            "start": "15:00",
            "end": "15:15",
            "latency": "300us",
            "markout_horizons": ["500ms", "5s", "30s"],
        }
    ) as sb:
        seen = random_agent(sb, random.Random(21), steps=70)
        card = sb.finish(out=tmp_path / "out")
    final = {
        (r["order_key"], r["fill_seq"], r["horizon_ns"]): (r["mo_ticks"], r["mo_bps"], r["mo_ccy"])
        for r in pq.read_table(tmp_path / "out" / "markouts.parquet").to_pylist()
    }
    n = 0
    for o in seen:
        for m in o.markouts:
            assert final[(m.order_id, m.fill_seq, m.horizon_ns)] == (m.mo_ticks, m.mo_bps, m.mo_ccy)
            n += 1
    assert n >= 30 and card.consistency.equal


def test_overlap_diagnostics(round_trip: Path) -> None:
    with Sandbox.open(base_spec(round_trip, start=at(10), end=at(35), latency="1ms")) as sb:
        sb.submit("buy", 1, price="99.99")
        sb.submit("buy", 1, price="99.99")
        sb.submit("buy", 1, type="market")
        sb.submit("buy", 1, type="market")
        sb.advance(to=at(12))
        card = sb.finish()
    d = card.diagnostics
    assert (d.same_price_overlaps, d.taker_same_event_overlaps, d.self_cross_pairs) == (1, 1, 0)
    assert any("overlapped" in w for w in card.warnings)


def _overlaps_by_pairs(live: list[tuple[str, int, int, int]]) -> tuple[int, int]:
    """The definition: every pair of resting intervals [start, end) that overlap in time."""
    same = cross = 0
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            (sa, pa, a0, a1), (sb_, pb, b0, b1) = live[i], live[j]
            if not (a0 < b1 and b0 < a1):
                continue
            if sa == sb_ and pa == pb:
                same += 1
            elif sa != sb_:
                buy, sell = (pa, pb) if sa == "buy" else (pb, pa)
                cross += buy >= sell
    return same, cross


def test_overlap_sweep_equals_every_pair() -> None:
    rng = random.Random(11)
    for n in (0, 1, 2, 7, 60, 400):
        live = []
        for _ in range(n):
            start = rng.randrange(0, 1_000)
            live.append((rng.choice(["buy", "sell"]), rng.randrange(95, 106), start, start + rng.randrange(1, 300)))
        live += live[: n // 10]  # exact duplicates: same start, end and price
        assert overlap_counts(live) == _overlaps_by_pairs(live), n


def test_the_running_account_equals_the_fills(round_trip: Path) -> None:
    """The session books each fill once as it is reported; the position and PnL it shows equal a recomputation."""
    with Sandbox.open(base_spec(round_trip, start=at(10), end=at(35), latency="1ms", fees=FEES)) as sb:
        sb.submit("buy", 2, type="market")
        sb.advance(to=at(12))
        sb.submit("sell", 2, price="100.06")
        obs = sb.advance(to=at(20))
        fills = [
            (o.sign, int(f["fill_qty"]), int(f["fill_px"]), str(f["liquidity"])) for o in sb.orders for f in o.fills
        ]
        again = account(fills, sb.spec.fees, **sb._units())
        assert (sb._acct.qty, sb._acct.cash_raw, sb._acct.fees) == (again.qty, again.cash_raw, again.fees)
        assert obs.position.qty == 0 and obs.position.fees == float(again.fees) == 0.2101
