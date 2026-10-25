"""Units and feed helpers: exact decimals, message parsing, the ingest line form, REST seeds."""

from __future__ import annotations

import json

import pytest
from livefill import feed
from livefill.units import from_fixed, to_fixed

from queuetruth.errors import QtError


def test_to_fixed_exact_and_inexact() -> None:
    assert to_fixed("83614.17", -9) == 83_614_170_000_000
    assert to_fixed("0.01250000", -8) == 1_250_000
    assert to_fixed(0.0125, -8) == 1_250_000  # JSON float through its shortest repr
    assert to_fixed(7, -2) == 700
    with pytest.raises(QtError) as e:
        to_fixed("0.000000001", -8)
    assert e.value.code == "QT-INPUT-004"
    for bad in ("abc", "nan", "inf", True):
        with pytest.raises(QtError) as e:
            to_fixed(bad, -8)  # type: ignore[arg-type]
        assert e.value.code == "QT-INPUT-002"


def test_from_fixed_round_trip() -> None:
    assert from_fixed(83_614_170_000_000, -9) == "83614.17"
    assert from_fixed(100_000_000, -8) == "1"
    assert from_fixed(0, -8) == "0"
    for text in ("0.99874", "12.5", "100000"):
        assert from_fixed(to_fixed(text, -9), -9) == text


def test_parse_order_trade_and_book_messages() -> None:
    order = (
        '{"data":{"id":2060025400799234,"id_str":"2060025400799234","order_type":0,"microtimestamp":"1791770863525000",'
        '"amount_str":"0.01250000","price_str":"83614.17"},"channel":"live_orders_btcusd","event":"order_created",'
        '"event_id":"a","pre_event_id":"b"}'
    )
    p = feed.parse_message(order)
    assert (p.event, p.market, p.prefix, p.order_id, p.microtimestamp) == (
        "order_created",
        "btcusd",
        "live_orders",
        2060025400799234,
        1791770863525000,
    )
    assert (p.event_id, p.pre_event_id, p.has_pre_event_id, p.trade_id) == ("a", "b", True, None)
    trade = (
        '{"data":{"id":653276544,"amount_str":"0.00006042","price_str":"83614.15","type":1,'
        '"microtimestamp":"1791770867238000","buy_order_id":11,"sell_order_id":12},"channel":"live_trades_btcusd",'
        '"event":"trade"}'
    )
    t = feed.parse_message(trade)
    assert (t.trade_id, t.buy_order_id, t.sell_order_id, t.order_id) == (653276544, 11, 12, None)
    ctl = feed.parse_message('{"event":"bts:heartbeat","channel":"","data":{"status":"success"}}')
    assert ctl.market is None and ctl.event == "bts:heartbeat"
    with pytest.raises(QtError):
        feed.parse_message("not json")
    with pytest.raises(QtError):
        feed.parse_message("[1,2]")


def test_jsonl_line_keeps_text_and_appends_recv_ns() -> None:
    raw = '{"data":{"amount":0.0125000,"price_str":"1.10"},"event":"order_created"}\n'
    line = feed.jsonl_line(raw, 123)
    assert line == '{"data":{"amount":0.0125000,"price_str":"1.10"},"event":"order_created","recv_ns":123}'
    assert json.loads(line)["recv_ns"] == 123
    assert feed.jsonl_line("{}", 5) == '{"recv_ns":5}'
    assert json.loads(feed.jsonl_line('{\n "a": 1\n}', 1)) == {"a": 1, "recv_ns": 1}
    with pytest.raises(QtError):
        feed.jsonl_line('{"recv_ns": 1}', 2)
    with pytest.raises(QtError):
        feed.jsonl_line("[1]", 2)


def test_rest_seed_line_shape_and_validation() -> None:
    body = '{"timestamp":"1","microtimestamp":"1000","bids":[["1.0","2.0","5"]],"asks":[["1.1","3.0","6"]]}'
    line = json.loads(feed.rest_seed_line("usdtusd", body, 9))
    assert line["channel"] == "detail_order_book_usdtusd" and line["event"] == "data"
    assert line["data"]["bids"] == [["1.0", "2.0", "5"]] and line["recv_ns"] == 9 and line["livefill_seed"] == "rest"
    with pytest.raises(QtError, match="group=2"):
        feed.rest_seed_line("x", '{"microtimestamp":"1","bids":[["1","2"]],"asks":[]}', 1)
    with pytest.raises(QtError, match="microtimestamp"):
        feed.rest_seed_line("x", '{"bids":[],"asks":[]}', 1)


def test_market_and_channel_validation() -> None:
    assert feed.check_market(" USDTUSD ") == "usdtusd"
    for bad in ("", "btc-usd", "btc_usd"):
        with pytest.raises(QtError):
            feed.check_market(bad)
    assert feed.check_channels(["live_orders", "live_trades", "live_orders"]) == ["live_orders", "live_trades"]
    with pytest.raises(QtError, match="live_orders is required"):
        feed.check_channels(["live_trades"])
    with pytest.raises(QtError):
        feed.check_channels(["live_orders", "nope"])
    subs = feed.subscribe_messages(["a", "b"], ["live_orders"])
    assert [json.loads(s)["data"]["channel"] for s in subs] == ["live_orders_a", "live_orders_b"]


def test_malformed_snapshot_entries_are_input_errors() -> None:
    from livefill.stream import parse_book

    ok = parse_book('{"data":{"microtimestamp":"1","bids":[["1.00","1","7"]],"asks":[]}}', 0, "ws")
    assert ok.entries == (("buy", 1_000_000_000, 100_000_000, 7),) and (ok.n_bids, ok.n_asks) == (1, 0)
    for bad in (
        '{"data":{"microtimestamp":"1","bids":[["1.00","1","abc"]],"asks":[]}}',
        '{"data":{"microtimestamp":"1","bids":[["1.00","1",true]],"asks":[]}}',
        '{"data":{"microtimestamp":"1","bids":[["1.00","1"]],"asks":[]}}',
        '{"data":{"microtimestamp":"1","asks":[]}}',
        "not json",
    ):
        with pytest.raises(QtError) as e:
            parse_book(bad, 5, "ws")
        assert e.value.code == "QT-INPUT-002" and "seq=5" in e.value.message
