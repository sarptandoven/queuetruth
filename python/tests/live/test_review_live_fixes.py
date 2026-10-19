"""Ports of the Python analytics review reproducers for the live package
(findings L1, L2, L3, L6/F10, L11, L13, L14, L15 and F11/L12; synthetic data only)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from live._fixgen import FixSession, at, instrument_map, log_name, render_log, write
from live._helpers import FIXTURES

from queuetruth.errors import QtError
from queuetruth.live import (
    IMPORTERS,
    ImportOptions,
    build_log,
    build_schedule,
    check_tables,
    import_quickfix,
    import_sim,
    import_venue,
    read_log,
    table_rows,
    to_schedule,
    write_canonical_log,
)
from queuetruth.live.schedule import schedule_report

MAP = "venue,instrument,instrument_id,price_exp,size_exp,tick_size,lot_size\nbinance,BTCUSDT,1,-9,-8,0.01,0.00001\n"


def test_l1_ibkr_tz_from_the_spec_equals_the_keyword(tmp_path: Path) -> None:
    inp = next((FIXTURES / "ibkr_flex" / "01_flex_xml_executions").glob("*.xml"))
    spec = {"venue": "ibkr_flex", "inputs": [{"path": str(inp)}]}
    import_venue({**spec, "out": str(tmp_path / "spec"), "tz": "America/New_York"})
    import_venue({**spec, "out": str(tmp_path / "kw")}, tz="America/New_York")
    import_venue({**spec, "out": str(tmp_path / "override"), "tz": "UTC"}, tz="America/New_York")
    fills = [
        next(e["ts_exchange"] for e in table_rows(read_log(tmp_path / n)[1]) if e["event_type"] == "fill")
        for n in ("spec", "kw", "override")
    ]
    assert fills[0] == fills[1] == fills[2] == 1792071001000000000  # 10:10:01 New York = 14:10:01 UTC


def _hbt_row(oid: int, key: str, leaves: str, status: int, ex: int, px: int = 10000) -> dict[str, Any]:
    return {
        "order_id": oid,
        "side": 1,
        "price_tick": px,
        "tick_size": "0.01",
        "qty": "5",
        "leaves_qty": leaves,
        "exec_qty": "0",
        "exec_price_tick": px,
        "status": status,
        "exch_timestamp": ex,
        "local_timestamp": ex - 100,
        "maker": True,
        "order_type": 0,
        "time_in_force": 0,
        "order_key": key,
    }


def test_l2_simulator_legs_of_one_order_become_one_order(tmp_path: Path) -> None:
    k = "binance:acct1:5000001"
    rows = [
        _hbt_row(1, k, "5", 1, 1792071000000000100),
        _hbt_row(1, k, "5", 4, 1792071001000000000),
        _hbt_row(2, k + ".leg2", "5", 1, 1792071001000000100, 10001),
        _hbt_row(2, k + ".leg2", "3", 5, 1792071002000000000, 10001),
    ]
    p = tmp_path / "hbt.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    with pytest.warns(UserWarning, match="unmapped_instruments"):
        s = import_sim("hftbacktest", [str(p)], tmp_path / "out")
    assert s.n_orders == 1 and s.validation_status == "simulated"
    o, e = (table_rows(t) for t in read_log(tmp_path / "out"))
    assert [r["order_key"] for r in o] == [k]
    u = 10**8  # default size_exp -8
    assert [(r["event_type"], r["qty"], r["px"], r["cum_qty"], r["leaves_qty"]) for r in e] == [
        ("submit_sent", 5 * u, 100000000000, None, None),
        ("ack", 5 * u, 100000000000, 0, 5 * u),
        ("amend_sent", 5 * u, 100010000000, None, None),
        ("amend_ack", 5 * u, 100010000000, 0, 5 * u),
        ("fill", 2 * u, 100010000000, 2 * u, 3 * u),
    ]
    assert any("merged from 2 schedule legs" in w for w in s.warnings)
    q = tmp_path / "fills.csv"
    with q.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "client_order_id",
                "trade_id",
                "order_side",
                "last_qty",
                "last_px",
                "liquidity_side",
                "commission",
                "ts_event",
                "instrument_id",
            ]
        )
        w.writerow([k, "T1", "BUY", "1", "100.00", "MAKER", "0.01 USDT", 1792071000500000000, "BTCUSDT.BINANCE"])
        w.writerow(
            [k + ".leg2", "T2", "BUY", "2", "100.01", "MAKER", "0.01 USDT", 1792071001500000000, "BTCUSDT.BINANCE"]
        )
    with pytest.warns(UserWarning, match="unmapped_instruments"):
        s2 = import_sim("nautilus", [str(q)], tmp_path / "out2")
    e2 = table_rows(read_log(tmp_path / "out2")[1])
    assert s2.n_orders == 1 and [(r["event_type"], r["qty"], r["cum_qty"]) for r in e2] == [
        ("fill", 1 * u, 1 * u),
        ("fill", 2 * u, 3 * u),
    ]


def test_l3_fix_order_without_time_in_force_is_day_and_scheduled(tmp_path: Path) -> None:
    s = FixSession()
    s.out(
        at(0),
        "D",
        [("11", "C1"), ("1", "A"), ("55", "ACME"), ("54", "1"), ("38", "100"), ("40", "2"), ("44", "101.25")],
    )
    s.inn(
        at(0, 600),
        "8",
        [
            ("37", "O1"),
            ("11", "C1"),
            ("17", "E0"),
            ("150", "0"),
            ("39", "0"),
            ("1", "A"),
            ("55", "ACME"),
            ("54", "1"),
            ("38", "100"),
            ("40", "2"),
            ("44", "101.25"),
            ("151", "100"),
            ("14", "0"),
            ("60", "20261015-13:30:00.000412"),
        ],
    )
    log = write(tmp_path / log_name(s, "cpp_ns"), render_log(s.records, "cpp_ns"))
    import_quickfix(log, tmp_path / "out", instrument_map=instrument_map(tmp_path / "im.csv"))
    assert table_rows(read_log(tmp_path / "out")[0])[0]["tif"] == "day"
    sc = build_schedule(tmp_path / "out")
    assert (sc.n_scheduled, sc.excluded) == (1, {})


def _bn(x: str, i: int, c: str, side: str, ts: int, **kw: Any) -> dict[str, Any]:
    m = {
        "e": "executionReport",
        "E": ts + 1,
        "s": "BTCUSDT",
        "c": c,
        "S": side,
        "o": "LIMIT",
        "f": "GTC",
        "q": "1.0",
        "p": "100.00",
        "x": x,
        "X": "NEW",
        "i": i,
        "l": "0",
        "z": "0",
        "L": "0",
        "n": "0",
        "N": None,
        "T": ts,
        "t": -1,
        "m": False,
    }
    m.update(kw)
    return m


def test_l6_self_trade_keeps_both_sides_fill(tmp_path: Path) -> None:
    msgs = [
        _bn("NEW", 1, "a", "SELL", 1792071000000),
        _bn("NEW", 2, "b", "BUY", 1792071001000),
        _bn("TRADE", 1, "a", "SELL", 1792071001000, l="1.0", z="1.0", L="100.00", t=555, m=True, X="FILLED"),
        _bn("TRADE", 2, "b", "BUY", 1792071001000, l="1.0", z="1.0", L="100.00", t=555, m=False, X="FILLED"),
    ]
    p = tmp_path / "ws.jsonl"
    p.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")
    r = IMPORTERS["binance"]([str(p)], ImportOptions(account="a"))
    assert (r.n_fills, r.dedupe_dropped) == (2, 0)
    o, e = build_log(r.orders, r.events)
    assert check_tables(o, e)[0] == []
    # a true duplicate (same side, same trade id) still collapses
    p2 = tmp_path / "dup.jsonl"
    p2.write_text("\n".join(json.dumps(m) for m in [*msgs[:3], msgs[2]]) + "\n")
    r2 = IMPORTERS["binance"]([str(p2)], ImportOptions(account="a"))
    assert r2.n_fills == 1 and r2.dedupe_dropped >= 1


def test_l11_summary_entry_fallback_share_matches_the_schedule(tmp_path: Path) -> None:
    b = FIXTURES / "binance"
    (tmp_path / "im.csv").write_text(MAP)
    ins = sorted((b / "01_ws_execution_report_lifecycle").glob("*.jsonl")) + sorted(
        (b / "05_ws_api_post_only_reject").glob("*.jsonl")
    )
    s = import_venue(
        {
            "venue": "binance",
            "inputs": [{"path": str(p)} for p in ins],
            "out": str(tmp_path / "out"),
            "instrument_map": {"path": str(tmp_path / "im.csv")},
        }
    )
    sc = build_schedule(tmp_path / "out")
    assert s.entry_fallback_share == sc.entry_fallback_share == 0.0  # the rejected order is not a fallback
    assert not any("entry_fallback_share" in w for w in s.warnings)  # R-B5: no warning at share 0


def test_entry_fallback_one_warning_text_in_import_and_schedule(tmp_path: Path) -> None:
    """Open conflict 11 (E17, E23): the import summary and `to_schedule` write the fallback in one format, next to
    the numeric 1.1 `entry_fallback_share`."""
    from queuetruth.live.canonical import entry_fallback_warning

    assert entry_fallback_warning(0, 5) is None and entry_fallback_warning(0, 0) is None
    assert entry_fallback_warning(1, 2) == (
        "entry_fallback_share=1/2: orders without a match/gateway-time ack scheduled with send_plus_latency"
    )
    t0 = 1_800_000_000_000_000_000
    order = {
        "order_key": "v:a:K",
        "venue": "v",
        "account": "a",
        "instrument": "X",
        "instrument_id": 7,
        "side": "buy",
        "order_type": "limit",
        "tif": "gtc",
        "orig_qty": 10,
        "qty_str": "10",
        "limit_px": 100,
        "px_str": "100",
        "price_exp": 0,
        "size_exp": 0,
    }
    base = {"order_key": "v:a:K", "ts_precision": "ns", "source": "synthetic", "derived": False}
    evs = [
        {**base, "event_type": "submit_sent", "ts_local_send": t0 - 1, "ts_exchange_kind": "unknown", "qty": 10},
        {**base, "event_type": "ack", "ts_local_recv": t0 + 5, "ts_exchange_kind": "unknown", "qty": 10},
    ]
    write_canonical_log(tmp_path / "live", [order], evs)
    rep = schedule_report(build_schedule(tmp_path / "live"))
    assert rep["entry_fallback_share"] == 1.0
    assert [w for w in rep["warnings"] if "entry_fallback_share" in w] == [entry_fallback_warning(1, 1)]


def test_l13_simulator_venues_route_and_reject_unhonoured_fields(tmp_path: Path) -> None:
    p = tmp_path / "hbt.jsonl"
    p.write_text(json.dumps(_hbt_row(1, "binance:a:1", "5", 1, 1792071000000000100)) + "\n")
    spec = {"venue": "sim_hftbacktest", "inputs": [{"path": str(p)}], "out": str(tmp_path / "o")}
    with pytest.warns(UserWarning, match="unmapped_instruments"):
        s = import_venue(spec)
    assert s.venue == "sim:hftbacktest" and s.validation_status == "simulated"
    assert {e["source"] for e in table_rows(read_log(tmp_path / "o")[1])} == {"sim"}
    for extra in ({"tz": "UTC"}, {"fix": {"venue_name": "cme"}}):
        with pytest.raises(QtError) as ei:
            import_venue({**spec, **extra})
        assert ei.value.code == "QT-CONFIG-001"
    for extra in ({"tz": "UTC"}, {"fix": {"delimiter": "|"}}):
        with pytest.raises(QtError) as ei:
            import_venue({"venue": "binance", "inputs": [{"path": str(p)}], "out": str(tmp_path / "b"), **extra})
        assert ei.value.code == "QT-CONFIG-001"


def test_l14_cancel_from_another_capture_orders_on_the_local_clock(tmp_path: Path) -> None:
    skew = 20_000_000  # local clock 20 ms ahead of the venue
    t0 = 1792071300000

    def loc(ms: int) -> int:
        return ms * 1_000_000 + skew

    api = [
        {
            "dir": "out",
            "local_ns": loc(t0),
            "msg": {
                "id": "r1",
                "method": "order.place",
                "params": {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "price": "60000.00",
                    "quantity": "0.01",
                    "newClientOrderId": "c1",
                },
            },
        },
        {
            "dir": "in",
            "local_ns": loc(t0 + 3),
            "msg": {"id": "r1", "status": 200, "result": {"orderId": 42, "clientOrderId": "c1"}},
        },
        {
            "dir": "out",
            "local_ns": loc(t0 + 5000),
            "msg": {"id": "r2", "method": "order.cancel", "params": {"symbol": "BTCUSDT", "orderId": 42}},
        },
        {"dir": "in", "local_ns": loc(t0 + 5003), "msg": {"id": "r2", "status": 200, "result": {"orderId": 42}}},
    ]
    er = {
        "e": "executionReport",
        "s": "BTCUSDT",
        "c": "c1",
        "S": "BUY",
        "o": "LIMIT",
        "f": "GTC",
        "q": "0.01",
        "p": "60000.00",
        "i": 42,
        "l": "0",
        "z": "0",
        "L": "0",
        "n": "0",
        "t": -1,
        "m": False,
    }
    uds = [
        {"recv_ns": loc(t0 + 2), "msg": {**er, "x": "NEW", "X": "NEW", "E": t0 + 2, "T": t0 + 1}},
        {
            "recv_ns": loc(t0 + 5003),
            "msg": {**er, "x": "CANCELED", "X": "CANCELED", "E": t0 + 5003, "T": t0 + 5002, "C": "c1", "c": "cx"},
        },
    ]
    (tmp_path / "api.jsonl").write_text("\n".join(json.dumps(x) for x in api) + "\n")
    (tmp_path / "uds.jsonl").write_text("\n".join(json.dumps(x) for x in uds) + "\n")
    (tmp_path / "im.csv").write_text(MAP)
    out = tmp_path / "out"
    import_venue(
        {
            "venue": "binance",
            "inputs": [{"path": str(tmp_path / f)} for f in ("api.jsonl", "uds.jsonl")],
            "out": str(out),
            "instrument_map": {"path": str(tmp_path / "im.csv")},
        }
    )
    types = [r["event_type"] for r in table_rows(read_log(out)[1])]
    assert types.index("cancel_sent") < types.index("cancel_ack")
    sc = build_schedule(out, entry="send_plus_latency", latency_ns=None)
    assert sc.spec.to_pylist()[0]["ts_cancel"].value == loc(t0 + 5000)


def test_f11_reprice_to_the_live_filled_qty_keeps_the_leg_and_l15_report(tmp_path: Path) -> None:
    px = 100
    order = {
        "order_key": "v:a:K",
        "venue": "v",
        "account": "a",
        "instrument": "X",
        "instrument_id": 7,
        "side": "buy",
        "order_type": "limit",
        "tif": "gtc",
        "orig_qty": 10,
        "qty_str": "10",
        "limit_px": px,
        "px_str": "100",
        "price_exp": 0,
        "size_exp": 0,
    }
    t0 = 1_800_000_000_000_000_000
    base = {"order_key": "v:a:K", "ts_precision": "ns", "source": "synthetic", "derived": False}
    evs = [
        {**base, "event_type": "submit_sent", "ts_local_send": t0 - 1, "ts_exchange_kind": "unknown", "qty": 10},
        {
            **base,
            "event_type": "ack",
            "ts_exchange": t0,
            "ts_exchange_kind": "match_time",
            "qty": 10,
            "cum_qty": 0,
            "leaves_qty": 10,
        },
        {
            **base,
            "event_type": "fill",
            "ts_exchange": t0 + 2,
            "ts_exchange_kind": "match_time",
            "qty": 6,
            "cum_qty": 6,
            "leaves_qty": 4,
            "fill_id": "f1",
        },
        {
            **base,
            "event_type": "amend_sent",
            "ts_local_send": t0 + 4,
            "ts_exchange_kind": "unknown",
            "qty": 6,
            "px": px - 1,
        },
        {
            **base,
            "event_type": "amend_ack",
            "ts_exchange": t0 + 5,
            "ts_exchange_kind": "match_time",
            "qty": 6,
            "px": px - 1,
            "cum_qty": 6,
            "leaves_qty": 0,
        },
    ]
    write_canonical_log(tmp_path / "live", [order], evs)
    spec, _ = to_schedule(str(tmp_path / "live"), entry="exchange_ack", latency_ns=None)
    legs = [(r["shadow_id"], r["qty"], r["price"]) for r in spec.to_pylist()]
    assert legs == [("v:a:K", 10, px), ("v:a:K.leg2", 6, px - 1)]
    rep = schedule_report(build_schedule(tmp_path / "live"))
    assert rep["n_scheduled"] == 1 and rep["entry_fallback_share"] == 0.0
    # A8: every re-priced leg is instruction-sized, so no special note any more
    assert not any("re-priced leg" in w for w in rep["warnings"])


_LEG_ENCODINGS = {
    "order_key_form": ("binance:acct1:5000001", "binance:acct1:5000001.leg2", "binance:acct1:5000001"),
    "plain_client_id": ("o3", "o3.leg2", "sim:{sim}:acct1:o3"),
}


@pytest.mark.parametrize("sim", ["hftbacktest", "nautilus"])
@pytest.mark.parametrize("encoding", sorted(_LEG_ENCODINGS))
def test_sim_orders_per_leg_aggregate_under_one_order_key(tmp_path: Path, sim: str, encoding: str) -> None:
    """A simulator that places a new order per leg of a re-priced live order (client ids
    `<order_key>.leg<k>` or a plain `<id>.leg<k>`) imports as one canonical order with leg events, no QT-INPUT-003."""
    k1, k2, key = _LEG_ENCODINGS[encoding]
    key = key.format(sim=sim)
    if sim == "hftbacktest":
        rows = [
            _hbt_row(1, k1, "5", 1, 1792071000000000100),
            _hbt_row(1, k1, "5", 4, 1792071001000000000),
            _hbt_row(2, k2, "5", 1, 1792071001000000100, 10001),
            _hbt_row(2, k2, "3", 5, 1792071002000000000, 10001),
        ]
        src = tmp_path / "hbt.jsonl"
        src.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    else:
        src = tmp_path / "orders.csv"
        with src.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                [
                    "client_order_id",
                    "venue_order_id",
                    "instrument_id",
                    "side",
                    "type",
                    "quantity",
                    "price",
                    "time_in_force",
                    "post_only",
                    "status",
                    "filled_qty",
                    "ts_init",
                    "ts_last",
                ]
            )
            w.writerow(
                [
                    k1,
                    "V1",
                    "BTCUSDT.BINANCE",
                    "BUY",
                    "LIMIT",
                    "5",
                    "100.00",
                    "GTC",
                    "False",
                    "CANCELED",
                    "0",
                    1792071000000000000,
                    1792071001000000000,
                ]
            )
            w.writerow(
                [
                    k2,
                    "V2",
                    "BTCUSDT.BINANCE",
                    "BUY",
                    "LIMIT",
                    "5",
                    "100.01",
                    "GTC",
                    "False",
                    "PARTIALLY_FILLED",
                    "2",
                    1792071001000000000,
                    1792071002000000000,
                ]
            )
    with pytest.warns(UserWarning, match="unmapped_instruments"):
        s = import_sim(sim, [str(src)], tmp_path / "out", account="acct1")
    o, e = (table_rows(t) for t in read_log(tmp_path / "out"))
    assert s.n_orders == 1 and [r["order_key"] for r in o] == [key]
    u = 10**8
    types = [(r["event_type"], r["qty"], r["px"], r["cum_qty"], r["leaves_qty"]) for r in e]
    tail = [
        ("amend_ack", 5 * u, 100010000000, 0, 5 * u),
        # the Nautilus orders report gives no avg_px: its fill (differenced from filled_qty) has no price
        ("fill", 2 * u, 100010000000 if sim == "hftbacktest" else None, 2 * u, 3 * u),
    ]
    assert types[-2:] == tail and "cancel_ack" not in [t[0] for t in types]
    assert o[0]["orig_qty"] == 5 * u and o[0]["limit_px"] == 100000000000  # the first leg's instruction
    assert any("merged from 2 schedule legs" in w for w in s.warnings)


@pytest.mark.parametrize(
    ("venue", "new_qty", "kept"),
    [
        ("cme", 6, True),
        ("cme", 10, False),
        ("cme", 12, False),
        ("fix44", 6, False),
        ("fix44", 10, False),
        ("binance", 6, True),
    ],
)
def test_cme_same_price_size_decrease_keeps_priority(tmp_path: Path, venue: str, new_qty: int, kept: bool) -> None:
    """CME (audit ruling A6): on `cme` a same-price size decrease keeps priority (a `qt.shadow_amend` of the
    leg); a same-size (A6) or larger modify starts a new leg; other venues keep their own rule."""
    key = f"{venue}:a:1"
    order = {
        "order_key": key,
        "venue": venue,
        "account": "a",
        "instrument": "ESZ6",
        "instrument_id": 7,
        "side": "buy",
        "order_type": "limit",
        "tif": "day",
        "orig_qty": 10,
        "qty_str": "10",
        "limit_px": 100,
        "px_str": "100",
        "price_exp": 0,
        "size_exp": 0,
    }
    t0 = 1_800_000_000_000_000_000
    base = {"order_key": key, "ts_precision": "ns", "source": "fix_dropcopy", "derived": False}
    evs = [
        {**base, "event_type": "submit_sent", "ts_local_send": t0 - 1, "ts_exchange_kind": "unknown", "qty": 10},
        {
            **base,
            "event_type": "ack",
            "ts_exchange": t0,
            "ts_exchange_kind": "gateway_time",
            "qty": 10,
            "cum_qty": 0,
            "leaves_qty": 10,
        },
        {
            **base,
            "event_type": "amend_ack",
            "ts_exchange": t0 + 5,
            "ts_exchange_kind": "gateway_time",
            "qty": new_qty,
            "px": 100,
            "cum_qty": 0,
            "leaves_qty": new_qty,
        },
        {
            **base,
            "event_type": "cancel_ack",
            "ts_exchange": t0 + 9,
            "ts_exchange_kind": "gateway_time",
            "cum_qty": 0,
            "leaves_qty": 0,
        },
    ]
    write_canonical_log(tmp_path / "live", [order], evs)
    sc = build_schedule(tmp_path / "live")
    legs = [r["shadow_id"] for r in sc.spec.to_pylist()]
    amends = [(r["shadow_id"], r["new_qty"]) for r in sc.amends.to_pylist()]
    if kept:
        assert legs == [key] and amends == [(key, new_qty)]
    elif new_qty == 10 and venue != "cme":
        assert legs == [key] and amends == []  # an unchanged total and price changes nothing off CME
    else:  # priority lost; on CME also for an unchanged modify (A6: iceberg refresh / account change)
        assert legs == [key, f"{key}.leg2"] and amends == []
