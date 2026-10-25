"""`pm ingest` on the committed fixtures against what the generator knows exactly (`truth.json`): the rebuilt book,
the own orders and fills, every queue entry point, the official queue positions and the on-chain queue truth."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from pm._helpers import (
    FIX,
    MS,
    T0,
    S,
    k_delta,
    k_fill,
    k_snapshot,
    k_subscribe,
    k_trade,
    k_user_order,
    write_jsonl,
)

from queuetruth.errors import QtError
from queuetruth.pm.book import ASK, BID, Ladder
from queuetruth.pm.calibrate import LevelIndex, calibrate
from queuetruth.pm.dataset import KINDS, read_l2
from queuetruth.pm.ingest import chain_queue, ingest, print_sides
from queuetruth.pm.keccak import keccak256
from queuetruth.pm.queue import Engine, Model, SimOrder
from queuetruth.pm.score import score
from queuetruth.pm.workdir import legs, load


def poly_hash(sim_id: str) -> str:
    """The fixture's order hash of a simulated order id."""
    return "0x" + keccak256(f"demo-order:{sim_id}".encode()).hex()


def final_book(root: Path) -> dict[str, dict[str, int]]:
    rows = read_l2(root, 1)
    view = Ladder()
    for r in range(len(rows)):
        kind = KINDS[rows.kind[r]]
        if kind == "clear":
            view.clear()
        elif kind in ("snapshot", "depth"):
            view.set(int(rows.side[r]), int(rows.price[r]), int(rows.new_qty[r]))
    return {s: {str(p): q for p, q in view.levels(side)} for s, side in (("B", BID), ("A", ASK))}


@pytest.mark.parametrize("venue", ["kalshi", "polymarket"])
def test_the_published_book_ends_where_the_simulated_book_ended(venue: str, kalshi_run: Any, poly_run: Any) -> None:
    run = kalshi_run if venue == "kalshi" else poly_run
    assert final_book(run.pm / "book") == run.truth["final_book"]
    assert run.ingest.book.verdict == "clean" and run.ingest.book_own_free.verdict == "clean"
    (inst,) = run.ingest.instruments
    assert inst.trades == inst.trades_matched and inst.trades_unmatched == 0 and inst.feed_gaps == 0
    assert inst.batches == run.truth["events"] + 1  # every simulated event is one batch, plus the first snapshot


@pytest.mark.parametrize("venue", ["kalshi", "polymarket"])
def test_own_orders_fills_and_queue_entry_points(venue: str, kalshi_run: Any, poly_run: Any) -> None:
    run = kalshi_run if venue == "kalshi" else poly_run
    d = load(run.pm)
    own = run.truth["own"]
    assert run.ingest.live["n_orders"] == len(own)
    assert run.ingest.live["n_fills"] == sum(len(o["fills"]) for o in own)
    gl = legs(d)
    if venue == "kalshi":
        key = {f"kalshi:default:{o['order_id']}": o for o in own}
    else:
        key = {f"polymarket:default:{poly_hash(o['order_id'])}": o for o in own}
    assert {g.order_key for g in gl} == set(key)
    for g in gl:
        o = key[g.order_key]
        assert (g.price, g.qty) == (o["price"], o["qty"])
        assert [(q, p) for _t, q, p in g.fills] == [(f["qty"], f["price"]) for f in o["fills"]]
    # the queue each order joined behind: the own-free book at its entry point
    eng = Engine([Model("risk_adverse")])
    sims = [SimOrder(g.key, g.side, g.price, float(g.qty), entry_seq=g.entry_seq, inside=g.inside) for g in gl]
    res = eng.run(d.book(1), sims)
    got = [r.visible_at_live for r in res]
    want = [key[g.order_key]["ahead_at_entry"] for g in gl]
    assert got == want
    ent = run.ingest.own_free
    if venue == "kalshi":  # placements and cancels carry client_order_id; fills are aligned to their batch
        cancels = sum(1 for o in own if "cancel_ms" in o)
        assert (ent["exact"], ent["aligned"]) == (len(own) + cancels, run.ingest.live["n_fills"])
        assert (ent["unaligned"], ent["outside"], ent["clamps"]) == (0, 0, 0)
    else:
        assert (ent["exact"], ent["unaligned"], ent["outside"], ent["clamps"]) == (0, 0, 0, 0)


