"""`import_venue` end to end, append mode, instrument map, unit and time helpers, simulator importers."""

from __future__ import annotations

import csv
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from live._helpers import FIXTURES

from queuetruth.errors import QtError
from queuetruth.live import (
    coverage,
    import_sim,
    import_venue,
    load_instrument_map,
    read_log,
    table_rows,
    validate,
)
from queuetruth.live._common import epoch_ns, make_order_key, rfc3339_ns, to_fixed
from queuetruth.live.importers.fix44 import fix_ts
from queuetruth.live.importers.ibkr_flex import _flex_ts
from queuetruth.schemas.models import LiveImportSummary


def test_import_venue_writes_valid_dir(tmp_path: Path) -> None:
    scen = FIXTURES / "binance/01_ws_execution_report_lifecycle"
    out = tmp_path / "live"
    s = import_venue(
        {
            "venue": "binance",
            "inputs": [{"path": str(scen / "user_data_stream.jsonl")}],
            "out": str(out),
            "account": "acct1",
        }
    )
    assert isinstance(s, LiveImportSummary) and s.validation_status == "fixture_only"
    assert (s.n_orders, s.n_events, s.n_fills) == (5, 15, 6)
    assert [f.name for f in s.files] == ["orders.parquet", "events.parquet", "manifest.json"]
    assert validate(str(out)) == []
    man = json.loads((out / "manifest.json").read_text())
    assert man["schema"] == "qt.live_manifest" and man["venues"] == ["binance"] and man["n_orders"] == 5
    # open conflict 11 (R-B5): the share is the numeric field; no warning when no order fell back
    assert s.entry_fallback_share == 0.0 and not any("entry_fallback_share" in w for w in s.warnings)
    again = import_venue(
        {
            "venue": "binance",
            "inputs": [{"path": str(scen / "user_data_stream.jsonl")}],
            "out": str(tmp_path / "again"),
            "account": "acct1",
        }
    )
    for a, b in zip(s.files, again.files, strict=True):
        assert a.sha256 == b.sha256  # byte-deterministic


def test_import_venue_append_dedupes(tmp_path: Path) -> None:
    scen = FIXTURES / "binance/04_ws_rest_dedupe"
    out = tmp_path / "live"
    import_venue(
        {
            "venue": "binance",
            "inputs": [{"path": str(scen / "user_data_stream.jsonl")}],
            "out": str(out),
            "account": "acct1",
        }
    )
    s = import_venue(
        {
            "venue": "binance",
            "inputs": [{"path": str(scen / "myTrades.json")}],
            "out": str(out),
            "account": "acct1",
            "append": True,
        }
    )
    assert (s.n_orders, s.n_events, s.dedupe_dropped) == (1, 2, 1)
    assert validate(str(out)) == []


def test_fills_only_coverage_flag(tmp_path: Path) -> None:
    scen = FIXTURES / "okx/02_rest_fills_ts_vs_filltime"
    s = import_venue(
        {"venue": "okx", "inputs": [{"path": str(scen / "trade_fills.json")}], "out": str(tmp_path), "account": "acct1"}
    )
    assert any("coverage=fills_only" in w for w in s.warnings)
    cov = coverage(tmp_path)
    assert cov["fills_only"] and cov["fills_only_orders"] == 2


def test_ibkr_tz_argument_and_default(tmp_path: Path) -> None:
    scen = FIXTURES / "ibkr_flex/01_flex_xml_executions"
    spec = {"venue": "ibkr_flex", "inputs": [{"path": str(scen / "flex_trades.xml")}], "out": str(tmp_path / "ny")}
    import_venue(spec, tz="America/New_York")
    ev = table_rows(read_log(tmp_path / "ny")[1])
    assert ev[0]["ts_exchange"] == 1792071001000000000
    s = import_venue(dict(spec, out=str(tmp_path / "utc")))
    ev_utc = table_rows(read_log(tmp_path / "utc")[1])
    assert ev_utc[0]["ts_exchange"] == 1792071001000000000 - 4 * 3600 * 10**9
    assert any("UTC" in w for w in s.warnings)


def test_inexact_decimal_fails_qt_input_004(tmp_path: Path) -> None:
    src = FIXTURES / "deribit/01_ws_user_orders_trades"
    work = tmp_path / "d"
    shutil.copytree(src, work)
    (work / "instrument_map.csv").write_text(
        "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\n"
        "deribit,BTC-PERPETUAL,101,,0,0,1,10\n"
    )
    with pytest.raises(QtError) as ei:
        import_venue(
            {
                "venue": "deribit",
                "inputs": [{"path": str(work / "ws_notifications.jsonl")}],
                "out": str(tmp_path / "o"),
                "instrument_map": {"path": str(work / "instrument_map.csv")},
            }
        )
    assert ei.value.code == "QT-INPUT-004"


