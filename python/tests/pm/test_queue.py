"""Queue models on prediction-market L2 books (`queuetruth.pm.queue`): each corrected-mode rule by hand, the bound
ordering on the fixtures, and agreement with the compiled Rust zoo."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pm._helpers import depth, l2_dataset, resync, trade

from queuetruth.errors import QtError
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.dataset import feed_gaps, read_l2
from queuetruth.pm.queue import Engine, Model, SimOrder

T = 1_792_764_000_000_000_000
MODELS = [
    Model("front_of_queue"),
    Model("trade_through_only"),
    Model("cancels_ahead"),
    Model("risk_adverse"),
    Model("power3", 1.0),
]
FOQ, TT, CA, RA, P3 = range(5)


def fills(res, k: int) -> list[tuple[int, float, str]]:
    return [(t - T, q, why) for t, q, why, _liq, _px in res.fills[k]]


def test_model_ids_parse_and_validate() -> None:
    assert Model.parse("power3[n=1.5]") == Model("power3", 1.5) and Model("power3", 1.5).id == "power3[n=1.5]"
    assert Model.parse("risk_adverse").id == "risk_adverse" and Model.parse("log2").n is None
    for bad in ["power3", "power3[n=x]", "risk_adverse[n=2]", "nope", "power[m=1]", "power[n=-1]"]:
        with pytest.raises(QtError) as e:
            Model.parse(bad)
        assert e.value.code == "QT-CONFIG-001", bad
    with pytest.raises(QtError):
        Engine([])


def test_zoo_spellings_round_trip_and_other_exchange_models_are_refused() -> None:
    assert Model("power3", 1.5).zoo_id == "hbt_power3[exch=partial;mode=corrected;n=1.5]"
    assert Model("power", 0.0183156).zoo_id == "hbt_power[exch=partial;mode=corrected;n=0.0183156]"
    assert Model("log").zoo_id == "hbt_log[exch=partial;mode=corrected]"
    assert Model("risk_adverse").zoo_id == "hbt_risk_adverse[exch=partial;mode=corrected]"
    assert Model("front_of_queue").zoo_id == "front_of_queue[mode=corrected]"
    assert Model("cancels_ahead").zoo_id is None and Model("power3", 1.0, True).zoo_id is None  # not in the zoo
    for m in (Model("power2", 54.5982), Model("log2"), Model("risk_adverse"), Model("trade_through_only")):
        assert Model.parse(m.zoo_id) == m
    for bad in ["hbt_power3[exch=no_partial;n=2]", "front_of_queue[exch=partial]", "cancels_ahead[exch=partial]"]:
        with pytest.raises(QtError, match="exch="):
            Model.parse(bad)


def test_a_placed_end_stops_the_order_before_a_later_batch_with_its_timestamp(tmp_path: Path) -> None:
    """A cancel placed in the published series (`end_seq`) stops the order there; by time alone, a print in a later
    batch with the cancel's millisecond would still fill it."""
    rows = l2_dataset(
        tmp_path,
        [
            resync(T, [(BID, 4900, 10), (ASK, 5100, 10)]),
            depth(T + 10, [(ASK, 5100, 10, 12)]),  # seq 2: the own cancel was in here
            depth(T + 30, [(BID, 4900, 10, 0)], [trade(T + 30, 4900, 15, ASK)]),  # seq 3, the cancel's millisecond
        ],
    )
    common = {"entry_seq": 1, "inside": True, "ts_end": T + 30}
    by_time = SimOrder("t", BID, 4900, 5.0, **common)
    by_seq = SimOrder("s", BID, 4900, 5.0, **common, end_seq=2, end_inside=True)
    before = SimOrder("b", BID, 4900, 5.0, entry_seq=2, inside=True, ts_end=T + 9, end_seq=2, end_inside=False)
    rt, rs, rb = Engine([Model("risk_adverse")]).run(rows, [by_time, by_seq, before])
    assert fills(rt, 0) == [(30, 5.0, "queue")]
    assert fills(rs, 0) == [] and (rs.end, rs.end_ts) == ("cancelled", T + 30)
    assert (rb.end, rb.end_ts, rb.fills) == ("cancelled_before_live", T + 9, [[]])


@pytest.fixture
def queue_book(tmp_path: Path):
    return l2_dataset(
        tmp_path,
        [
            resync(T, [(BID, 4900, 10), (ASK, 5100, 10)]),
            depth(T + 10, [(BID, 4900, 10, 14)]),  # 4 join behind
            depth(T + 20, [(BID, 4900, 14, 11)]),  # a cancel of 3: ahead or behind?
            depth(T + 30, [(BID, 4900, 11, 2)], [trade(T + 30, 4900, 9, ASK)]),  # a print of 9 at the level
            depth(T + 40, [(BID, 4900, 2, 0)], [trade(T + 40, 4900, 2, ASK)]),  # the level empties
            depth(T + 50, [(ASK, 5100, 10, 9)], [trade(T + 50, 4800, 1, ASK)]),  # a print through 0.49
        ],
    )