def test_kalshi_queue_positions_are_the_true_queue_ahead(kalshi_run: Any) -> None:
    rows = pq.read_table(kalshi_run.pm / "pm" / "queue_samples.parquet").to_pylist()
    truth = {(o["order_id"], s["t_ms"]): s["queue_ahead"] for o in kalshi_run.truth["own"] for s in o["samples"]}
    assert len(rows) == len(truth) == kalshi_run.ingest.queue_samples == 76
    for r in rows:
        assert r["order_key"] == f"kalshi:default:{r['venue_order_id']}"
        t_ms = (r["ts_local_send"].value + 18_000_000) // 1_000_000  # the request went out 18 ms before the sample
        assert truth[(r["venue_order_id"], t_ms)] == r["queue_position"]
    m = json.loads((kalshi_run.pm / "markets.json").read_text())["markets"][0]
    assert m["settlement"] == "1.0000" and m["kalshi_fee"]["fee_type"] == "quadratic_with_maker_fees"
    assert m["lip"][0]["period_reward"] == "50" and m["lip"][0]["target_size"] == 30_000


def test_polymarket_chain_queue_truth_equals_the_simulated_queue(poly_run: Any) -> None:
    rows = pq.read_table(poly_run.pm / "pm" / "chain_queue.parquet").to_pylist()
    truth = {poly_hash(o["order_id"]): o for o in poly_run.truth["own"] if o["fills"]}
    assert len(rows) == len(truth) == poly_run.ingest.chain["queue_truth_orders"] == 15
    for r in rows:
        assert r["ahead_others"] == truth[r["order_hash"]]["fills"][0]["ahead_before"]
        assert r["ahead_own"] == 0  # the fixture's strategy never has two orders on one side
    filled = pq.read_table(poly_run.pm / "pm" / "filled_orders.parquet").to_pylist()
    makers = {m["order_hash"] for x in poly_run.truth["matches"] for m in x["makers"]}
    assert {f["order_hash"] for f in filled} == makers and all(f["price_consistent"] for f in filled)
    ch = poly_run.ingest.chain
    assert (ch["fills_with_ws_time"], ch["inexact_prices"], ch["unknown_tokens"]) == (ch["fills"], 0, 0)
    m = json.loads((poly_run.pm / "markets.json").read_text())["markets"][0]
    assert m["settlement"] == "0" and m["poly_fee"]["rate"] == "0.04"


def test_chain_queue_counts_own_size_ahead_and_unplaced_orders() -> None:
    from queuetruth.pm.batches import Batch, TradeMsg

    tx1, tx2 = "0xaa", "0xbb"
    t = [TradeMsg(10, 10, 4500, 30, ASK, tx_hash=tx1), TradeMsg(10, 10, 4500, 4, ASK, tx_hash=tx2)]
    batches = {1: [Batch("depth", 10, 10, 7, changes=[(BID, 4500, 50, 16)], trades=t)]}

    def row(tx: str, li: int, h: str, size: int, own: bool, price: int = 4500) -> dict:
        return {
            "role": "maker",
            "tx": tx,
            "block": 1,
            "log_index": li,
            "order_hash": h,
            "size": size,
            "own": own,
            "instrument_id": 1,
            "canon_side": BID,
            "price": price,
        }

    rows = [
        row(tx1, 0, "0xo1", 10, False),
        row(tx1, 1, "0xmine1", 5, True),  # the user's earlier order at the level
        row(tx1, 2, "0xo2", 7, False, price=4400),  # another level: not ahead
        row(tx1, 3, "0xmine2", 8, True),
        row(tx2, 4, "0xo3", 4, False),  # after it: not ahead
        row("0xcc", 5, "0xmine3", 1, True),  # its print is in no batch
    ]
    out, unplaced, no_hash = chain_queue(rows, batches, {"0xmine1": "k1", "0xmine2": "k2", "0xmine3": "k3"})
    got = {r["order_key"]: (r["ahead_others"], r["ahead_own"], r["makers_ahead"], r["seq"]) for r in out}
    assert got == {"k1": (10, 0, 1, 7), "k2": (10, 5, 2, 7)}
    assert (unplaced, no_hash) == (1, 0)


