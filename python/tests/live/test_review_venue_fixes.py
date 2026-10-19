"""Review findings L5, L7, L8, L9 and L10 (python_analytics.md, "Findings in live/"): venue importer fixes, each
ported from its reproducer with synthetic in-test data (fake accounts, no network)."""

from __future__ import annotations

import calendar
import json
from pathlib import Path
from typing import Any

import pytest

from queuetruth.errors import QtError
from queuetruth.live import IMPORTERS, ImportOptions, build_log, check_tables, table_rows


def _jsonl(path: Path, lines: list[Any]) -> str:
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(path)


def _run(venue: str, paths: list[str], **extra: Any) -> Any:
    return IMPORTERS[venue](paths, ImportOptions(account="a", extra=extra))


def _valid(res: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Canonical rows of an import; asserts the SPEC 3.8 validator finds no error."""
    orders, events = build_log(res.orders, res.events)
    errors, _, _ = check_tables(orders, events)
    assert errors == []
    return table_rows(orders), table_rows(events)


def _evs(events: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted((e for e in events if e["order_key"] == key), key=lambda e: e["event_seq"])


# ---- L5 Hyperliquid: modify whose new-oid `open` precedes the old-oid `canceled` --------------------------------

_CLOID = "0x000000000000000000000000000000c1"


def _hl_order(oid: int, px: str, sz: str, ts: int, orig: str = "1.0") -> dict[str, Any]:
    return {
        "coin": "ETH",
        "side": "B",
        "limitPx": px,
        "sz": sz,
        "oid": oid,
        "timestamp": ts,
        "origSz": orig,
        "cloid": _CLOID,
    }


def _hl_log(path: Path, modify: list[dict[str, Any]], *, with_open: bool = True) -> str:
    lines: list[Any] = []
    if with_open:
        lines.append(
            {
                "recv_ns": 1792090802509900000,
                "msg": {
                    "channel": "orderUpdates",
                    "data": [
                        {
                            "order": _hl_order(3, "2500.0", "1.0", 1792090802500),
                            "status": "open",
                            "statusTimestamp": 1792090802500,
                        }
                    ],
                },
            }
        )
    lines.append({"recv_ns": 1792090807009900000, "msg": {"channel": "orderUpdates", "data": modify}})
    fill = {
        "coin": "ETH",
        "px": "2501.5",
        "sz": "0.4",
        "side": "B",
        "time": 1792090809000,
        "oid": 13,
        "tid": 77,
        "crossed": False,
        "fee": "-0.01",
        "feeToken": "USDC",
        "cloid": _CLOID,
    }
    lines.append(
        {
            "recv_ns": 1792090809009900000,
            "msg": {"channel": "userFills", "data": {"isSnapshot": False, "user": "0xabc", "fills": [fill]}},
        }
    )
    end = {
        "order": _hl_order(13, "2501.5", "0.6", 1792090807000),
        "status": "canceled",
        "statusTimestamp": 1792090811000,
    }
    lines.append({"recv_ns": 1792090811009900000, "msg": {"channel": "orderUpdates", "data": [end]}})
    return _jsonl(path, lines)


def _hl_modify() -> list[dict[str, Any]]:
    return [
        {"order": _hl_order(3, "2500.0", "1.0", 1792090802500), "status": "canceled", "statusTimestamp": 1792090807000},
        {"order": _hl_order(13, "2501.5", "1.0", 1792090807000), "status": "open", "statusTimestamp": 1792090807000},
    ]


def _strip_raw(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "raw"} for r in rows]


def test_l5_hyperliquid_modify_open_listed_first_stays_one_order(tmp_path: Path) -> None:
    got = {}
    for name, data in (("cancel_first", _hl_modify()), ("open_first", _hl_modify()[::-1])):
        res = _run("hyperliquid", [_hl_log(tmp_path / f"{name}.jsonl", data)])
        orders, events = _valid(res)
        assert [o["order_key"] for o in orders] == ["hyperliquid:a:3"]  # no phantom order for oid 13
        evs = _evs(events, "hyperliquid:a:3")
        assert [e["event_type"] for e in evs] == ["ack", "amend_ack", "fill", "cancel_ack"]
        amend = evs[1]
        assert amend["px"] == 2501500000000 and amend["qty"] == 100000000
        assert amend["amend_keeps_priority"] is False
        assert "oid 13" in amend["reason"] and "oid 3" in amend["reason"]
        assert evs[2]["fill_id"] == "1792090809000:ETH:77" and evs[3]["cum_qty"] == 40000000
        assert orders[0]["venue_order_id"] == "3" and orders[0]["client_order_id"] == _CLOID
        got[name] = (_strip_raw(orders), _strip_raw(events))
    assert got["open_first"] == got["cancel_first"]


def test_l5_hyperliquid_modify_of_order_not_seen_before(tmp_path: Path) -> None:
    """The modify message is the first sight of the old order: still one order, same rows for both listings."""
    got = {}
    for name, data in (("cancel_first", _hl_modify()), ("open_first", _hl_modify()[::-1])):
        res = _run("hyperliquid", [_hl_log(tmp_path / f"{name}.jsonl", data, with_open=False)])
        orders, events = _valid(res)
        assert [o["order_key"] for o in orders] == ["hyperliquid:a:3"]
        assert orders[0]["limit_px"] == 2500000000000  # the replaced order's own price, not the new one
        assert [e["event_type"] for e in _evs(events, "hyperliquid:a:3")] == ["amend_ack", "fill", "cancel_ack"]
        got[name] = (_strip_raw(orders), _strip_raw(events))
    assert got["open_first"] == got["cancel_first"]


# ---- L7 Deribit: `price: "market_price"` on open trigger-market orders --------------------------------------------


def _deribit_order(oid: str, state: str, price: Any, order_type: str, ts: int, **kw: Any) -> dict[str, Any]:
    o = {
        "order_id": oid,
        "label": "",
        "instrument_name": "ETH-PERPETUAL",
        "direction": "sell",
        "order_type": order_type,
        "order_state": state,
        "price": price,
        "amount": 10,
        "filled_amount": 0,
        "creation_timestamp": 1792071000000,
        "last_update_timestamp": ts,
        "time_in_force": "good_til_cancelled",
        "post_only": False,
        "reduce_only": True,
        "replaced": False,
    }
    o.update(kw)
    return {"jsonrpc": "2.0", "method": "subscription", "params": {"channel": "user.orders.any.any.raw", "data": o}}


def test_l7_deribit_non_numeric_price_is_null_and_import_continues(tmp_path: Path) -> None:
    path = _jsonl(
        tmp_path / "ws.jsonl",
        [
            _deribit_order("ETH-1", "untriggered", "market_price", "stop_market", 1792071000000, trigger_price=2400),
            _deribit_order("ETH-2", "open", 2500, "limit", 1792071001000),
            _deribit_order(
                "ETH-1",
                "cancelled",
                "market_price",
                "stop_market",
                1792071002000,
                cancel_reason="user_request",
                trigger_price=2400,
            ),
            _deribit_order("ETH-2", "cancelled", 2500, "limit", 1792071003000, cancel_reason="user_request"),
        ],
    )
    res = _run("deribit", [path])  # used to raise QT-INPUT-002 for the whole import
    orders, events = _valid(res)
    by_key = {o["order_key"]: o for o in orders}
    trig = by_key["deribit:a:ETH-1"]
    assert trig["limit_px"] is None and trig["px_str"] is None and trig["order_type"] == "stop"
    assert [(e["event_type"], e["px"]) for e in _evs(events, "deribit:a:ETH-1")] == [("cancel_ack", None)]
    assert by_key["deribit:a:ETH-2"]["limit_px"] == 2500000000000
    assert [e["event_type"] for e in _evs(events, "deribit:a:ETH-2")] == ["ack", "cancel_ack"]
    assert any("2 order record(s) with a non-numeric price" in w and "'market_price' x2" in w for w in res.warnings)


# ---- L8 Coinbase: monotone user-channel state, PENDING-first ack ---------------------------------------------------


def _cb_msg(seq: int, ts: str, status: str, cum: str, leaves: str, value: str, nf: int) -> dict[str, Any]:
    o = {
        "client_order_id": "c1",
        "cumulative_quantity": cum,
        "filled_value": value,
        "leaves_quantity": leaves,
        "limit_price": "60000.00",
        "number_of_fills": str(nf),
        "order_id": "OID1",
        "order_side": "BUY",
        "order_type": "Limit",
        "post_only": "false",
        "product_id": "BTC-USD",
        "status": status,
        "time_in_force": "GOOD_UNTIL_CANCELLED",
        "total_fees": "0",
        "creation_time": "2026-10-15T16:00:01.000123Z",
    }
    return {
        "recv_ns": 1792080001001023000 + seq,
        "msg": {"channel": "user", "timestamp": ts, "sequence_num": seq, "events": [{"type": "update", "orders": [o]}]},
    }


_P = _cb_msg(0, "2026-10-15T16:00:01.2Z", "PENDING", "0", "1.0", "0", 0)
_A = _cb_msg(1, "2026-10-15T16:00:01.5Z", "OPEN", "0", "1.0", "0", 0)
_B = _cb_msg(2, "2026-10-15T16:00:02Z", "OPEN", "0.3", "0.7", "18000", 1)
_C = _cb_msg(3, "2026-10-15T16:00:03Z", "OPEN", "0.5", "0.5", "30000", 2)
_D = _cb_msg(4, "2026-10-15T16:00:04Z", "CANCELLED", "0.5", "0", "30000", 2)
_STALE = "stale user-channel order update ignored"


def _cb(tmp_path: Path, name: str, msgs: list[dict[str, Any]]) -> tuple[Any, list[dict[str, Any]]]:
    res = _run("coinbase", [_jsonl(tmp_path / f"{name}.jsonl", msgs)])
    _, events = _valid(res)
    return res, _evs(events, "coinbase:a:OID1")


def _stale_count(res: Any) -> int:
    return sum(n for r, n in res.skipped.items() if r.startswith(_STALE))


def test_l8_coinbase_replayed_lower_cumulative_quantity_is_ignored(tmp_path: Path) -> None:
    res, evs = _cb(tmp_path, "replayed", [_A, _B, _C, _B, _D])  # B replayed after C: used to fail the validator
    assert [(e["event_type"], e["qty"], e["cum_qty"]) for e in evs] == [
        ("ack", 100000000, 0),
        ("fill", 30000000, 30000000),
        ("fill", 20000000, 50000000),
        ("cancel_ack", None, 50000000),
    ]
    assert _stale_count(res) == 1
    assert any(_STALE in w and w.startswith("coinbase: 1 record(s)") for w in res.warnings)
    _, in_order = _cb(tmp_path, "in_order", [_A, _B, _C, _D])
    assert _strip_raw(evs) == _strip_raw(in_order)


def test_l8_coinbase_out_of_order_and_repeated_terminal(tmp_path: Path) -> None:
    res, evs = _cb(tmp_path, "ooo", [_A, _C, _B, _D, _D])
    assert [(e["event_type"], e["qty"], e["cum_qty"]) for e in evs] == [
        ("ack", 100000000, 0),
        ("fill", 50000000, 50000000),
        ("cancel_ack", None, 50000000),  # one terminal event, not two
    ]
    assert _stale_count(res) == 2


def test_l8_coinbase_pending_first_order_is_acked(tmp_path: Path) -> None:
    res, evs = _cb(tmp_path, "pending", [_P, _A, _B, _D])
    assert [e["event_type"] for e in evs] == ["ack", "fill", "fill", "cancel_ack"]
    ack = evs[0]
    created = calendar.timegm((2026, 10, 15, 16, 0, 1, 0, 0, 0)) * 10**9 + 123_000
    assert (ack["ts_exchange"], ack["qty"], ack["cum_qty"], ack["leaves_qty"]) == (created, 100000000, 0, 100000000)
    assert ack["px"] == 60000000000000 and ack["ts_exchange_kind"] == "record_time"
    assert res.skipped.get("user-channel status PENDING") == 1
    # a PENDING order that then fails is rejected, never acked
    failed = _cb_msg(5, "2026-10-15T16:00:01.6Z", "FAILED", "0", "0", "0", 0)
    _, evs = _cb(tmp_path, "pending_failed", [_P, failed])
    assert [e["event_type"] for e in evs] == ["reject"]


# ---- L9 Coinbase: quote-sized fill whose base size is not an exact decimal ----------------------------------------


def _cb_fill(entry: str, oid: str, size: str, price: str, in_quote: bool, t: str = "16:00:01.5") -> dict[str, Any]:
    return {
        "entry_id": entry,
        "trade_id": f"t-{entry}",
        "order_id": oid,
        "trade_time": f"2026-10-15T{t}Z",
        "trade_type": "FILL",
        "price": price,
        "size": size,
        "commission": "0.06",
        "product_id": "BTC-USD",
        "liquidity_indicator": "TAKER",
        "size_in_quote": in_quote,
        "side": "BUY",
    }


def test_l9_coinbase_inexact_quote_fill_excludes_its_order(tmp_path: Path) -> None:
    fills = [
        _cb_fill("e1", "o1", "10", "67123.45", True),  # 10 / 67123.45 does not terminate
        _cb_fill("e2", "o2", "0.001", "67000", False),
        _cb_fill("e3", "o3", "10", "50000", True, "16:00:02"),  # exact: 0.0002 BTC
        _cb_fill("e4", "o1", "0.0001", "67100", False, "16:00:03"),  # exact, but o1's filled qty is unknown
    ]
    path = tmp_path / "fills.json"
    path.write_text(json.dumps({"fills": fills}))
    res = _run("coinbase", [str(path)])  # used to raise QT-INPUT-004 for the whole import
    orders, events = _valid(res)
    assert sorted(o["order_key"] for o in orders) == ["coinbase:a:o2", "coinbase:a:o3"]
    assert [e["qty"] for e in _evs(events, "coinbase:a:o3")] == [20000]
    msg = next(w for w in res.warnings if "order(s) excluded with their" in w)
    assert msg.startswith("coinbase: 1 order(s) excluded with their 2 record(s)")
    assert "o1 (entry e1: 10 USD / 67123.45)" in msg and "not an exact decimal" in msg


def test_l9_coinbase_inexact_quote_fill_excludes_user_channel_records(tmp_path: Path) -> None:
    ws = [_A, _B, _D]  # order OID1 on the user channel ...
    fills = [_cb_fill("e9", "OID1", "10", "67123.45", True)]  # ... and its quote-sized List Fills row
    other = _cb_msg(7, "2026-10-15T16:00:05Z", "OPEN", "0", "2.0", "0", 0)
    other["msg"]["events"][0]["orders"][0].update(order_id="OID2", client_order_id="c2")
    ws_path = _jsonl(tmp_path / "ws.jsonl", [*ws, other])
    fills_path = tmp_path / "fills.json"
    fills_path.write_text(json.dumps({"fills": fills}))
    res = _run("coinbase", [ws_path, str(fills_path)])
    orders, events = _valid(res)
    assert [o["order_key"] for o in orders] == ["coinbase:a:OID2"]
    assert [e["event_type"] for e in _evs(events, "coinbase:a:OID2")] == ["ack"]
    assert any(w.startswith("coinbase: 1 order(s) excluded with their 4 record(s)") for w in res.warnings)


# ---- L10 IBKR Flex: slash-date order inferred per file -------------------------------------------------------------


def _flex_xml(path: Path, rows: list[tuple[str, str]]) -> str:
    """Flex XML with one BUY execution per (exec id, dateTime)."""
    trades = "\n".join(
        f'<Trade accountId="U1" symbol="ACME" quantity="10" tradePrice="50.25" ibCommission="-0.1" '
        f'ibCommissionCurrency="USD" buySell="BUY" ibOrderID="41{i}" ibExecID="{eid}" tradeID="70{i}" '
        f'dateTime="{when}" levelOfDetail="EXECUTION" orderType="LMT" transactionType="ExchTrade" />'
        for i, (eid, when) in enumerate(rows)
    )
    path.write_text(
        f"<FlexQueryResponse><FlexStatements><FlexStatement><Trades>\n{trades}\n</Trades>"
        "</FlexStatement></FlexStatements></FlexQueryResponse>"
    )
    return str(path)


def _flex_csv(path: Path, rows: list[tuple[str, str]]) -> str:
    head = "IBExecID,TradeID,IBOrderID,Symbol,Buy/Sell,Quantity,TradePrice,IBCommission,Date/Time,LevelOfDetail\n"
    body = "".join(f'{e},80{i},42{i},ACME,SELL,-5,50.5,-0.05,"{w}",EXECUTION\n' for i, (e, w) in enumerate(rows))
    path.write_text(head + body)
    return str(path)


def _utc(y: int, mo: int, d: int, h: int, mi: int, s: int) -> int:
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) * 10**9


def _fill_ts(res: Any) -> dict[str, int]:
    _, events = _valid(res)
    return {e["fill_id"]: e["ts_exchange"] for e in events if e["event_type"] == "fill"}


def test_l10_ibkr_slash_date_order_inferred_per_file(tmp_path: Path) -> None:
    dmy = _flex_xml(tmp_path / "dmy.xml", [("x1", "05/03/2026;093015"), ("x2", "13/03/2026;100000")])
    mdy = _flex_csv(tmp_path / "mdy.csv", [("y1", "05/03/2026, 09:30:15"), ("y2", "03/14/2026, 10:00:00")])
    res = _run("ibkr_flex", [dmy, mdy], tz="UTC")
    ts = _fill_ts(res)
    assert ts["x1"] == _utc(2026, 3, 5, 9, 30, 15)  # dd/MM file (13/03 settles it): 5 March, not 3 May
    assert ts["x2"] == _utc(2026, 3, 13, 10, 0, 0)
    assert ts["y1"] == _utc(2026, 5, 3, 9, 30, 15)  # MM/dd file (03/14 settles it)
    assert ts["y2"] == _utc(2026, 3, 14, 10, 0, 0)
    assert not any("ambiguous" in w for w in res.warnings)


def test_l10_ibkr_contradictory_slash_dates_fail(tmp_path: Path) -> None:
    bad = _flex_xml(tmp_path / "mixed.xml", [("x1", "13/03/2026;093015"), ("x2", "03/14/2026;100000")])
    with pytest.raises(QtError) as ei:
        _run("ibkr_flex", [bad], tz="UTC")
    assert ei.value.code == "QT-INPUT-002" and "mixed.xml" in ei.value.message
    assert "yyyyMMdd" in ei.value.message


def test_l10_ibkr_ambiguous_slash_dates_warn_and_flex_date_order_option(tmp_path: Path) -> None:
    amb = _flex_xml(tmp_path / "amb.xml", [("x1", "05/03/2026;093015"), ("x2", "06/03/2026;100000")])
    res = _run("ibkr_flex", [amb], tz="UTC")
    assert _fill_ts(res)["x1"] == _utc(2026, 5, 3, 9, 30, 15)  # US reading kept ...
    note = next(w for w in res.warnings if "ambiguous" in w)  # ... and named, with the way out
    assert "amb.xml" in note and "MM/dd/yyyy" in note and "flex_date_order='dmy'" in note
    assert "yyyyMMdd" in note and "yyyy-MM-dd" in note
    res = _run("ibkr_flex", [amb], tz="UTC", flex_date_order="dmy")
    assert _fill_ts(res)["x1"] == _utc(2026, 3, 5, 9, 30, 15)
    assert not any("ambiguous" in w for w in res.warnings)
    with pytest.raises(QtError) as ei:
        _run("ibkr_flex", [amb], tz="UTC", flex_date_order="ymd")
    assert ei.value.code == "QT-CONFIG-001"
    dmy_only = _flex_xml(tmp_path / "dmy.xml", [("z1", "13/03/2026;093015")])
    with pytest.raises(QtError) as ei:  # forced MM/dd on a day > 12: month 13 is not a date
        _run("ibkr_flex", [dmy_only], tz="UTC", flex_date_order="mdy")
    assert ei.value.code == "QT-INPUT-002" and "MM/dd/yyyy" in ei.value.message