def test_queue_rules_by_hand(queue_book) -> None:
    eng = Engine(MODELS)
    (r,) = eng.run(queue_book, [SimOrder("o", BID, 4900, 5.0, ts_live=T + 1)])
    assert (r.activated_ts, r.activated_seq, r.visible_at_live) == (T + 10, 2, 10.0)
    # front_of_queue: fills on the first print at its price
    assert fills(r, FOQ) == [(30, 5.0, "queue")]
    # trade_through_only: only the print through its price fills it
    assert fills(r, TT) == [(50, 5.0, "trade_through")]
    # cancels_ahead: the cancel of 3 left the queue ahead (10 -> 7); the print of 9 fills 2 beyond it,
    # the next print of 2 fills 2 more, the trade through the last 1
    assert fills(r, CA) == [(30, 2.0, "queue"), (40, 2.0, "queue"), (50, 1.0, "trade_through")]
    # risk_adverse: only prints shrink the queue (10 -> 1 -> -1): 1 lot at the second print
    assert fills(r, RA) == [(40, 1.0, "queue"), (50, 4.0, "trade_through")]
    # power3[n=1]: the cancel splits by size ahead (10 of 14): F = 10 - 3 x 10/14 = 7.86 -> 1 lot at the print
    assert fills(r, P3) == [(30, 1.0, "queue"), (40, 2.0, "queue"), (50, 2.0, "trade_through")]
    assert r.end == "filled" and r.end_ts == T + 50


def test_atomic_variants_place_a_cancel_where_one_order_fits(tmp_path: Path) -> None:
    """A decrease beyond the prints is one order: it was ahead only if it fits in front of the order, behind only if
    it fits behind; when both fit, the family splits it as usual."""
    assert Model.parse("power3[n=1;atomic]") == Model("power3", 1.0, True)
    assert Model("log", None, True).id == "log[atomic]"
    assert Model.parse("hbt_power[n=2;atomic=true;mode=corrected]").id == "power[n=2;atomic]"
    for bad in ["cancels_ahead[atomic]", "risk_adverse[atomic]", "power3[n=1;atomic=no]"]:
        with pytest.raises(QtError):
            Model.parse(bad)
    rows = l2_dataset(
        tmp_path,
        [
            resync(T, [(BID, 4900, 10), (ASK, 5100, 10)]),
            depth(T + 10, [(BID, 4900, 10, 14)]),  # live behind 10; 4 join behind it
            depth(T + 20, [(BID, 4900, 14, 8)]),  # 6 leave: only 4 are behind, so the order was ahead
            depth(T + 30, [(BID, 4900, 8, 20)]),  # 12 join behind
            depth(T + 40, [(BID, 4900, 20, 15)]),  # 5 leave: more than the 4 ahead, so they were behind
            depth(T + 50, [(BID, 4900, 15, 13)]),  # 2 leave: they fit on both sides, the family splits them
            depth(T + 60, [(ASK, 5100, 10, 9)]),
        ],
    )
    models = [Model("power3", 1.0), Model("power3", 1.0, True), Model("cancels_ahead")]
    o = SimOrder("o", BID, 4900, 1.0, ts_live=T + 1, probes=[3, 4, 5, 6, 7])
    (r,) = Engine(models).run(rows, [o])
    est = {seq: f for seq, f in r.probes}
    assert est[3][1] == pytest.approx(10.0)  # before the first cancel
    assert est[4][0] == pytest.approx(10 - 6 * 10 / 14) and est[4][1] == pytest.approx(4.0)  # plain splits 6 x 10/14
    assert est[5][1] == pytest.approx(4.0) and est[6][1] == pytest.approx(4.0)
    assert est[7][1] == pytest.approx(4.0 - 2 * 4 / 15)  # both fit: power3[n=1] takes the share ahead, 4 of 15
    assert est[7][2] == pytest.approx(0.0)  # cancels_ahead, the lower bound, is unchanged by the variant