def test_print_sides_are_checked_against_the_chain_takers(poly_run: Any) -> None:
    """`last_trade_price.side` is read as the taker's side; the chain's `OrdersMatched` names each taker's side, so
    every print with a transaction hash of the export is checked (the fixture follows the reading: no mismatch)."""
    from queuetruth.pm.batches import Batch, TradeMsg
    from queuetruth.pm.chain import ChainDecode, Match

    ch = poly_run.ingest.chain
    assert ch["prints_side_checked"] >= 60 and ch["prints_side_mismatch"] == 0

    def match(tx: str, side: str, token: str) -> Match:
        return Match(tx, 1, 0, None, "0xex", "v2", 0, "0xh", "0xw", side, int(token), 0, 0, (), None)

    chain = ChainDecode(matches=[match("0xa1", "BUY", "11"), match("0xa2", "BUY", "22"), match("0xa3", "SELL", "22")])
    token = {"11": ("m", True), "22": ("m", False)}  # YES token 11, NO token 22
    prints = [
        TradeMsg(1, 1, 5000, 1, BID, tx_hash="0xA1"),  # a YES buy: consistent (hash case ignored)
        TradeMsg(1, 1, 5000, 1, BID, tx_hash="0xa2"),  # a NO buy sells YES: inconsistent
        TradeMsg(1, 1, 5000, 1, BID, tx_hash="0xa3"),  # a NO sell buys YES: consistent
        TradeMsg(1, 1, 5000, 1, ASK, tx_hash="0xff"),  # not in the export: not checked
        TradeMsg(1, 1, 5000, 1, ASK),
    ]
    batches = {7: [Batch("depth", 1, 1, 1, trades=prints)]}
    assert print_sides(chain, token, {"m": 7}, batches) == (3, 1)