def test_instrument_map_loader(tmp_path: Path) -> None:
    p = tmp_path / "m.csv"
    p.write_text(
        "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\n"
        "binance,BTCUSDT,1,,-2,-5,0.01,0.00001\nbinance,BTCUSDT,2,2026-10-15,-2,-5,0.01,0.00001\n"
    )
    m = load_instrument_map(p)
    assert m.lookup("binance", "BTCUSDT", None).instrument_id == 1  # type: ignore[union-attr]
    assert m.lookup("binance", "BTCUSDT", 20741).instrument_id == 2  # type: ignore[union-attr]
    assert m.entries[0].tick_size == 1 and m.entries[0].lot_size == 1
    p.write_text("venue,instrument,instrument_id,price_exp,size_exp,tick_size,lot_size\nx,Y,1,-2,0,0.001,1\n")
    with pytest.raises(QtError) as ei:
        load_instrument_map(p)
    assert ei.value.code == "QT-INPUT-004"


def test_unit_and_time_helpers() -> None:
    assert to_fixed(Decimal("60000.5"), -9, "px") == 60000500000000
    with pytest.raises(QtError):
        to_fixed(Decimal("0.0000000001"), -9, "px")
    assert epoch_ns(1792071120000417, "auto", "t") == (1792071120000417000, "us")
    with pytest.raises(QtError):
        epoch_ns(1792071120000417, "ms", "t")
    assert rfc3339_ns("2026-10-15T16:00:06.000077001Z", "t") == (1792080006000077001, "ns")
    assert rfc3339_ns("2026-10-15T18:00:06+02:00", "t")[0] == 1792080006000000000
    assert fix_ts("20261015-20:00:01.000412", "60", "x") == (1792094401000412000, "us")
    with pytest.raises(QtError):  # 2026-11-01 01:30 happens twice in New York
        _flex_ts("20261101;013000", "America/New_York", "x")
    with pytest.raises(QtError):  # 2026-03-08 02:30 does not exist in New York
        _flex_ts("20260308;023000", "America/New_York", "x")
    key = make_order_key("v", "acct with space", "id/1")
    assert key == "v:acct_with_space:id_1"
    long = make_order_key("v", "a", "x" * 300)
    assert len(long) <= 116


# ---- simulator importers -----------------------------------------------------------------------------------------


def _hbt_rows() -> list[dict[str, Any]]:
    base = {
        "order_id": 7,
        "side": 1,
        "price_tick": 10000,
        "tick_size": "0.01",
        "qty": "5",
        "order_type": 0,
        "time_in_force": 0,
        "order_key": "binance:acct1:5000001.leg2",
    }
    return [
        dict(
            base,
            leaves_qty="5",
            exec_qty="0",
            exec_price_tick=0,
            status=1,
            exch_timestamp=1792071000000000100,
            local_timestamp=1792071000000000000,
            maker=False,
        ),
        dict(
            base,
            leaves_qty="3",
            exec_qty="2",
            exec_price_tick=10000,
            status=5,
            exch_timestamp=1792071001000000000,
            local_timestamp=1792071001000000050,
            maker=True,
        ),
        dict(
            base,
            leaves_qty="3",
            exec_qty="0",
            exec_price_tick=0,
            status=4,
            exch_timestamp=1792071002000000000,
            local_timestamp=1792071002000000050,
            maker=False,
        ),
        {
            "order_id": 8,
            "side": -1,
            "price_tick": 10010,
            "tick_size": "0.01",
            "qty": "1",
            "leaves_qty": "1",
            "exec_qty": "0",
            "exec_price_tick": 0,
            "status": 6,
            "exch_timestamp": 1792071003000000000,
            "local_timestamp": 1792071003000000000,
            "maker": False,
            "order_type": 0,
            "time_in_force": 1,
        },
    ]


