"""QuantConnect LEAN importer, pairing with a canonical live log and reconciliation (`queuetruth.live.importers.lean`)
on the fixtures of tests/fixtures/live/backtesters (written from LEAN release tag 18241's source by make_fixtures.py)
and on malformed inputs."""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from queuetruth import api
from queuetruth import live as qlive
from queuetruth.errors import QtError
from queuetruth.live import read_log, table_rows
from queuetruth.live.importers import SIM_IMPORTERS, ImportOptions, lean
from queuetruth.live.importers import freqtrade as ft

FIX = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "live" / "backtesters"
RESULT = FIX / "lean_result.json"
EVENTS = FIX / "lean_order_events.json"
ECSV = FIX / "lean_order_events.csv"
RESULT2 = FIX / "lean_run2_result.json"
EVENTS2 = FIX / "lean_run2_order_events.json"
SIM = [RESULT, EVENTS]
IMAP = FIX / "instrument_map.csv"
NS = 1_000_000_000
H = 3600 * NS
T0 = 1_709_251_200 * NS  # 2024-03-01T00:00:00Z

#: the scenario's outcome, the same as for Backtrader (python/tests/live/test_backtesters.py)
CATEGORIES = {
    "binance:main:7100001": "match",
    "binance:main:7100002": "match",
    "binance:main:7100003": "missed_fill",
    "binance:main:7100004": "price",
    "binance:main:7100005": "partial_fill",
    "binance:main:7100006": "timing",
    "binance:main:7100007": "no_sim_twin",
    "sim:lean:main:7": "no_live_twin",
}


def _err(code: str, fn: Any, *a: Any, **kw: Any) -> QtError:
    with pytest.raises(QtError) as e:
        fn(*a, **kw)
    assert e.value.code == code, str(e.value)
    return e.value


@pytest.fixture
def live(tmp_path: Path) -> Path:
    """The live arm: the fixture's Binance user-data stream imported as a canonical live log."""
    out = tmp_path / "live"
    api.import_live(
        {
            "venue": "binance",
            "inputs": [{"path": str(FIX / "binance_user_stream.jsonl")}],
            "out": str(out),
            "account": "main",
            "instrument_map": {"path": str(IMAP)},
        }
    )
    return out


@pytest.fixture
def w(tmp_path: Path) -> Any:
    """Writes a throwaway JSON input into the test's tmp_path and returns its path."""
    n = [0]

    def _w(doc: Any) -> str:
        n[0] += 1
        return _write(tmp_path, f"in{n[0]}.json", doc)

    return _w


def _write(tmp: Path, name: str, doc: Any) -> str:
    p = tmp / name
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return str(p)