def test_samples_probes_cancel_and_amend(queue_book) -> None:
    o = SimOrder("o", BID, 4900, 5.0, ts_live=T + 1, ts_end=T + 35, samples=[T + 5, T + 25, T + 99], probes=[3, 4])
    (r,) = Engine(MODELS).run(queue_book, [o])
    # T + 5 is after the order's entry: it sees the queue of 10 the order joined behind
    assert r.samples_before_live == 0 and [t - T for t, _ in r.samples] == [5, 25]
    assert r.samples[0][1][CA] == pytest.approx(10.0) and r.samples[1][1][CA] == pytest.approx(7.0)
    assert r.samples[1][1][RA] == pytest.approx(10.0)
    assert [(seq, est[CA], est[RA]) for seq, est in r.probes] == [(3, 10.0, 10.0), (4, 7.0, 10.0)]
    assert (r.end, r.end_ts) == ("cancelled", T + 35) and r.samples_after_end == 1
    assert fills(r, TT) == []  # cancelled before the trade through
    amended = SimOrder("a", BID, 4900, 5.0, ts_live=T + 1, amends=[(T + 25, 2.0)])
    (ra,) = Engine([Model("front_of_queue")]).run(queue_book, [amended])
    assert fills(ra, 0) == [(30, 2.0, "queue")]  # the priority-keeping reduction to 2 before the print


def test_entry_by_position_cross_at_live_post_only_and_taker_cap(tmp_path: Path) -> None:
    rows = l2_dataset(
        tmp_path,
        [
            resync(T, [(BID, 4900, 10), (ASK, 5100, 4), (ASK, 5200, 6)]),
            depth(T + 10, [(BID, 4800, 0, 1)]),
            depth(T + 20, [(ASK, 5100, 4, 3)]),
        ],
    )
    orders = [
        SimOrder("x", BID, 5150, 8.0, ts_live=T + 1),
        SimOrder("po", BID, 5150, 8.0, ts_live=T + 1, post_only=True),
        SimOrder("seq", BID, 4900, 1.0, entry_seq=2, inside=True),
    ]
    rx, rpo, rseq = Engine([Model("front_of_queue"), Model("trade_through_only")]).run(rows, orders)
    assert [(t - T, q, why, liq, px) for t, q, why, liq, px in rx.fills[0]] == [
        (1, 4.0, "crossed_at_live", "taker", 5100.0)  # the visible 4 at or through its price, nothing more
    ]
    assert rx.fills[1] == []  # trade_through_only never takes
    assert rpo.end == "rejected_post_only" and rpo.fills == [[], []]
    assert rseq.activated_seq == 3  # joined inside batch 2: live from the first batch after it


def test_clear_censors_but_a_restatement_does_not_and_gaps_stop_orders(tmp_path: Path) -> None:
    same = [(BID, 4900, 10), (ASK, 5100, 10)]
    rows = l2_dataset(
        tmp_path / "a",
        [resync(T, same), resync(T + 10, same), depth(T + 20, [(BID, 4900, 10, 1)], [trade(T + 20, 4900, 9, ASK)])],
    )
    (r,) = Engine([Model("cancels_ahead")]).run(rows, [SimOrder("o", BID, 4900, 2.0, ts_live=T + 1)])
    assert r.end == "open_at_end" and fills(r, 0) == []  # the identical restatement kept it live: F 10 - 9 = 1
    rows = l2_dataset(
        tmp_path / "b",
        [resync(T, same), depth(T + 5, [(BID, 4900, 10, 11)]), resync(T + 10, [(BID, 4900, 3)])],
    )
    (r,) = Engine([Model("cancels_ahead")]).run(rows, [SimOrder("o", BID, 4900, 2.0, ts_live=T + 1)])
    assert (r.activated_ts, r.end, r.end_ts) == (T + 5, "censored_clear", T + 10)  # a book that changed under it
    rows = l2_dataset(
        tmp_path / "c", [resync(T, same), depth(T + 3, [(BID, 4900, 10, 11)]), depth(T + 20, [(BID, 4900, 11, 12)])]
    )
    (r,) = Engine([Model("cancels_ahead")]).run(
        rows, [SimOrder("o", BID, 4900, 2.0, ts_live=T + 1)], gaps=[(T + 5, T + 9)]
    )
    assert (r.end, r.end_ts) == ("censored_gap", T + 20)  # stopped at the first batch after the gap opened
    empty = Engine([Model("cancels_ahead")]).run(read_l2(tmp_path / "a", 2), [SimOrder("o", BID, 1, 1.0, ts_live=0)])
    assert empty[0].end == "no_data"
    with pytest.raises(QtError, match="needs ts_live or entry_seq"):
        Engine([Model("log")]).run(rows, [SimOrder("o", BID, 4900, 1.0)])
    late = Engine([Model("log")]).run(rows, [SimOrder("o", BID, 4900, 1.0, ts_live=T + 99)])
    assert late[0].end == "after_data"


def _first_fill_times(res, k: int) -> list[float]:
    return [min((t for t, *_ in r.fills[k]), default=np.inf) for r in res]