def test_ingest_is_deterministic(kalshi_run: Any, tmp_path: Path) -> None:
    again = ingest(
        {
            "venue": "kalshi",
            "inputs": [{"path": str(FIX / "kalshi" / "ws.jsonl")}, {"path": str(FIX / "kalshi" / "rest.jsonl")}],
            "out": str(tmp_path / "again"),
        }
    )
    assert again.run_id == kalshi_run.ingest.run_id
    assert again.book.dataset_id == kalshi_run.ingest.book.dataset_id
    assert [(f.name, f.sha256) for f in again.files] == [(f.name, f.sha256) for f in kalshi_run.ingest.files]
    assert again.counters == kalshi_run.ingest.counters
    assert again.counters["derived_fills_superseded"] == 16 and again.warnings == []


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ({"venue": "kalshi", "inputs": [], "out": "x"}, "QT-CONFIG-001"),
        ({"venue": "nyse", "inputs": [{"path": "a"}], "out": "x"}, "QT-CONFIG-001"),
        ({"venue": "kalshi", "inputs": [{"path": "a"}]}, "QT-CONFIG-001"),
        ({"venue": "kalshi", "inputs": [{"path": "a"}], "chain": [{"path": "b"}], "out": "x"}, "QT-CONFIG-001"),
        ({"venue": "polymarket", "inputs": [{"path": "a"}], "kalshi_yes_price": "true", "out": "x"}, "QT-CONFIG-001"),
        ({"venue": "kalshi", "inputs": [{"path": "missing.jsonl"}], "out": "x"}, "QT-INPUT-001"),
    ],
)
def test_ingest_spec_errors(tmp_path: Path, spec: dict, code: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(QtError) as e:
        ingest(spec)
    assert e.value.code == code


def test_ingest_of_nothing_and_instrument_ids(tmp_path: Path) -> None:
    empty = tmp_path / "e.jsonl"
    empty.write_text('{"recv_ns": 1, "msg": {"type": "ok"}}\n', encoding="utf-8")
    with pytest.raises(QtError, match="no kalshi market data"):
        ingest({"venue": "kalshi", "inputs": [{"path": str(empty)}], "out": str(tmp_path / "o")})
    ws = FIX / "kalshi" / "ws.jsonl"
    doc = ingest(
        {
            "venue": "kalshi",
            "inputs": [{"path": str(ws)}],
            "out": str(tmp_path / "ids"),
            "instrument_ids": {"KXDEMO-26OCT23-T50": 42},
        }
    )
    assert doc.instruments[0].instrument_id == 42
    with pytest.raises(QtError, match="no id for"):
        ingest(
            {"venue": "kalshi", "inputs": [{"path": str(ws)}], "out": str(tmp_path / "x"), "instrument_ids": {"Z": 1}}
        )


def test_an_ingest_never_writes_into_a_used_directory(poly_run: Any, tmp_path: Path) -> None:
    """A second ingest into the same directory used to leave the first run's chain tables behind, and the
    calibration then fitted on them."""
    p = FIX / "polymarket"
    spec = {
        "venue": "polymarket",
        "inputs": [{"path": str(p / f)} for f in ("market.jsonl", "user.jsonl", "gamma.json")],
        "out": str(poly_run.pm),
    }
    with pytest.raises(QtError) as e:
        ingest(spec)
    assert e.value.code == "QT-CONFIG-001" and "exists and is not empty" in e.value.message
    # files the summary does not list are never read: stale chain tables copied next to a chain-less ingest
    root = tmp_path / "nochain"
    ingest({**spec, "out": str(root)})
    for name in ("chain_fills", "chain_queue", "filled_orders"):
        shutil.copy(poly_run.pm / "pm" / f"{name}.parquet", root / "pm" / f"{name}.parquet")
    assert load(root).chain_queue == [] and load(root).chain_fills == []
    c = calibrate({"pm": {"path": str(root)}, "out": str(tmp_path / "cal"), "bootstrap_b": 0})
    assert c.fill_truth.n_chain == 0


def test_run_ids_depend_on_the_capture_bytes_not_their_location(tmp_path: Path) -> None:
    """SPEC 7.3: the same capture in two places gives the same run_id for ingest, calibrate and score."""
    ids = []
    for where in ("a", "b/c"):
        cap = tmp_path / where / "cap"
        shutil.copytree(FIX / "kalshi", cap)
        ing = ingest(
            {
                "venue": "kalshi",
                "inputs": [{"path": str(cap / "ws.jsonl")}, {"path": str(cap / "rest.jsonl")}],
                "out": str(tmp_path / where / "in"),
            }
        )
        cal = calibrate({"pm": {"path": str(tmp_path / where / "in")}, "out": str(tmp_path / where / "cal")})
        sc = score(
            {
                "pm": {"path": str(tmp_path / where / "in")},
                "calibration": {"path": str(tmp_path / where / "cal")},
                "out": str(tmp_path / where / "sc"),
            }
        )
        ids.append((ing.run_id, cal.run_id, sc.run_id))
    assert ids[0] == ids[1]


def amend_up_capture(tmp: Path) -> Path:
    """o-7 rests 5 at 0.49, the user's o-8 joins behind it, o-7 is amended up to 8 (back of the queue), a taker
    sells 11 (4 + 3 others, o-8's 2, then 2 of o-7) and o-7 is cancelled."""
    created7, created8 = (T0 + S) // MS, (T0 + 2500 * MS) // MS
    return write_jsonl(
        tmp / "amend.jsonl",
        [
            *k_subscribe(1, False),
            k_snapshot(1, T0, [("0.4900", "4.00")], [("0.5000", "7.00")]),
            k_delta(2, T0 + S, "yes", "0.4900", "5.00", clock=10, cid="mm-7"),
            k_user_order(T0 + S, "o-7", "mm-7", "bid", "0.4900", "5.00", "0.00", "5.00", "resting", created7),
            k_delta(3, T0 + 2 * S, "yes", "0.4900", "3.00", clock=11),
            k_delta(4, T0 + 2500 * MS, "yes", "0.4900", "2.00", clock=12, cid="mm-8"),
            k_user_order(T0 + 2500 * MS, "o-8", "mm-8", "bid", "0.4900", "2.00", "0.00", "2.00", "resting", created8),
            k_delta(5, T0 + 3 * S, "yes", "0.4900", "3.00", clock=13, cid="mm-7"),
            k_user_order(
                T0 + 3 * S, "o-7", "mm-7", "bid", "0.4900", "8.00", "0.00", "8.00", "resting", created7, "Amend"
            ),
            k_trade(T0 + 4 * S, "0.4900", "11.00", "no", "t-1"),
            k_delta(6, T0 + 4 * S, "yes", "0.4900", "-11.00", clock=14),
            k_fill(T0 + 4 * S, "t-1", "o-8", "mm-8", "bid", "0.4900", "2.00", "0.00"),
            k_fill(T0 + 4 * S, "t-1", "o-7", "mm-7", "bid", "0.4900", "2.00", "0.00"),
            k_user_order(T0 + 4 * S, "o-8", "mm-8", "bid", "0.4900", "2.00", "2.00", "0.00", "executed", created8),
            k_user_order(T0 + 4 * S, "o-7", "mm-7", "bid", "0.4900", "8.00", "2.00", "6.00", "resting", created7),
            k_delta(7, T0 + 5 * S, "yes", "0.4900", "-6.00", clock=15, cid="mm-7"),
            k_user_order(T0 + 5 * S, "o-7", "mm-7", "bid", "0.4900", "8.00", "2.00", "0.00", "canceled", created7),
        ],
    )


def test_an_amend_up_re_enters_the_queue_at_the_back(tmp_path: Path) -> None:
    """A size increase loses priority: the order's history goes on from its re-entry point instead of ending (the
    fill after the amend used to be dropped from calibration and scoring)."""
    out = tmp_path / "in"
    ingest({"venue": "kalshi", "inputs": [{"path": str(amend_up_capture(tmp_path))}], "out": str(out)})
    d = load(out)
    gl = legs(d)
    by = {g.key: g for g in gl}
    k7, k8 = "kalshi:default:o-7", "kalshi:default:o-8"
    assert set(by) == {k7, f"{k7}#up1", k8}
    first, again = by[k7], by[f"{k7}#up1"]
    assert (first.qty, first.how, first.end_ts, first.fills) == (500, "exact+increase", T0 + 3 * S, [])
    assert (again.order_key, again.qty, again.how, again.inside) == (k7, 800, "exact", True)
    assert again.fills == [(T0 + 4 * S, 200, 4900)] and again.end_ts == T0 + 5 * S and again.end_seq is not None
    # behind the others' 7 contracts (the own-free book after the amend) and the user's o-8 (own size ahead)
    i = gl.index(again)
    assert LevelIndex(gl).own_ahead(i, T0 + 3500 * MS) == 200
    sim = SimOrder(again.key, again.side, again.price, 800.0, entry_seq=again.entry_seq, inside=True, **again.sim_end())
    (res,) = Engine([Model("risk_adverse")]).run(d.book(1), [sim])
    # an independent shadow: o-8's 2 contracts ahead are not in its queue, so it fills 4 where the order filled 2
    assert res.visible_at_live == 700 and [f[:2] for f in res.fills[0]] == [(T0 + 4 * S, 400.0)]
    doc = score({"pm": {"path": str(out)}, "model": "risk_adverse", "out": str(tmp_path / "sc"), "bootstrap_b": 0})
    assert doc.live.fills == 2 and doc.live.filled_qty == 4.0  # the fill after the amend counts
    assert any(w.startswith("own orders: 2 of 3 order(s) joined behind your own size") for w in doc.warnings)


def test_dropped_subscription_messages_are_a_data_quality_reason(tmp_path: Path) -> None:
    recs = [
        *k_subscribe(1, False),
        k_snapshot(1, T0, [("0.4900", "4.00")], [("0.5000", "7.00")]),
        k_delta(2, T0 + S, "yes", "0.4900", "1.00", clock=10),
        k_delta(7, T0 + 2 * S, "yes", "0.4900", "1.00", clock=11, sid=2),  # the market on a second subscription
    ]
    doc = ingest(
        {
            "venue": "kalshi",
            "inputs": [{"path": str(write_jsonl(tmp_path / "k.jsonl", recs))}],
            "out": str(tmp_path / "o"),
        }
    )
    assert doc.book.verdict == "warnings" and "duplicate_subscription_messages = 1" in doc.book.reasons
    assert any("another subscription delivers were dropped" in w for w in doc.warnings)