def _rows(out: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    o, e = read_log(out)
    strip = lambda rs: [{k: v for k, v in r.items() if k != "raw"} for r in table_rows(rs)]  # noqa: E731
    return strip(o), strip(e)


def _orders() -> dict[str, Any]:
    return json.loads(RESULT.read_text())["orders"]


def _events() -> list[dict[str, Any]]:
    return json.loads(EVENTS.read_text())


def _view(run: Any) -> list[tuple[Any, ...]]:
    return [
        (o.ref, o.instrument, o.side, o.order_type, o.qty, o.limit_px, o.submit_ns, o.placed_ns, o.end,
         [(f.ts_ns, f.qty, f.px, f.fee, f.fill_id) for f in o.fills])
        for o in run.orders
    ]  # fmt: skip


def test_fixture_generator_is_current(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("lean_make_fixtures", FIX / "make_fixtures.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = tmp_path
    mod.main()
    for name in (
        "lean_result.json", "lean_order_events.json", "lean_order_events.csv", "lean_run2_result.json",
        "lean_run2_order_events.json",
    ):  # fmt: skip
        assert (tmp_path / name).read_text() == (FIX / name).read_text(), name


def test_registered_in_importer_registries() -> None:
    assert SIM_IMPORTERS["lean"] is lean.import_lean_sim
    assert qlive.SIM_VENUES["sim_lean"] == "lean"
    assert lean.LEAN_VERSION == "18241" and "data_tz" not in lean.LEAN_OPTIONS


def test_read_lean_result_and_events() -> None:
    run = lean.read_lean([str(p) for p in SIM], {})
    assert [o.ref for o in run.orders] == ["1", "2", "3", "4", "5", "6", "7"]
    first = run.orders[0]
    assert (first.instrument, first.side, first.order_type, first.tif, first.qty, first.limit_px) == (
        "BTCUSDT", "buy", "limit", "gtc", Decimal("0.01"), Decimal("61000.0"),
    )  # fmt: skip
    # placed and submitted at the end of the 09:00 bar; LEAN fills it at the end of the next bar
    assert first.submit_ns == first.placed_ns == T0 + 10 * H
    # LEAN logged 0.00999 with a fee of 0.00001 BTC (BinanceFeeModel): the gross 0.01, the fee in USDT
    raw = next(e for e in _events() if (e["orderId"], e["orderEventId"]) == (1, 2))
    assert (raw["fillQuantity"], raw["orderFeeAmount"], raw["orderFeeCurrency"]) == (0.00999, 0.00001, "BTC")
    assert [(f.ts_ns, f.precision, f.qty, f.px, f.fee, f.fill_id) for f in first.fills] == [
        (T0 + 11 * H, "s", Decimal("0.01"), Decimal("61000.0"), Decimal("0.61"), "1:2")
    ]
    market = run.orders[3]
    assert (market.side, market.order_type, market.tif, market.limit_px) == ("sell", "market", "other", None)
    assert market.fills[0].ts_ns == market.submit_ns == T0 + 16 * H
    assert market.fills[0].px == Decimal("61210.123456789")
    gtd = run.orders[6]
    assert gtd.fills == [] and gtd.tif == "gtd"
    # BacktestingBrokerage cancels an order past its Day / GoodTilDate expiry with "The order has expired."
    assert gtd.end == (T0 + 25 * H, "s", "expire", "Expired: The order has expired.")
    assert run.skipped == {}
    assert [w.split(":")[0] for w in run.warnings] == ["4 buy fill(s) charged in the base asset"]
    assert all(f.qty == o.qty for o in run.orders for f in o.fills)


def test_events_csv_and_events_only(tmp_path: Path) -> None:
    js = lean.read_lean([str(RESULT), str(EVENTS)], {})
    assert _view(lean.read_lean([str(RESULT), str(ECSV)], {})) == _view(js)
    # without the orders section: the orders come from their events (time in force unknown: gtc)
    alone = lean.read_lean([str(EVENTS)], {})
    assert _view(alone) == _view(js)
    assert alone.orders[6].tif == "gtc" and any(w.startswith("no orders section") for w in alone.warnings)
    # JSON lines of events read the same
    jl = _write(tmp_path, "events.jsonl", "\n".join(json.dumps(e) for e in _events()) + "\n")
    assert _view(lean.read_lean([str(RESULT), jl], {})) == _view(js)


def test_second_run_types_partials_invalid_and_csharp_names() -> None:
    run = lean.read_lean([str(RESULT2), str(EVENTS2)], {})
    got = {o.ref: o for o in run.orders}
    limit = got["1"]
    assert (limit.order_type, limit.tif, limit.qty) == ("limit", "day", Decimal("0.03"))
    assert [(f.qty, f.fee, f.fill_id) for f in limit.fills] == [
        (Decimal("0.01"), None, "1:2"),  # LEAN charges the whole fee on the final Filled event
        (Decimal("0.005"), None, "1:3"),
        (Decimal("0.015"), Decimal("1.815"), "1:4"),
    ]
    # the update moved the limit to 62400: the orders section holds the final price; the event is counted
    assert got["2"].limit_px == Decimal("62400.0") and got["2"].end is not None
    assert got["2"].end[2:] == ("cancel_ack", "Canceled: Canceled by the algorithm")
    assert run.skipped == {
        "UpdateSubmitted event (the orders section holds the final price and quantity)": 1,
        "partiallyfilled event without a fill quantity (an order update, no fill)": 1,
        "order LEAN rounded to quantity 0 and invalidated (below the lot size)": 1,
    }
    # order 8 (quantity 0, direction Hold, Invalid) is left out; order 9's refused update is no fill
    assert "8" not in got and got["4"].end is not None and got["4"].end[2] == "expire"
    assert [(f.qty, f.fee) for f in got["9"].fills] == [(Decimal("0.004"), None), (Decimal("0.006"), Decimal("0.602"))]
    assert (got["3"].order_type, got["3"].limit_px, got["3"].side) == ("stop", None, "sell")
    assert (got["4"].order_type, got["4"].limit_px, got["4"].tif) == ("stop_limit", Decimal("61050.0"), "gtd")
    refused = got["5"]
    assert refused.placed_ns is None and refused.submit_ns == T0 + 4 * H  # never rested: no ack
    assert refused.end is not None and refused.end[2] == "reject" and "Insufficient buying power" in refused.end[3]
    assert got["6"].order_type == "stop" and got["6"].fills[0].px == Decimal("59100.0")
    assert (got["7"].order_type, got["7"].tif) == ("market", "other")  # MarketOnOpen
    # UtcTime text from the C# names
    assert limit.fills[0].ts_ns == T0 + 3 * H


def test_import_lean_log(tmp_path: Path) -> None:
    s = qlive.import_sim("lean", [str(p) for p in SIM], tmp_path / "s", instrument_map=str(IMAP), venue="binance")
    assert (s.venue, s.n_orders, s.n_fills, s.validation_status) == ("sim:lean", 7, 6, "simulated")
    assert any("1 backtest price / size value(s) rounded half-even" in w for w in s.warnings)
    orders, events = _rows(tmp_path / "s")
    assert {o["order_key"] for o in orders} == {f"sim:lean:main:{i}" for i in range(1, 8)}
    assert all(o["instrument_id"] == 1 and o["price_exp"] == -2 for o in orders)
    by = {(e["order_key"], e["event_type"]): e for e in events}
    f4 = by[("sim:lean:main:4", "fill")]
    assert f4["px"] == 6121012 and f4["liquidity"] == "taker" and f4["source"] == "sim"
    assert by[("sim:lean:main:1", "fill")]["liquidity"] == "unknown"
    assert by[("sim:lean:main:1", "submit_sent")]["ts_local_send"] == T0 + 10 * H
    assert by[("sim:lean:main:1", "ack")]["ts_exchange"] == T0 + 10 * H
    assert by[("sim:lean:main:7", "expire")]["reason"] == "Expired: The order has expired."
    assert by[("sim:lean:main:1", "fill")]["qty"] == 1000 and by[("sim:lean:main:1", "fill")]["fee"] is not None
    s2 = qlive.import_sim("lean", [str(RESULT2), str(EVENTS2)], tmp_path / "s2", instrument_map=str(IMAP),
                          venue="binance")  # fmt: skip
    assert (s2.n_orders, s2.n_fills) == (8, 8)
    _, ev2 = _rows(tmp_path / "s2")
    kinds = [e["event_type"] for e in ev2 if e["order_key"] == "sim:lean:main:5"]
    assert kinds == ["submit_sent", "reject"]


def test_timeframe_moves_bar_end_fills_to_the_bar_start(w: Any) -> None:
    run = lean.read_lean([str(p) for p in SIM], {"timeframe": "1h"})
    first, market = run.orders[0], run.orders[3]
    assert first.fills[0].ts_ns == T0 + 10 * H  # stamped 11:00, the end of the bar [10:00, 11:00)
    assert market.fills[0].ts_ns == T0 + 16 * H  # filled on the bar it was placed on: kept
    assert any(w.startswith("timeframe=1h: 5 fill time(s) stamped at the end of their bar") for w in run.warnings)
    # fill_delay_bars moves them on afterwards
    late = lean.read_lean([str(p) for p in SIM], {"timeframe": "1h", "fill_delay_bars": 1})
    assert late.orders[0].fills[0].ts_ns == T0 + 11 * H
    # never before the submission: a fill a quarter bar after it goes back to the submission
    ev = _events()
    ev[1]["time"] = ev[0]["time"] + 900
    run2 = lean.read_lean([str(RESULT), w(ev)], {"timeframe": "1h"})
    assert run2.orders[0].fills[0].ts_ns == T0 + 10 * H


def test_times_symbols_and_fees(w: Any) -> None:
    ev = _events()
    ev[1]["time"] = ev[1]["time"] + 0.25  # Unix seconds with a fraction: ms precision
    ev[3]["time"] = "2024-03-01T13:00:00.5"  # ISO text without an offset: UTC
    ev[3]["orderFeeCurrency"] = "BTC"  # another currency than the fill price: left out
    run = lean.read_lean([str(RESULT), w(ev)], {})
    one, two = run.orders[0].fills[0], run.orders[1].fills[0]
    assert (one.ts_ns, one.precision) == (T0 + 11 * H + 250_000_000, "ms")
    assert (two.ts_ns, two.precision, two.fee) == (T0 + 13 * H + 500_000_000, "ms", None)
    assert any(w.startswith("1 fill fee(s) in another currency") for w in run.warnings)
    # symbols: the ticker of a symbol object, the first word of a security identifier; renamed by option
    orders = _orders()
    orders["1"]["symbol"] = {"id": "ETHUSDT 18N"}
    for e in ev:
        if e["orderId"] == 1:
            e["symbolValue"] = None
    events_only = lean.read_lean([w([e for e in ev if e["orderId"] == 1])], {})
    assert events_only.orders[0].instrument == "BTCUSDT"  # from symbol "BTCUSDT 18N"
    eth = _events()
    for e in eth[:2]:
        e["symbol"], e["symbolValue"] = "ETHUSDT 18N", "ETHUSDT"
    mixed = lean.read_lean([w({"orders": orders}), w(eth)], {})
    assert {o.instrument for o in mixed.orders} == {"ETHUSDT", "BTCUSDT"}
    named = lean.read_lean([w({"orders": orders}), w(eth)], {"instruments": "ETHUSDT:ETH-USDT"})
    assert {o.instrument for o in named.orders} == {"ETH-USDT", "BTCUSDT"}
    # an event of another symbol than its order
    e = _err("QT-INPUT-002", lean.read_lean, [w({"orders": orders}), str(EVENTS)], {})
    assert "order 1 event of symbol BTCUSDT, but the order is ETHUSDT" in str(e)
    assert {o.instrument for o in lean.read_lean([str(p) for p in SIM], {"instrument": "X"}).orders} == {"X"}
    _err("QT-CONFIG-001", lean.read_lean, [str(p) for p in SIM], {"instruments": "SOLUSDT:SOL"})


def test_events_in_the_result_and_duplicates(w: Any) -> None:
    # a QuantConnect API result packet carries orderEvents next to orders; the events file repeats them
    doc = {"orders": _orders(), "orderEvents": _events()}
    both = lean.read_lean([w(doc), str(EVENTS)], {})
    assert _view(both) == _view(lean.read_lean([str(p) for p in SIM], {}))
    assert both.skipped == {"duplicate order event (same orderId and orderEventId)": 14}
    alone = lean.read_lean([w(doc)], {})
    assert _view(alone) == _view(both)
    # the same event id with other content: two backtests mixed
    ev = _events()
    ev[1]["fillPrice"] = 1.0
    ev[1].pop("algorithmId")
    e = _err("QT-INPUT-002", lean.read_lean, [w(doc), w(ev)], {})
    assert "two backtests mixed?" in str(e)
    # the hyphenated names of older LEAN releases
    old = []
    for x in _events():
        y = dict(x)
        for new, hy in (("orderId", "order-id"), ("orderEventId", "order-event-id"), ("symbolValue", "symbol-value")):
            y[hy] = y.pop(new)
        old.append(y)
    assert _view(lean.read_lean([str(RESULT), w(old)], {})) == _view(both)


def test_refusals_of_mixed_backtests(tmp_path: Path, w: Any) -> None:
    e = _err("QT-CONFIG-001", lean.read_lean, [str(RESULT), str(EVENTS), str(EVENTS)], {})
    assert "given twice" in str(e)
    e = _err("QT-CONFIG-001", lean.read_lean, [str(RESULT), str(RESULT2), str(EVENTS)], {})
    assert "one import holds one backtest" in str(e)
    ev = _events()
    ev[0]["algorithmId"] = "other"
    e = _err("QT-CONFIG-001", lean.read_lean, [str(RESULT), w(ev)], {})
    assert "import one backtest at a time" in str(e)
    # events of another backtest than the orders section
    e = _err("QT-INPUT-002", lean.read_lean, [str(RESULT), str(EVENTS2)], {})
    assert "order id(s) [8, 9] that the orders section does not hold" in str(e)
    over = _events()
    over[1]["fillQuantity"] = 0.03
    over[1].pop("orderFeeAmount")
    e = _err("QT-INPUT-002", lean.read_lean, [str(RESULT), w(over)], {})
    assert "the fills of order 1 sum to 0.03, more than its quantity 0.01" in str(e)
    ev = [*_events(), dict(_events()[1], orderId=9, id="qtfixture0001-9-2")]
    e = _err("QT-INPUT-002", lean.read_lean, [str(RESULT), w(ev)], {})
    assert "order id(s) [9] that the orders section does not hold" in str(e)
    _err("QT-CONFIG-001", qlive.import_sim, "lean", [str(p) for p in SIM], tmp_path / "x", data_tz="UTC")


def _bad_event(i: int, **kw: Any) -> list[dict[str, Any]]:
    ev = _events()
    for k, v in kw.items():
        if v is None:
            ev[i].pop(k, None)
        else:
            ev[i][k] = v
    return ev


def _bad_order(oid: str, **kw: Any) -> dict[str, Any]:
    orders = _orders()
    for k, v in kw.items():
        if v is None:
            orders[oid].pop(k, None)
        else:
            orders[oid][k] = v
    return {"orders": orders}


def test_malformed(tmp_path: Path, w: Any) -> None:
    cases: dict[str, tuple[list[str], str]] = {
        "orders only": ([str(RESULT)], "no LEAN order events"),
        "status": ([str(RESULT), w(_bad_event(1, status="done"))], "OrderStatus"),
        "status number": ([str(RESULT), w(_bad_event(1, status=4))], "OrderStatus"),
        "no order id": ([str(RESULT), w(_bad_event(1, orderId=None))], "not LEAN backtest output"),
        "no event id": ([str(RESULT), w(_bad_event(1, orderEventId=None, id=None))], "orderEventId"),
        "fill sign": ([str(RESULT), w(_bad_event(1, fillQuantity=-0.01))], "contradicts"),
        "zero fill": ([str(RESULT), w(_bad_event(1, fillQuantity=0))], "no fill event"),
        "fill price": ([str(RESULT), w(_bad_event(1, fillPrice=0))], "fillPrice"),
        "time": ([str(RESULT), w(_bad_event(1, time="yesterday"))], "time"),
        "bool time": ([str(RESULT), w(_bad_event(1, time=True))], "time"),
        "exercise": ([w(_bad_order("1", type=6)), str(EVENTS)], "cannot be mapped"),
        "combo": ([w(_bad_order("1", type="ComboLimit")), str(EVENTS)], "cannot be mapped"),
        "type number": ([w(_bad_order("1", type=12)), str(EVENTS)], "OrderType"),
        "hold": ([w(_bad_order("1", direction=2)), str(EVENTS)], "Hold"),
        "qty sign": ([w(_bad_order("1", quantity=-0.01)), str(EVENTS)], "contradicts"),
        "no limit": ([w(_bad_order("1", limitPrice=None)), str(EVENTS)], "limitPrice"),
        "tif": ([w(_bad_order("1", properties={"timeInForce": "IOC"})), str(EVENTS)], "timeInForce"),
        "no type": ([w(_bad_order("1", type=None)), str(EVENTS)], "without type"),
        "filled no fill": (
            [str(RESULT), w([e for e in _events() if (e["orderId"], e["orderEventId"]) != (1, 2)])],
            "no fill event",
        ),
        "canceled no event": (
            [str(RESULT), w([e for e in _events() if (e["orderId"], e["orderEventId"]) != (7, 2)])],
            "no canceled event",
        ),
        "events only no type": (
            [w([{k: v for k, v in e.items() if k != "orderType"} for e in _events()])],
            "orderType",
        ),
        "not lean": ([w({"trades": []})], "not LEAN backtest output"),
        "scalar": ([w([1, 2])], "expected a LEAN result object"),
        "orders type": ([w({"orders": 3})], "orders must be"),
        "events type": ([w({"orders": {}, "orderEvents": {}})], "orderEvents must be an array"),
        "empty": ([_write(tmp_path, "empty.json", "  \n")], "empty input"),
        "csv": ([str(RESULT), _write(tmp_path, "e.csv", "a,b\n1,2\n")], "no orderId column"),
        "dup order": ([w({"orders": [_orders()["1"], _orders()["1"]]}), str(EVENTS)], "twice"),
    }
    for name, (paths, msg) in cases.items():
        with pytest.raises(QtError) as e:
            lean.read_lean(paths, {})
            pytest.fail(f"{name}: no error")
        assert e.value.code == "QT-INPUT-002", (name, str(e.value))
        assert msg in str(e.value), (name, str(e.value))


def test_what_lean_emits_beyond_the_happy_path(w: Any) -> None:
    """Outputs of LEAN 18241 the fixtures' main scenario does not show (review of the importer against its source)."""
    # an update refused by the brokerage model: `new OrderEvent(order, ...)` copies status Filled with no fill
    ev = _events()
    refused = dict(ev[1], orderEventId=3, id="qtfixture0001-1-3", fillQuantity=0.0, fillPrice=0.0)
    refused |= {"time": ev[1]["time"] + 60, "message": "BrokerageModel declared unable to update order"}
    ev.insert(2, refused)
    for k in ("orderFeeAmount", "orderFeeCurrency"):
        ev[2].pop(k)
    run = lean.read_lean([str(RESULT), w(ev)], {})
    assert len(run.orders[0].fills) == 1
    assert run.skipped == {"filled event without a fill quantity (an order update, no fill)": 1}
    # an order rounded to quantity 0 and invalidated, with and without the orders section
    orders = _orders()
    orders["8"] = dict(orders["1"], id=8, quantity=0.0, direction=2, status=7)
    zero = dict(ev[0], orderId=8, orderEventId=1, id="qtfixture0001-8-1", status="invalid", direction="hold",
                quantity=0.0, message="Unable to submit order with id 8 that has zero quantity.")  # fmt: skip
    for paths in ([w({"orders": orders}), w([*_events(), zero])], [w([*_events(), zero])]):
        r = lean.read_lean(paths, {})
        assert len(r.orders) == 7
        assert r.skipped["order LEAN rounded to quantity 0 and invalidated (below the lot size)"] == 1
    # events only: an update that grew the order from 0.01 to 0.02 (LEAN stamps every event with the current
    # quantity and prices): the last event's quantity and limit
    grown = _events()[:2]
    grown[1] = dict(grown[1], quantity=0.02, limitPrice=61010.0, fillQuantity=0.02, orderFeeAmount=None)
    g = lean.read_lean([w(grown)], {})
    assert (g.orders[0].qty, g.orders[0].limit_px, g.orders[0].filled) == (
        Decimal("0.02"), Decimal("61010.0"), Decimal("0.02"),
    )  # fmt: skip
    # timeInForce [] is GoodTilCanceled in LEAN's own reader
    o2 = _orders()
    o2["1"]["properties"] = {"timeInForce": []}
    assert lean.read_lean([w({"orders": o2}), str(EVENTS)], {}).orders[0].tif == "gtc"
    # a contingent order (OTO, OCO, bracket) is warned
    o3 = _orders()
    o3["2"]["contingency"] = {"id": 1, "count": 2, "links": [{"id": 1, "type": 1, "role": 1}], "orderIds": [1, 2]}
    assert any(
        x.startswith("1 contingent order(s)") for x in lean.read_lean([w({"orders": o3}), str(EVENTS)], {}).warnings
    )
    # a base-asset fee only on a buy of that base: a fee in another currency is left out
    other = _events()
    other[1]["orderFeeCurrency"] = "BNB"
    r = lean.read_lean([str(RESULT), w(other)], {})
    assert r.orders[0].fills[0].qty == Decimal("0.00999") and r.orders[0].fills[0].fee is None
    assert any(x.startswith("1 fill fee(s) in another currency") for x in r.warnings)
    # whole-second doubles take the file's precision
    ms = _events()
    ms[3]["time"] = ms[3]["time"] + 0.5
    r = lean.read_lean([str(RESULT), w(ms)], {})
    assert {f.precision for o in r.orders for f in o.fills} == {"ms"}
    # C# decimals are kept exactly, not rounded to 15 significant digits
    exact = _events()
    exact[7]["fillPrice"] = Decimal("61210.1234567890123")
    text = json.dumps(exact, default=str).replace('"61210.1234567890123"', "61210.1234567890123")
    r = lean.read_lean([str(RESULT), w(text)], {})
    assert r.orders[3].fills[0].px == Decimal("61210.1234567890123")


def _b36(n: int) -> str:
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def test_base_asset_fee_add_back_is_spot_crypto_only(w: Any) -> None:
    """LEAN subtracts a base-asset fee from fillQuantity for SecurityType.Crypto (7) only: an inverse crypto future
    (CryptoFuture, 11: BTCUSD with the fee in BTC) keeps its fill quantity, and so does a sell."""
    assert lean._sid_security_type("BTCUSDT 18N") == 7
    assert lean._sid_security_type(f"BTCUSD {_b36(1611)}") == 11
    assert lean._sid_security_type("BTCUSDT") is None and lean._sid_security_type("X ???") is None
    # an inverse future buy of 0.01 contracts, filled in full, the fee 0.00001 BTC: not added back
    orders = _orders()
    orders["1"]["securityType"] = 11
    ev = _events()
    ev[1] |= {"fillQuantity": 0.01, "fillPriceCurrency": "USD", "orderFeeCurrency": "BTC", "symbolValue": "BTCUSD"}
    ev[0]["symbolValue"] = "BTCUSD"
    orders["1"]["symbol"] = {"value": "BTCUSD", "id": f"BTCUSD {_b36(1611)}", "permtick": "BTCUSD"}
    r = lean.read_lean([w({"orders": orders}), w(ev)], {})
    f = r.orders[0].fills[0]
    assert (f.qty, f.fee) == (Decimal("0.01"), None)
    assert any(x.startswith("1 fill fee(s) in another currency") for x in r.warnings)
    # events only: the type comes from the security identifier
    fut = [dict(e, symbol=f"BTCUSD {_b36(1611)}") for e in ev[:2]]
    assert lean.read_lean([w(fut)], {}).orders[0].fills[0].qty == Decimal("0.01")
    spot = lean.read_lean([w(_events()[:2])], {})  # "BTCUSDT 18N": Crypto, added back
    assert spot.orders[0].fills[0].qty == Decimal("0.01") and spot.orders[0].fills[0].fee == Decimal("0.61")
    # an unknown security type: not added back
    unknown = [dict(e, symbol="BTCUSDT") for e in _events()[:2]]
    assert lean.read_lean([w(unknown)], {}).orders[0].fills[0].qty == Decimal("0.00999")
    # a sell charged in the base asset: LEAN does not subtract it, nor does the importer add it
    sell = _events()[2:4]
    sell[1] |= {"orderFeeAmount": 0.00001, "orderFeeCurrency": "BTC"}
    sr = lean.read_lean([w(sell)], {})
    assert (sr.orders[0].fills[0].qty, sr.orders[0].fills[0].fee) == (Decimal("0.01"), None)


def test_zero_fee_is_a_known_zero(w: Any) -> None:
    """LEAN's default fee model writes orderFeeAmount 0 in USD: a known zero fee; only currency QCC means none."""
    ev = _events()
    ev[3] |= {"orderFeeAmount": 0, "orderFeeCurrency": "USDT"}
    assert lean.read_lean([str(RESULT), w(ev)], {}).orders[1].fills[0].fee == Decimal(0)
    ev[3]["orderFeeCurrency"] = "QCC"
    assert lean.read_lean([str(RESULT), w(ev)], {}).orders[1].fills[0].fee is None


def test_append_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "s"
    qlive.import_sim("lean", [str(p) for p in SIM], out, instrument_map=str(IMAP), venue="binance")
    e = _err("QT-CONFIG-001", qlive.import_sim, "lean", [str(RESULT2), str(EVENTS2)], out, instrument_map=str(IMAP),
             venue="binance", append=True)  # fmt: skip
    assert "one log holds one backtest" in str(e)
    _err("QT-CONFIG-001", lean.import_lean, [str(p) for p in SIM], out, append=True)


# ---- pairing with a live log, reconcile ----------------------------------------------------------------------------


def test_import_sim_pairs_with_a_live_log(tmp_path: Path, live: Path) -> None:
    s = qlive.import_sim(
        "lean", [str(p) for p in SIM], tmp_path / "s", instrument_map=str(IMAP), pair_with=str(live), timeframe="1h"
    )
    assert any("6 of 7 simulator order(s) paired with an order of the live log" in w for w in s.warnings)
    orders, events = _rows(tmp_path / "s")
    keys = {o["order_key"] for o in orders}
    assert keys == {f"binance:main:{7100001 + i}" for i in range(6)} | {"sim:lean:main:7"}
    fill = next(e for e in events if e["order_key"] == "binance:main:7100001" and e["event_type"] == "fill")
    assert fill["ts_exchange"] == T0 + 10 * H + 5 * NS  # the bar start, clamped to the live entry 10:00:05
    assert "bar time 2024-03-01T10:00:00.000Z clamped" in fill["reason"]


def test_reconcile_end_to_end(tmp_path: Path, live: Path) -> None:
    rep = lean.reconcile_lean(SIM, live, tmp_path / "rec", timeframe="1h")
    assert {(r.live_key or r.sim_key): r.category for r in rep.orders} == CATEGORIES
    assert (rep.kind, rep.n_live_orders, rep.n_sim_orders, rep.n_paired) == ("lean", 7, 7, 6)
    sc = rep.score
    assert sc is not None and sc.two_by_two == {"both": 4, "neither": 0, "live_only": 0, "sim_only": 1}
    assert rep.missed_fill_notional == {"BTCUSDT": "608"} and rep.sim_price_advantage_quote == {"BTCUSDT": "0.1962"}
    assert "+20.0 pp (95% CI [+0.0, +60.0])" in sc.headline
    missed = next(r for r in rep.orders if r.category == "missed_fill")
    assert "the live order rested 59m28s and ended cancelled" in missed.explanation
    timing = next(r for r in rep.orders if r.category == "timing")
    assert "outside the backtest fill candle [2024-03-01T20:00:00.000Z, +1h)" in timing.explanation
    doc = json.loads((tmp_path / "rec" / "reconcile.json").read_text())
    assert ft.ReconcileReport.model_validate(doc) == rep and doc["kind"] == "lean"
    rep2 = lean.reconcile_lean(SIM, live, tmp_path / "x" / "rec2", timeframe="1h")
    assert (tmp_path / "rec" / "reconcile.json").read_bytes() == (
        tmp_path / "x" / "rec2" / "reconcile.json"
    ).read_bytes() and rep2.run_id == rep.run_id
    # the order-events CSV gives the same account
    csv_rep = lean.reconcile_lean([RESULT, ECSV], live, tmp_path / "csv", timeframe="1h")
    assert csv_rep.counts == rep.counts and csv_rep.n_paired == 6
    # without timeframe the bar-end stamps stay: the limit fills are an hour after the live fills' bars
    raw = lean.reconcile_lean(SIM, live, tmp_path / "raw", score=False, pair_window_ms=2 * 3_600_000)
    assert raw.n_paired == 6 and raw.counts["timing"] >= 1


def test_reconcile_refusals(tmp_path: Path, live: Path) -> None:
    _err("QT-CONFIG-001", lean.reconcile_lean, SIM, live, tmp_path / "a", pair_with=str(live))
    _err("QT-CONFIG-001", lean.reconcile_lean, SIM, live, tmp_path / "a", data_tz="UTC")
    _err("QT-INPUT-001", lean.reconcile_lean, [tmp_path / "none.json"], live, tmp_path / "a")
    e = _err("QT-INPUT-001", lean.reconcile_lean, SIM, FIX / "binance_user_stream.jsonl", tmp_path / "a")
    assert "canonical live-log directory" in str(e)
    sim_log = tmp_path / "simlog"
    qlive.import_sim("lean", [str(p) for p in SIM], sim_log, instrument_map=str(IMAP), venue="binance")
    e = _err("QT-CONFIG-001", lean.reconcile_lean, SIM, sim_log, tmp_path / "b", score=False)
    assert "arms swapped?" in str(e)


def test_import_live_api_spec_and_helpers(tmp_path: Path) -> None:
    with pytest.warns(qlive.LiveImportWarning, match="unmapped_instruments"):  # no instrument map
        s = api.import_live(
            {"venue": "sim_lean", "inputs": [{"path": str(RESULT)}, {"path": str(EVENTS)}], "out": str(tmp_path / "v")}
        )
    assert s.venue == "sim:lean" and s.validation_status == "simulated" and s.n_orders == 7
    a = lean.import_lean([str(p) for p in SIM], tmp_path / "a", instrument_map=str(IMAP), venue="binance")
    b = qlive.import_sim("lean", [str(p) for p in SIM], tmp_path / "b", instrument_map=str(IMAP), venue="binance")
    assert _rows(tmp_path / "a") == _rows(tmp_path / "b") and a.n_events == b.n_events
    res = lean.import_lean_sim([str(p) for p in SIM], ImportOptions(extra={"venue": "binance"}))
    assert res.venue == "sim:lean" and len(res.orders) == 7
    _err("QT-INPUT-001", lean.import_lean, [str(tmp_path / "missing.json")], tmp_path / "c")