@pytest.mark.parametrize("venue", ["kalshi", "polymarket"])
def test_bounds_order_first_fills_on_the_fixtures(venue: str, kalshi_run: Any, poly_run: Any) -> None:
    """front_of_queue <= cancels_ahead <= power3[n] <= risk_adverse <= trade_through_only, order by order."""
    run = kalshi_run if venue == "kalshi" else poly_run
    root = run.pm / "book_own_free"
    rows = read_l2(root, 1)
    lot = 100 if venue == "kalshi" else 1_000_000
    sims = []
    t0, t1 = int(rows.ts_event.min()), int(rows.ts_event.max())
    k = 0
    for t in range(t0 + 1_000_000_000, t1, 700_000_000):
        for side, px in ((BID, 4400), (ASK, 4600), (BID, 4500), (ASK, 4700)):
            k += 1
            sims.append(SimOrder(f"s{k}", side, px, 15.0 * lot, ts_live=t, ts_end=t + 9_000_000_000))
    order = [
        Model("front_of_queue"),
        Model("cancels_ahead"),
        *(Model("power3", n) for n in (0.2, 1.0, 5.0)),
        Model("risk_adverse"),
        Model("trade_through_only"),
    ]
    res = Engine(order, lot=float(lot)).run(rows, sims, gaps=feed_gaps(root).get(1, []))
    times = np.array([_first_fill_times(res, j) for j in range(len(order))])
    assert np.all(times[:-1] <= times[1:])  # never filled = inf
    filled = np.array([[sum(q for _t, q, *_ in r.fills[j]) for r in res] for j in range(len(order))])
    assert np.all(np.diff(filled, axis=0) <= 1e-9)  # filled quantity shrinks from the optimistic bound down
    assert np.isfinite(times[0]).sum() > np.isfinite(times[-1]).sum() > 0


@pytest.mark.requires_core
@pytest.mark.parametrize("venue", ["kalshi", "polymarket"])
def test_engine_reproduces_the_rust_zoo_corrected_mode(
    venue: str, kalshi_run: Any, poly_run: Any, tmp_path: Path
) -> None:
    """Same orders, same dataset: every fill of every model equals `queuetruth zoo --mode corrected`."""
    import pyarrow.parquet as pq

    from queuetruth import api

    run = kalshi_run if venue == "kalshi" else poly_run
    ds = run.pm / "book_own_free"
    rows = read_l2(ds, 1)
    qty = 1500 if venue == "kalshi" else 15_000_000
    shadows = []
    t0, t1 = int(rows.ts_event.min()), int(rows.ts_event.max())
    for t in range(t0 + 1_000_000_000, t1, 1_500_000_000):
        for side, px in (("B", 4400), ("A", 4600), ("B", 4300), ("A", 4700)):
            shadows.append(
                {
                    "shadow_id": f"s{len(shadows):03d}",
                    "instrument_id": 1,
                    "side": side,
                    "price": px,
                    "qty": qty,
                    "ts_submit": t,
                    "latency_ns": 0,
                    "ts_cancel": t + 8_000_000_000,
                }
            )
    with open(tmp_path / "shadows.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(shadows[0]))
        w.writeheader()
        w.writerows(shadows)
    models = [
        Model("front_of_queue"),
        Model("trade_through_only"),
        Model("risk_adverse"),
        Model("power3", 2.0),
        Model("power", 3.0),
        Model("power2", 0.5),
        Model("log"),
        Model("log2"),
    ]
    pairs = [(str(m.zoo_id), m) for m in models]  # the zoo takes the kit's zoo spelling as given
    api.run_zoo(
        {
            "dataset": {"path": str(ds)},
            "shadows": {"path": str(tmp_path / "shadows.csv")},
            "models": [z for z, _ in pairs],
            "mode": "corrected",
            "exchange": "partial",
            "write_fills": True,
            "horizons_ns": [1_000_000_000],
            "out": str(tmp_path / "zoo"),
        }
    )
    zoo: dict[tuple[str, str], list[tuple[int, float, str, str]]] = {}
    for f in pq.read_table(tmp_path / "zoo" / "zoo_fills.parquet").to_pylist():
        fam = f["model_id"].split("[")[0]
        zoo.setdefault((fam, f["shadow_id"]), []).append(
            (f["ts_event"].value, f["fill_qty"], f["reason"], f["liquidity"])
        )
    orders = [
        SimOrder(
            s["shadow_id"],
            BID if s["side"] == "B" else ASK,
            s["price"],
            float(s["qty"]),
            ts_live=s["ts_submit"],
            ts_end=s["ts_cancel"],
        )
        for s in shadows
    ]
    res = Engine([m for _, m in pairs]).run(rows, orders, gaps=feed_gaps(ds).get(1, []))
    n_fills = 0
    for j, (zid, _m) in enumerate(pairs):
        fam = zid.split("[")[0]
        for o, r in zip(orders, res, strict=True):
            mine = [(t, q, why, liq) for t, q, why, liq, _px in r.fills[j]]
            assert mine == zoo.get((fam, o.key), []), (zid, o.key)
            n_fills += len(mine)
    assert n_fills > 100