def test_hftbacktest_snapshots_jsonl_and_csv(tmp_path: Path) -> None:
    rows = _hbt_rows()
    jl = tmp_path / "orders.jsonl"
    jl.write_text("".join(json.dumps(r) + "\n" for r in rows))
    s = import_sim("hftbacktest", [str(jl)], tmp_path / "out", account="acct1", size_exp=0, map_venue="binance")
    o, e = (table_rows(t) for t in read_log(tmp_path / "out"))
    assert s.venue == "sim:hftbacktest" and {r["venue"] for r in o} == {"sim:hftbacktest"}
    by = {r["order_key"]: r for r in o}
    assert set(by) == {"binance:acct1:5000001", "sim:hftbacktest:acct1:8"}
    assert by["binance:acct1:5000001"]["limit_px"] == 100000000000 and by["sim:hftbacktest:acct1:8"]["tif"] == "gtx"
    ev = [(r["order_key"], r["event_type"], r["qty"]) for r in e]
    assert ev == [
        ("binance:acct1:5000001", "submit_sent", 5),
        ("binance:acct1:5000001", "ack", 5),
        ("binance:acct1:5000001", "fill", 2),
        ("binance:acct1:5000001", "cancel_ack", None),
        ("sim:hftbacktest:acct1:8", "submit_sent", 1),
        ("sim:hftbacktest:acct1:8", "post_only_reject", None),
    ]
    fill = next(r for r in e if r["event_type"] == "fill")
    assert (fill["px"], fill["liquidity"], fill["derived"], fill["source"]) == (
        100000000000,
        "maker",
        True,
        "sim",  # schema 1.1 (SPEC 3.11 #15); v0.1 stopgap was "synthetic"
    )
    assert {r["validation_status"] for r in o} == {"simulated"}  # 1.1; v0.1 stopgap "fixture_only"
    cs = tmp_path / "orders.csv"
    with cs.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in rows[0]})
    import_sim("hftbacktest", [str(cs)], tmp_path / "out_csv", account="acct1", size_exp=0)
    e2 = table_rows(read_log(tmp_path / "out_csv")[1])
    assert [(r["event_type"], r["qty"]) for r in e2] == [(x[1], x[2]) for x in ev]


def test_nautilus_reports_and_events(tmp_path: Path) -> None:
    orders = tmp_path / "orders_report.csv"
    orders.write_text(
        "client_order_id,venue_order_id,instrument_id,side,type,quantity,price,time_in_force,post_only,"
        "reduce_only,status,filled_qty,avg_px,ts_init,ts_last\n"
        "binance:acct1:5000002,V-1,BTCUSDT.BINANCE,SELL,LIMIT,0.30000000,60010.50,GTC,True,False,FILLED,"
        "0.30000000,60010.50,1792071001000000000,1792071007000000000\n"
        "binance:acct1:5000004,V-2,BTCUSDT.BINANCE,SELL,LIMIT,0.10000000,60030.00,GTC,False,False,CANCELED,"
        "0,,1792071002000000000,1792071010000000000\n"
        "O-1,V-3,BTCUSDT.BINANCE,BUY,LIMIT,0.20000000,60000.00,GTC,False,False,FILLED,0.20000000,60000.00,"
        "1792071003000000000,1792071004000000000\n"
    )
    fills = pa.table(
        {
            "type": ["OrderFilled", "OrderFilled"],
            "client_order_id": ["binance:acct1:5000002", "binance:acct1:5000002"],
            "trade_id": ["T-1", "T-2"],
            "order_side": ["SELL", "SELL"],
            "last_qty": ["0.10000000", "0.20000000"],
            "last_px": ["60010.50", "60010.50"],
            "liquidity_side": ["MAKER", "MAKER"],
            "commission": ["0.60 USDT", "1.20 USDT"],
            "ts_event": [1792071005000000000, 1792071007000000000],
            "instrument_id": ["BTCUSDT.BINANCE", "BTCUSDT.BINANCE"],
        }
    )
    pq.write_table(fills, tmp_path / "order_filled.parquet")
    s = import_sim("nautilus", [str(orders), str(tmp_path / "order_filled.parquet")], tmp_path / "out", account="acct1")
    o, e = (table_rows(t) for t in read_log(tmp_path / "out"))
    by = {r["order_key"]: r for r in o}
    assert set(by) == {"binance:acct1:5000002", "binance:acct1:5000004", "sim:nautilus:acct1:O-1"}
    assert by["binance:acct1:5000002"]["tif"] == "post_only" and by["binance:acct1:5000002"]["instrument"] == "BTCUSDT"
    ev = {(r["order_key"], r["event_seq"]): r for r in e}
    assert [r["event_type"] for k, r in sorted(ev.items()) if k[0] == "binance:acct1:5000002"] == [
        "ack",
        "fill",
        "fill",
    ]
    assert ev[("binance:acct1:5000002", 2)]["fee"] == pytest.approx(0.6)
    assert ev[("binance:acct1:5000002", 2)]["fee_ccy"] == "USDT"
    assert [r["event_type"] for k, r in sorted(ev.items()) if k[0] == "binance:acct1:5000004"] == ["ack", "cancel_ack"]
    derived = [r for r in e if r["order_key"] == "sim:nautilus:acct1:O-1" and r["event_type"] == "fill"]
    assert len(derived) == 1 and derived[0]["derived"] and derived[0]["qty"] == 20000000
    assert any("derived" in w for w in s.warnings)
    assert validate(str(tmp_path / "out")) == []
