"""Own-order removal from an L2-only tape (trader review E2, `queuetruth.score.l2_own`).

The hand-built tape has three own bid orders: A at 100 (5 lots: ack, a 3-lot fill, cancel of the remaining 2), C at
98 (4 lots, outside the 2-level view until 100 empties) and D at 99 (2 lots: ack, cancel). The feed reflects A's
ack 0.2 ms before the own ack time and its cancel 0.5 ms before; D's ack shows 5 ms late. The own-free copy must
equal the tape the other traders alone would have produced (`OTHERS`), row by row."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from queuetruth.errors import QtError
from queuetruth.schemas.io import write_table
from queuetruth.score._dataset import DatasetView
from queuetruth.score._io import make_table, read_live_log, rows
from queuetruth.score.l2_own import (
    SIDECAR,
    OwnFill,
    OwnOrder,
    own_free_l2_dataset,
    own_free_l2_from_live,
    own_orders_from_log,
    unit_scale,
)
from score.qt_score_helpers import DAY0, MIDNIGHT, MS, T0, TICK, S, log_from, make_dataset, order_events

US = 1_000
B, A, N = ord("B"), ord("A"), ord("N")
LAST, OOV, INTO, SNAP = 1, 2, 4, 8
P98, P99, P100, P101, P102 = (k * TICK for k in (998, 999, 1000, 1001, 1002))
L2_PATH = "l2/date=2026-01-01/instrument_id=1/part-00000.parquet"
CMP = ("ts_event", "kind", "side", "price", "prev_qty", "new_qty", "prev_ct", "new_ct", "trade_qty", "flags")


def dep(t: int, side: int, px: int, prev: int, new: int, pct: int, nct: int, level: int = 0, flags: int = 0) -> dict:
    return {"ts": t, "kind": "depth", "side": side, "price": px, "prev": prev, "new": new, "pct": pct, "nct": nct,
            "level": level, "flags": flags}  # fmt: skip


def snap(t: int, side: int, px: int, new: int, ct: int, level: int) -> dict:
    return {**dep(t, side, px, 0, new, 0, ct, level, SNAP), "kind": "snapshot"}


def clear(t: int) -> dict:
    return {**dep(t, N, 0, 0, 0, 0, 0, 0, SNAP), "kind": "clear", "price": None, "pct": None, "nct": None}


def trd(t: int, px: int, qty: int) -> dict:
    return {**dep(t, B, px, 0, 0, 0, 0, 0, 0), "kind": "trade", "trade": qty, "pct": None, "nct": None}


def batches_with_own() -> list[list[dict]]:
    t = T0
    return [
        [clear(t), snap(t, B, P100, 10, 2, 0), snap(t, B, P99, 5, 1, 1), snap(t, A, P101, 8, 1, 0),
         snap(t, A, P102, 6, 1, 1)],
        [dep(t + S, B, P100, 10, 13, 2, 3)],
        [dep(t + 2 * S - 200 * US, B, P100, 13, 18, 3, 4)],  # A ack (own time t + 2 s)
        [dep(t + 5 * S // 2 + 5 * MS, B, P99, 5, 7, 1, 2, 1)],  # D ack (own time t + 2.5 s), 5 ms late
        [dep(t + 3 * S, B, P100, 18, 20, 4, 5)],
        [trd(t + 4 * S, P100, 12), dep(t + 4 * S, B, P100, 20, 8, 5, 3)],
        [trd(t + 5 * S, P100, 1), trd(t + 5 * S, P100, 3), dep(t + 5 * S, B, P100, 8, 4, 3, 2)],  # A fills 3
        [dep(t + 6 * S - 500 * US, B, P100, 4, 2, 2, 1)],  # A cancel (own time t + 6 s)
        [dep(t + 7 * S, B, P100, 2, 0, 1, 0, 255), dep(t + 7 * S, B, P98, 0, 7, 0, 2, 1, INTO)],
        [dep(t + 8 * S, B, P100, 0, 1, 0, 1, 0), dep(t + 8 * S, B, P99, 7, 5, 2, 1, 1)],  # D cancel at t + 8 s
    ]  # fmt: skip


def batches_others(keep_prints: bool = True) -> list[list[dict]]:
    """The tape without the own orders (C's 3 other lots at 98 are out of view until 100 empties)."""
    t = T0
    b6 = [trd(t + 5 * S, P100, 1)] + ([trd(t + 5 * S, P100, 3)] if keep_prints else [])
    return [
        [clear(t), snap(t, B, P100, 10, 2, 0), snap(t, B, P99, 5, 1, 1), snap(t, A, P101, 8, 1, 0),
         snap(t, A, P102, 6, 1, 1)],
        [dep(t + S, B, P100, 10, 13, 2, 3)],
        [dep(t + 3 * S, B, P100, 13, 15, 3, 4)],
        [trd(t + 4 * S, P100, 12), dep(t + 4 * S, B, P100, 15, 3, 4, 2)],
        [*b6, dep(t + 5 * S, B, P100, 3, 2, 2, 1)],
        [dep(t + 7 * S, B, P100, 2, 0, 1, 0, 255), dep(t + 7 * S, B, P98, 0, 3, 0, 1, 1, INTO)],
        [dep(t + 8 * S, B, P100, 0, 1, 0, 1, 0)],
    ]  # fmt: skip


OWN = [
    OwnOrder(1, "bid", P100, ((T0 + 2 * S, 5), (T0 + 5 * S, 2), (T0 + 6 * S, 0)), (OwnFill(T0 + 5 * S, 3, P100),), "A"),
    OwnOrder(1, "bid", P98, ((T0 + 3 * S // 2, 4),), (), "C"),
    OwnOrder(1, "bid", P99, ((T0 + 5 * S // 2, 2), (T0 + 8 * S, 0)), (), "D"),
]


def l2_rows(batches: list[list[dict]]) -> list[dict[str, Any]]:
    out = []
    for eid, b in enumerate(batches, start=1):
        for i, r in enumerate(b):
            out.append(
                {
                    "ts_event": r["ts"],
                    "ts_recv": r["ts"] + MS,
                    "instrument_id": 1,
                    "event_id": eid,
                    "sequence": eid,
                    "kind": r["kind"],
                    "side": r["side"],
                    "price": r["price"],
                    "prev_qty": r["prev"],
                    "new_qty": r["new"],
                    "prev_ct": r["pct"],
                    "new_ct": r["nct"],
                    "trade_qty": r.get("trade", 0),
                    "aggressor": A if r["kind"] == "trade" else N,
                    "level": r["level"],
                    "flags": r["flags"] | (LAST if i == len(b) - 1 else 0),
                }
            )
    return out


def make_l2_dataset(root: Path, batches: list[list[dict]]) -> Path:
    """L2-only dataset (venue generic, tick 0.01, lot 1) with one partition."""
    root.mkdir(parents=True, exist_ok=True)
    inst = {
        "instrument_id": 1,
        "session_date": DAY0,
        "symbol": "SYM",
        "venue": "generic",
        "source_venue": "test",
        "price_exp": -9,
        "tick_size": TICK,
        "lot_size": 1,
        "size_exp": 0,
        "contract_multiplier": 1.0,
        "currency": "USD",
        "session_tz": "UTC",
        "midnight_utc_ns": MIDNIGHT,
        "open_s": 0,
        "close_s": 86_400,
        "source": "test",
    }
    write_table(make_table("qt.instruments", [inst]), root / "instruments.parquet")
    rs = l2_rows(batches)
    fe = write_table(make_table("qt.l2", rs), root / L2_PATH)
    part = {
        "instrument_id": 1,
        "session_date": DAY0,
        "files": [{"path": L2_PATH, "rows": fe["rows"], "sha256": fe["sha256"]}],
        "rows": fe["rows"],
        "ts_min": rs[0]["ts_event"],
        "ts_max": rs[-1]["ts_event"],
    }
    man = {
        "schema": "qt.dataset_manifest",
        "schema_version": 1,
        "dataset_id": "1" * 64,
        "kind": "l2",
        "source_format": "tardis_l2",
        "venue": "generic",
        "source_venue": "test",
        "inputs": [],
        "partitions": [part],
        "price_exp_by_instrument": {"1": -9},
        "created_by": "test",
        "validation_status": "synthetic_only",
    }
    (root / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    return root


def read_l2(root: Path) -> list[dict[str, Any]]:
    return rows(pq.read_table(root / L2_PATH))


def comparable(rs: list[dict[str, Any]]) -> list[tuple]:
    return [tuple(r[c] for c in CMP) for r in rs]


def assert_batches_valid(rs: list[dict[str, Any]]) -> None:
    """Event ids non-decreasing; exactly one LAST_IN_BATCH per event id, on its last row."""
    ids = [r["event_id"] for r in rs]
    assert ids == sorted(ids)
    for i, r in enumerate(rs):
        is_last = i == len(rs) - 1 or rs[i + 1]["event_id"] != r["event_id"]
        assert bool(r["flags"] & LAST) == is_last, (i, r)


@pytest.fixture
def tape(tmp_path: Path) -> Path:
    return make_l2_dataset(tmp_path / "with_own", batches_with_own())


def test_own_free_copy_equals_the_tape_without_own_orders(tape: Path, tmp_path: Path) -> None:
    res = own_free_l2_dataset(tape, tmp_path / "free", OWN, match_window_ns=10 * MS)
    got = read_l2(res.root)
    assert comparable(got) == comparable(l2_rows(batches_others()))
    assert_batches_valid(got)
    st = res.stats
    assert (st.orders, st.orders_matched) == (3, 3)
    assert (st.changes, st.changes_aligned, st.changes_unaligned, st.changes_unaligned_not_visible) == (6, 5, 1, 1)
    assert (st.depth_rows_dropped, st.flags_moved, st.clamps, st.empty_while_resting) == (4, 1, 0, 0)
    assert (st.fills, st.fills_matched, st.fills_unaligned) == (1, 1, 0)
    assert (st.trade_rows_reduced, st.trade_rows_dropped) == (0, 0)  # default: prints kept for the twin
    assert st.rows_adjusted == 8 and st.overlapping_orders == 0  # b3, b3b, b4, b5, b6, b7, b9 at 99, 98
    # the copy is a valid dataset: new id, row counts, source end of data, sidecar
    view = DatasetView(res.root)
    assert view.kind == "l2" and view.end_of_data(1, DAY0) == T0 + 8 * S
    man = json.loads((res.root / "manifest.json").read_text())
    assert man["dataset_id"] == res.dataset_id != "1" * 64
    assert man["partitions"][0]["rows"] == len(got) == man["partitions"][0]["files"][0]["rows"]
    side = json.loads((res.root / SIDECAR).read_text())
    assert side["source_dataset_id"] == "1" * 64 and side["stats"]["depth_rows_dropped"] == 4
    assert (res.root / "instruments.parquet").exists()
    assert any("1 of 6 own resting-quantity change(s) matched no feed row" in w for w in res.warnings)


def test_default_window_leaves_late_feed_row_unaligned(tape: Path, tmp_path: Path) -> None:
    res = own_free_l2_dataset(tape, tmp_path / "free", OWN)  # 1 ms: D's ack row is 5 ms late
    st = res.stats
    assert (st.changes_aligned, st.changes_unaligned, st.changes_unaligned_not_visible) == (4, 2, 1)
    got = read_l2(res.root)
    d_ack = [r for r in got if r["price"] == P99 and r["ts_event"] == T0 + 5 * S // 2 + 5 * MS]
    assert len(d_ack) == 1 and (d_ack[0]["prev_qty"], d_ack[0]["new_qty"]) == (3, 5)  # D applied at its own time
    # A's ack (0.2 ms early) and cancel (0.5 ms early) are aligned inside the default window
    assert not [
        r for r in got if r["price"] == P100 and r["ts_event"] in (T0 + 2 * S - 200 * US, T0 + 6 * S - 500 * US)
    ]
    assert_batches_valid(got)
    assert any(
        "2 of 6 own resting-quantity change(s)" in w and "(1 where the level was not visible)" in w
        for w in res.warnings
    )


def test_remove_mode_subtracts_own_fills_from_prints(tape: Path, tmp_path: Path) -> None:
    res = own_free_l2_dataset(tape, tmp_path / "free", OWN, match_window_ns=10 * MS, own_fill_prints="remove")
    got = read_l2(res.root)
    assert comparable(got) == comparable(l2_rows(batches_others(keep_prints=False)))
    assert (res.stats.trade_rows_dropped, res.stats.trade_rows_reduced, res.stats.fills_matched) == (1, 0, 1)
    assert_batches_valid(got)
    # a 2-lot own fill with no exact print: nearest prints first (1 lot, then 1 of the 3-lot print)
    own2 = [OwnOrder(1, "B", P100, ((T0 + 2 * S, 5), (T0 + 5 * S, 3)), (OwnFill(T0 + 5 * S, 2, P100),), "A")]
    res2 = own_free_l2_dataset(tape, tmp_path / "free2", own2, own_fill_prints="remove")
    prints = [r["trade_qty"] for r in read_l2(res2.root) if r["kind"] == "trade" and r["ts_event"] == T0 + 5 * S]
    assert prints == [2] and (res2.stats.trade_rows_dropped, res2.stats.trade_rows_reduced) == (1, 1)


def test_unmatched_fill_and_clamp_are_counted(tape: Path, tmp_path: Path) -> None:
    # the fill (and its resting step) logged 3 ms after the print, and never cancelled: the step lands after the
    # 8 -> 4 row, and the 2 lots still resting meet an emptied level later; both show up in the diagnostics
    late = [
        OwnOrder(1, "bid", P100, ((T0 + 2 * S, 5), (T0 + 5 * S + 3 * MS, 2)), (OwnFill(T0 + 5 * S + 3 * MS, 3, P100),)),
        OwnOrder(1, "ask", P102, ((T0 + S, 2),), ()),  # no feed row after the ack: unaligned, nothing adjusted
    ]
    res = own_free_l2_dataset(tape, tmp_path / "free", late)
    st = res.stats
    assert (st.fills_unaligned, st.fill_qty_unmatched) == (1, 3)
    assert (st.changes, st.changes_aligned, st.changes_unaligned, st.changes_unaligned_not_visible) == (3, 1, 2, 0)
    assert st.clamps == 2 and st.empty_while_resting == 2
    for text in ("matched no trade print", "clamped at 0", "show an empty level while an own order rests"):
        assert any(text in w for w in res.warnings), text
    assert [r["new_qty"] for r in read_l2(res.root) if r["price"] == P102] == [6]
    big = [OwnOrder(1, "bid", P100, ((T0 + 2 * S - 200 * US, 30),), ())]
    res2 = own_free_l2_dataset(tape, tmp_path / "free2", big)
    assert res2.stats.clamps > 0 and any("clamped at 0" in w for w in res2.warnings)
    assert all(r["prev_qty"] >= 0 and r["new_qty"] >= 0 for r in read_l2(res2.root))


def test_overlapping_own_orders_are_flagged(tape: Path, tmp_path: Path) -> None:
    second = OwnOrder(1, "bid", P100, ((T0 + 3 * S, 2), (T0 + 7 * S, 0)), (), "A2")
    res = own_free_l2_dataset(tape, tmp_path / "free", [*OWN, second], match_window_ns=10 * MS)
    assert res.stats.overlapping_orders == 1
    assert any("joined a level where another own order was resting" in w for w in res.warnings)


def test_builder_from_live_log_matches_records(tape: Path, tmp_path: Path) -> None:
    sc = 10  # live prices carry one decimal less (price_exp -8) than the dataset (-9)
    live = log_from(
        [
            order_events("A", "buy", P100 // sc, 5, T0 + 2 * S, fills=[(T0 + 5 * S, 3)], cancel_at=T0 + 6 * S),
            order_events("C", "buy", P98 // sc, 4, T0 + 3 * S // 2),
            order_events("D", "buy", P99 // sc, 2, T0 + 5 * S // 2, cancel_at=T0 + 8 * S),
            order_events("I", "buy", P100 // sc, 1, T0 + 3 * S, tif="ioc"),
            order_events("E", "sell", P101 // sc, 3, T0 + S, fills=[(T0 + S, 1)], liquidity="taker", instrument_id=2),
        ],
        tmp_path / "live",
    )
    orders, events = read_live_log(live)
    own, warn = own_orders_from_log(orders, events, price_scale=unit_scale(-8, -9))
    by = {o.key: o for o in own}
    assert set(by) == {"A", "C", "D", "E"}
    for o in OWN:
        assert by[o.key] == o
    assert by["E"].resting == ((T0 + S, 2),) and by["E"].fills == ()  # entry taker fill folded into the start
    assert any("skipped 1 order(s) IOC/FOK" in w for w in warn)
    assert any("1 taker fill(s) at entry" in w for w in warn)
    res = own_free_l2_from_live(tape, live, tmp_path / "free", price_scale=sc, match_window_ns=10 * MS)
    ref = own_free_l2_dataset(tape, tmp_path / "ref", OWN, match_window_ns=10 * MS)
    assert res.dataset_id == ref.dataset_id
    assert any("instrument(s) 2 have no partition" in w for w in res.warnings)
    assert any("IOC/FOK" in w for w in res.warnings)


def test_amend_to_new_price_splits_records(tmp_path: Path) -> None:
    o, ev = order_events("M", "buy", P99, 4, T0 + S, fills=[(T0 + 2 * S, 1)])
    ev = [*ev, {**ev[-1], "event_type": "amend_ack", "ts_exchange": T0 + 3 * S, "qty": 3, "px": P100,
                "fill_id": None, "event_seq": len(ev) + 1}]  # fmt: skip
    ev.append({**ev[-1], "event_type": "cancel_ack", "qty": None, "px": None, "ts_exchange": T0 + 4 * S,
               "event_seq": len(ev) + 1})  # fmt: skip
    live = log_from([(o, ev)], tmp_path / "live")
    own, _ = own_orders_from_log(*read_live_log(live))
    assert [(x.key, x.price, x.resting) for x in own] == [
        ("M#leg0", P99, ((T0 + S, 4), (T0 + 2 * S, 3), (T0 + 3 * S, 0))),
        ("M#leg1", P100, ((T0 + 3 * S, 2), (T0 + 4 * S, 0))),
    ]
    assert own[0].fills == (OwnFill(T0 + 2 * S, 1, P99),)


def test_config_and_unit_errors(tape: Path, tmp_path: Path) -> None:
    mbo = make_dataset(tmp_path / "mbo")
    with pytest.raises(QtError) as e:
        own_free_l2_dataset(mbo, tmp_path / "x", OWN)
    assert e.value.code == "QT-CONFIG-001" and "L2-only" in e.value.message
    for kw in ({"match_window_ns": -1}, {"own_fill_prints": "drop"}):
        with pytest.raises(QtError) as e:
            own_free_l2_dataset(tape, tmp_path / "y", OWN, **kw)
        assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        own_free_l2_dataset(tape, tape, OWN)
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        OwnOrder(1, "up", P100, ((T0, 1),))
    assert e.value.code == "QT-CONFIG-001"
    live = log_from([order_events("A", "buy", 1005, 5, T0 + S)], tmp_path / "live")
    orders, events = read_live_log(live)
    with pytest.raises(QtError) as e:
        own_orders_from_log(orders, events, price_scale=1e8)  # type: ignore[arg-type]
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        own_orders_from_log(orders, events, price_scale=Fraction(1, 10))
    assert e.value.code == "QT-INPUT-004"
    assert unit_scale(0, -8) == 100_000_000 and unit_scale(-8, -9) == 10 and unit_scale(-9, -8) == Fraction(1, 10)


@pytest.mark.integration
@pytest.mark.requires_core
def test_zoo_twin_sees_queue_without_its_own_size(tape: Path, tmp_path: Path) -> None:
    from queuetruth import api

    shadows = tmp_path / "shadows.parquet"
    spec_row = {
        "shadow_id": "A",
        "instrument_id": 1,
        "side": B,
        "price_mode": "abs",
        "price": P100,
        "offset_ticks": 0,
        "qty": 5,
        "ts_submit": T0 + 2 * S,
        "latency_ns": 0,
        "ts_cancel": T0 + 6 * S,
        "source": "live_log",
        "order_key": "A",
    }
    write_table(make_table("qt.shadow_spec", [spec_row]), shadows)
    keep = own_free_l2_dataset(tape, tmp_path / "keep", OWN).root
    remove = own_free_l2_dataset(tape, tmp_path / "remove", OWN, own_fill_prints="remove").root
    models = ["nt_queue_l2", "hbt_power3[n=2]"]

    def zoo(ds: Path, name: str) -> dict[str, dict[str, Any]]:
        s = api.run_zoo(
            {
                "dataset": {"path": str(ds)},
                "shadows": {"path": str(shadows)},
                "out": str(tmp_path / name),
                "models": models,
                "horizons_ns": [S, 3 * S],
                "h_max_ns": 3 * S,
                "bootstrap_b": 10,
            }
        )
        assert len(s.leaderboard) == len(models)
        preds = rows(pq.read_table(tmp_path / name / "zoo_predictions.parquet"))
        return {r["model_id"].split("[")[0]: r for r in preds if r["horizon_ns"] == 3 * S}

    full, free, gone = zoo(tape, "z_full"), zoo(keep, "z_keep"), zoo(remove, "z_remove")
    for fam in ("nt_queue_l2", "hbt_power3"):
        assert full[fam]["visible_level_qty_at_live"] == 18  # 13 ahead + the live order's own 5
        assert free[fam]["visible_level_qty_at_live"] == 18 - 5
        assert gone[fam]["visible_level_qty_at_live"] == 13
        # live filled 3 lots at t + 5 s: only the own-free tape with the prints kept lets the twin fill like that;
        # on the raw tape its own 5 lots sit ahead of it, and without the prints nothing reaches it
        assert full[fam]["p_fill"] == 0.0 and gone[fam]["p_fill"] == 0.0
        assert (free[fam]["p_fill"], free[fam]["e_fill_qty"]) == (1.0, 3.0)
        assert free[fam]["model_ts_first_fill"] == T0 + 5 * S
