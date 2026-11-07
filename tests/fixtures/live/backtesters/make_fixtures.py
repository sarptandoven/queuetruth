"""Writes the Backtrader, VectorBT and LEAN fixtures of this directory (deterministic; run from anywhere):

    python tests/fixtures/live/backtesters/make_fixtures.py

One invented BTCUSDT strategy on 1-hour bars (labelled by their open time, 2024-03-01 UTC) runs live on Binance and
as a backtest in each library. Outputs:

- `binance_user_stream.jsonl`: the live run as captured Binance user-data stream `executionReport` messages, each
  wrapped as `{"recv_ns": ..., "msg": ...}` (the live arm, imported with `queuetruth live import --venue binance`).
  Seven orders: four filled, one cancelled unfilled, one cancelled after a partial fill, one still open.
- `instrument_map.csv`: BTCUSDT with tick 0.01 and lot 0.00001.
- `bt_orders.jsonl`: the backtest's order and trade notifications as the `QtOrderLog` analyzer of
  docs/how-to/reconcile-backtrader-vectorbt.md writes them (Backtrader 1.9.78.123 `Order` / `Trade` fields): each
  order is created on the bar before it executes (`BackBroker` fills on the next bar), one order expires unfilled.
- `bt_transactions.json`: the same backtest's `bt.analyzers.Transactions` result dumped as JSON.
- `bt_orders_run2.jsonl`: a second Backtrader run (refs restart at 1): a fill over two notifications and three
  execution bits, a cancelled limit, a triggered stop, a cancelled stop-limit and a `Margin` refusal at submission.
  Submitted and Accepted come on the bar after `created`, as `BackBroker` delivers them.
- `vbt_sweep_orders.csv`: a two-parameter sweep with MultiIndex column labels; `vbt_orders_naive_ny.json`: the
  order records of a portfolio with a naive New York index, as `to_json` writes them.
- `vbt_orders.csv`, `vbt_orders.json`, `vbt_trades.csv`: `pf.orders.records_readable.to_csv(...)`, the same
  records with `.to_json(..., orient="records")` (epoch milliseconds, pandas' default) and
  `pf.trades.records_readable.to_csv(...)` (VectorBT 0.28.1 column titles). The market sell carries slippage, so
  its price is not on the 0.01 tick.
- `lean_result.json`, `lean_order_events.json`: the same backtest in QuantConnect LEAN (release tag 18241), as a
  local run writes it: the result JSON's `orders` section (`Order`, camelCase, enums as numbers) and the
  `<algorithm id>-order-events.json` array (`SerializedOrderEvent`: Unix-second times, camelCase enum names). LEAN
  stamps events with the end of the bar, so each order is submitted an hour after Backtrader creates it and a limit
  fill comes on the next bar's end; the market sell fills on the bar it is placed on. The expiring order is
  cancelled by its GoodTilDate time in force. `lean_order_events.csv`: the same events as a CSV with the JSON names.
- `lean_run2_result.json`, `lean_run2_order_events.json`: a second LEAN backtest earlier that day, its events with
  the C# property names of `OrderEvent` (enums as numbers, `UtcTime` text, `OrderFee` objects): a limit with two
  partial fills, an updated then cancelled limit, a stop market, a cancelled stop-limit, a refused (Invalid) order,
  a trailing stop and a market-on-open order.

Every number is invented; none comes from market data.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAY = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
MS = int(DAY.timestamp() * 1000)
H = 3_600_000


def _ms(hour: float, extra_ms: int = 0) -> int:
    return MS + int(hour * H) + extra_ms


# ---- the live run (Binance) ---------------------------------------------------------------------------------------

# (order id, side, type, qty, price, placed ms, status, executed qty, [(trade id, fill ms, qty, price, maker)], end ms)
LIVE = [
    (7100001, "BUY", "LIMIT", "0.01", "61000.00", _ms(10, 5_000), "FILLED", "0.01",
     [(91000001, _ms(10, 1_200_000), "0.01", "61000.00", True)], _ms(10, 1_200_000)),
    (7100002, "SELL", "LIMIT", "0.01", "61500.00", _ms(12, 3_000), "FILLED", "0.01",
     [(91000002, _ms(12, 2_110_000), "0.01", "61500.00", True)], _ms(12, 2_110_000)),
    (7100003, "BUY", "LIMIT", "0.01", "60800.00", _ms(14, 2_000), "CANCELED", "0", [], _ms(14, 3_570_000)),
    (7100004, "SELL", "MARKET", "0.01", "0", _ms(16, 1_000), "FILLED", "0.01",
     [(91000003, _ms(16, 1_200), "0.01", "61190.50", False)], _ms(16, 1_200)),
    (7100005, "BUY", "LIMIT", "0.02", "60900.00", _ms(18, 4_000), "CANCELED", "0.008",
     [(91000004, _ms(18, 2_460_000), "0.008", "60900.00", True)], _ms(18, 3_540_000)),
    (7100006, "BUY", "LIMIT", "0.01", "61100.00", _ms(20, 2_000), "FILLED", "0.01",
     [(91000005, _ms(21, 1_800_000), "0.01", "61100.00", True)], _ms(21, 1_800_000)),
    (7100007, "SELL", "LIMIT", "0.01", "61800.00", _ms(22, 1_800_000), "NEW", "0", [], _ms(22, 1_800_000)),
]  # fmt: skip


def _q8(s: str) -> str:
    whole, _, frac = s.partition(".")
    return f"{whole}.{(frac + '0' * 8)[:8]}"


def _report(
    oid: int,
    side: str,
    typ: str,
    qty: str,
    px: str,
    placed: int,
    x: str,
    status: str,
    at: int,
    cum: str,
    last: tuple[int, str, str, bool] | None,
) -> str:
    tid, lq, lp, maker = last if last is not None else (-1, "0", "0", False)
    msg = {
        "e": "executionReport", "E": at + 1, "s": "BTCUSDT", "c": f"qtBT-{oid}", "S": side, "o": typ,
        "f": "GTC", "q": _q8(qty), "p": _q8(px), "P": "0.00000000", "F": "0.00000000", "g": -1, "C": "",
        "x": x, "X": status, "r": "NONE", "i": oid, "l": _q8(lq), "z": _q8(cum), "L": _q8(lp),
        "n": f"{float(lq) * float(lp) * 0.001:.8f}" if last else "0", "N": "USDT" if last else None, "T": at,
        "t": tid, "I": oid * 10 + (tid % 7 if last else 0), "w": True, "m": maker, "M": False, "O": placed,
        "Z": "0.00000000", "Y": "0.00000000", "Q": "0.00000000", "W": placed, "V": "EXPIRE_MAKER",
    }  # fmt: skip
    return json.dumps({"recv_ns": (at + 1) * 1_000_000 + 250_000, "msg": msg}, separators=(",", ":"))


def binance() -> str:
    """The live run as captured user-data stream `executionReport` messages (receive time in the wrapper)."""
    lines: list[tuple[int, str]] = []
    for oid, side, typ, qty, px, placed, status, _executed, fills, end in LIVE:
        lines.append((placed, _report(oid, side, typ, qty, px, placed, "NEW", "NEW", placed, "0", None)))
        cum = 0.0
        for tid, ts, q, p, maker in fills:
            cum += float(q)
            done = abs(cum - float(qty)) < 1e-12
            st = "FILLED" if done else "PARTIALLY_FILLED"
            lines.append((ts, _report(oid, side, typ, qty, px, placed, "TRADE", st, ts, repr(round(cum, 8)),
                                      (tid, q, p, maker))))  # fmt: skip
        if status == "CANCELED":
            lines.append((end, _report(oid, side, typ, qty, px, placed, "CANCELED", "CANCELED", end,
                                       repr(round(cum, 8)) if cum else "0", None)))  # fmt: skip
    lines.sort(key=lambda x: x[0])
    return "\n".join(line for _, line in lines) + "\n"


# ---- the backtest -------------------------------------------------------------------------------------------------

# (side, exectype, size, price, created hour, fill hour or None, fill price, end) shared by both libraries: Backtrader
# creates the order on one bar and fills it on the next; VectorBT records the fill on that next bar.
BACKTEST = [
    ("Buy", "Limit", 0.01, 61000.0, 9, 10, 61000.0, None),
    ("Sell", "Limit", 0.01, 61500.0, 11, 12, 61500.0, None),
    ("Buy", "Limit", 0.01, 60800.0, 13, 14, 60800.0, None),
    ("Sell", "Market", 0.01, None, 15, 16, 61210.123456789, None),
    ("Buy", "Limit", 0.02, 60900.0, 17, 18, 60900.0, None),
    ("Buy", "Limit", 0.01, 61100.0, 19, 20, 61100.0, None),
    ("Buy", "Limit", 0.01, 61300.0, 22, None, None, "Expired"),
]
COMM = 0.001


def _iso(hour: float) -> str:
    return (DAY + dt.timedelta(hours=hour)).replace(tzinfo=None).isoformat()


def backtrader() -> tuple[str, str]:
    lines: list[str] = []
    txn: dict[str, list[list[object]]] = {}

    def order_line(ref: int, status: str, b: tuple, at: float, bits: list[dict[str, object]]) -> None:
        side, ex, size, price, created, *_ = b
        signed = size if side == "Buy" else -size
        lines.append(
            json.dumps(
                {
                    "event": "order",
                    "ref": ref,
                    "data": "BTCUSDT",
                    "status": status,
                    "ordtype": side,
                    "exectype": ex,
                    "size": signed,
                    "price": price if price is not None else 61200.0,  # Backtrader keeps the close as reference
                    "pricelimit": price if price is not None else 61200.0,
                    "created": _iso(created),
                    "dt": _iso(at),
                    "valid": _iso(23) if b[7] == "Expired" else None,
                    "executed_size": sum(x["size"] for x in bits),  # type: ignore[misc]
                    "executed_price": bits[-1]["price"] if bits else 0.0,
                    "comm": sum(x["comm"] for x in bits),  # type: ignore[misc]
                    "exbits": bits,
                    "order_key": None,
                }
            )
        )

    for ref, b in enumerate(BACKTEST, start=1):
        side, _ex, size, _price, created, fill_h, fill_px, end = b
        # BackBroker delivers Submitted and Accepted at the next bar's _brokernotify, then executes on that bar
        order_line(ref, "Submitted", b, created + 1, [])
        order_line(ref, "Accepted", b, created + 1, [])
        if fill_h is not None:
            signed = size if side == "Buy" else -size
            comm = abs(signed) * fill_px * COMM
            bits = [{"dt": _iso(fill_h), "size": signed, "price": fill_px, "comm": comm}]
            order_line(ref, "Completed", b, fill_h, bits)
            txn.setdefault(_iso(fill_h), []).append([signed, fill_px, 0, "BTCUSDT", -signed * fill_px])
        else:
            order_line(ref, str(end), b, 23, [])
    # trade notifications: open at the first buy, closed by the sell; again for the second round trip
    for ref, status, d_open, d_close, pnl in (
        (1, "Open", 10, None, 0.0),
        (1, "Closed", 10, 12, 3.7849999999999997),
        (2, "Open", 14, None, 0.0),
        (2, "Closed", 14, 16, 2.8889012345678),
    ):
        lines.append(
            json.dumps(
                {
                    "event": "trade",
                    "ref": ref,
                    "data": "BTCUSDT",
                    "status": status,
                    "dtopen": _iso(d_open),
                    "dtclose": None if d_close is None else _iso(d_close),
                    "size": 0.01 if status == "Open" else 0.0,
                    "price": 61000.0 if ref == 1 else 60800.0,
                    "pnl": pnl,
                    "pnlcomm": pnl,
                }
            )
        )
    return "\n".join(lines) + "\n", json.dumps(txn, indent=2) + "\n"


def _line(
    ref: int,
    status: str,
    side: str,
    ex: str,
    size: float,
    price: float,
    plimit: float,
    created: float,
    at: float,
    bits: list[tuple[float, float, float]],
) -> str:
    signed = size if side == "Buy" else -size
    exbits = [{"dt": _iso(h), "size": q if side == "Buy" else -q, "price": p, "comm": q * p * COMM} for h, q, p in bits]
    return json.dumps({
        "event": "order", "ref": ref, "data": "BTCUSDT", "status": status, "ordtype": side, "exectype": ex,
        "size": signed, "price": price, "pricelimit": plimit, "created": _iso(created), "dt": _iso(at),
        "valid": None, "exbits": exbits, "order_key": None,
    })  # fmt: skip


def backtrader_second_run() -> str:
    """A second Backtrader run (refs restart at 1) earlier that day: a limit buy filled over two notifications and
    three execution bits, a cancelled limit sell, a triggered stop, a cancelled stop-limit and an order refused at
    submission for lack of cash (Submitted, then Margin, never Accepted)."""
    rows = [
        _line(1, "Submitted", "Buy", "Limit", 0.03, 60500.0, 60500.0, 1, 2, []),
        _line(1, "Accepted", "Buy", "Limit", 0.03, 60500.0, 60500.0, 1, 2, []),
        _line(1, "Partial", "Buy", "Limit", 0.03, 60500.0, 60500.0, 1, 2, [(2, 0.01, 60500.0), (2, 0.005, 60500.0)]),
        _line(1, "Completed", "Buy", "Limit", 0.03, 60500.0, 60500.0, 1, 3, [(3, 0.015, 60500.0)]),
        _line(2, "Submitted", "Sell", "Limit", 0.01, 62500.0, 62500.0, 1, 2, []),
        _line(2, "Accepted", "Sell", "Limit", 0.01, 62500.0, 62500.0, 1, 2, []),
        _line(2, "Canceled", "Sell", "Limit", 0.01, 62500.0, 62500.0, 1, 5, []),
        _line(3, "Submitted", "Sell", "Stop", 0.01, 60000.0, 60000.0, 2, 3, []),
        _line(3, "Accepted", "Sell", "Stop", 0.01, 60000.0, 60000.0, 2, 3, []),
        _line(3, "Completed", "Sell", "Stop", 0.01, 60000.0, 60000.0, 2, 4, [(4, 0.01, 59990.0)]),
        _line(4, "Submitted", "Buy", "StopLimit", 0.01, 61000.0, 61050.0, 2, 3, []),
        _line(4, "Accepted", "Buy", "StopLimit", 0.01, 61000.0, 61050.0, 2, 3, []),
        _line(4, "Canceled", "Buy", "StopLimit", 0.01, 61000.0, 61050.0, 2, 5, []),
        _line(5, "Submitted", "Buy", "Limit", 5.0, 60000.0, 60000.0, 3, 4, []),
        _line(5, "Margin", "Buy", "Limit", 5.0, 60000.0, 60000.0, 3, 4, []),
    ]
    return "\n".join(rows) + "\n"


def vectorbt_sweep() -> str:
    """A two-parameter sweep (`fast` window 10 and 20) on BTCUSDT: MultiIndex column labels as pandas writes them."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["", "Order Id", "Column", "Timestamp", "Size", "Price", "Fees", "Side"])
    rows = [
        (0, "(10, 'BTCUSDT')", 10, 0.01, 61000.0, "Buy"), (1, "(10, 'BTCUSDT')", 12, 0.01, 61500.0, "Sell"),
        (2, "(20, 'BTCUSDT')", 14, 0.01, 60800.0, "Buy"), (3, "(20, 'BTCUSDT')", 16, 0.01, 61200.0, "Sell"),
    ]  # fmt: skip
    for i, (o, col, h, size, px, side) in enumerate(rows):
        w.writerow([i, o, col, f"{_iso(h).replace('T', ' ')}+00:00", size, px, repr(size * px * COMM), side])
    return buf.getvalue()


def vectorbt() -> tuple[str, str, str]:
    rows = []
    oid = 0
    for side, _ex, size, _price, created, fill_h, fill_px, _end in BACKTEST:
        if fill_h is None:
            continue  # VectorBT records executions only
        at = created + 1  # the bar whose close price the decision fills at
        rows.append((oid, "BTCUSDT", at, size, fill_px, size * fill_px * COMM, side))
        oid += 1
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["", "Order Id", "Column", "Timestamp", "Size", "Price", "Fees", "Side"])
    for i, (o, col, h, size, px, fee, side) in enumerate(rows):
        w.writerow([i, o, col, f"{_iso(h).replace('T', ' ')}+00:00", repr(size), repr(px), repr(fee), side])
    js = [
        {"Order Id": o, "Column": col, "Timestamp": _ms(h), "Size": size, "Price": px, "Fees": fee, "Side": side}
        for o, col, h, size, px, fee, side in rows
    ]
    tbuf = io.StringIO()
    t = csv.writer(tbuf, lineterminator="\n")
    t.writerow(
        ["", "Exit Trade Id", "Column", "Size", "Entry Timestamp", "Avg Entry Price", "Entry Fees", "Exit Timestamp",
         "Avg Exit Price", "Exit Fees", "PnL", "Return", "Direction", "Status", "Position Id"]
    )  # fmt: skip
    trades = [
        (0, 0.01, 10, 61000.0, 0.61, 12, 61500.0, 0.615, 3.775, 0.006188, "Closed", 0),
        (1, 0.01, 14, 60800.0, 0.608, 16, 61210.123456789, 0.61210123456789, 2.8889, 0.004751, "Closed", 1),
        (2, 0.03, 18, 60966.666666666664, 1.829, 20, 61150.0, 0.0, 3.671, 0.002007, "Open", 2),
    ]
    for i, (tid, size, eh, ep, ef, xh, xp, xf, pnl, ret, st, pid) in enumerate(trades):
        t.writerow(
            [i, tid, "BTCUSDT", size, f"{_iso(eh).replace('T', ' ')}+00:00", ep, ef,
             f"{_iso(xh).replace('T', ' ')}+00:00", xp, xf, pnl, ret, "Long", st, pid]
        )  # fmt: skip
    return buf.getvalue(), json.dumps(js) + "\n", tbuf.getvalue()


def vectorbt_naive_json() -> str:
    """The order records of a portfolio indexed by naive New York wall-clock times, as `to_json` writes them: epoch
    milliseconds of the wall-clock time read as UTC (10:00 UTC is 05:00 EST on 2024-03-01)."""
    rows = json.loads(vectorbt()[1])
    for r in rows:
        r["Timestamp"] -= 5 * H
    return json.dumps(rows) + "\n"


# ---- LEAN ----------------------------------------------------------------------------------------------------------

LEAN_ALGO = "qtfixture0001"
LEAN_SID = "BTCUSDT 18N"
#: LEAN `OrderType`, `OrderStatus` and `OrderDirection` numbers (Common/Orders/OrderTypes.cs)
LEAN_TYPE = {"market": 0, "limit": 1, "stopMarket": 2, "stopLimit": 3, "marketOnOpen": 4, "trailingStop": 11}
LEAN_STATUS = {"submitted": 1, "partiallyFilled": 2, "filled": 3, "canceled": 5, "invalid": 7, "updateSubmitted": 9}


def _utc(hour: float) -> str:
    return (DAY + dt.timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unix(hour: float) -> float:
    return (DAY + dt.timedelta(hours=hour)).timestamp()


def _lean_order(
    oid: int,
    typ: str,
    qty: float,
    status: str,
    hour: float,
    *,
    limit: float | None = None,
    stop: float | None = None,
    tif: dict[str, str] | None = None,
    last_fill: float | None = None,
    price: float = 0.0,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """One `Order` of the result JSON's `orders` section (camelCase names, enums as numbers)."""
    o: dict[str, object] = {"type": LEAN_TYPE[typ]}
    if limit is not None:
        o["limitPrice"] = limit
    if stop is not None:
        o["stopPrice"] = stop
        o["stopTriggered"] = status == "filled"
    o |= extra or {}
    o |= {
        "id": oid, "contingentId": 0, "brokerId": [],
        "symbol": {"value": "BTCUSDT", "id": LEAN_SID, "permtick": "BTCUSDT"},
        "price": price, "priceCurrency": "USDT", "time": _utc(hour), "createdTime": _utc(hour),
    }  # fmt: skip
    if last_fill is not None:
        o["lastFillTime"] = _utc(last_fill)
    o |= {
        "quantity": qty, "status": LEAN_STATUS[status], "properties": {"timeInForce": tif or {}},
        "securityType": 7, "direction": 0 if qty > 0 else 1, "value": abs(qty) * price,
        "isMarketable": False, "priceAdjustmentMode": 0,
    }  # fmt: skip
    return o


def _lean_event(
    oid: int,
    eid: int,
    typ: str,
    qty: float,
    status: str,
    hour: float,
    *,
    fill: tuple[float, float] | None = None,
    limit: float | None = None,
    stop: float | None = None,
    message: str = "",
) -> dict[str, object]:
    """One `SerializedOrderEvent` of the order-events file."""
    e: dict[str, object] = {
        "id": f"{LEAN_ALGO}-{oid}-{eid}", "algorithmId": LEAN_ALGO, "orderId": oid, "orderEventId": eid,
        "symbol": LEAN_SID, "symbolValue": "BTCUSDT", "symbolPermtick": "BTCUSDT", "time": _unix(hour),
        "status": status,
    }  # fmt: skip
    fq, fp = fill if fill is not None else (0.0, 0.0)
    if fill is not None:
        e |= {"orderFeeAmount": abs(fq) * fp * COMM, "orderFeeCurrency": "USDT"}
    e |= {"fillPrice": fp, "fillPriceCurrency": "USDT", "fillQuantity": fq, "direction": "buy" if qty > 0 else "sell",
          "orderType": typ}  # fmt: skip
    if message:
        e["message"] = message
    e |= {"isAssignment": False, "quantity": qty}
    if stop is not None:
        e["stopPrice"] = stop
    if limit is not None:
        e["limitPrice"] = limit
    return e


def lean() -> tuple[str, str, str]:
    """The backtest of `BACKTEST` in LEAN: (result JSON, order-events JSON, order-events CSV)."""
    orders: dict[str, object] = {}
    events: list[dict[str, object]] = []
    for oid, (side, ex, size, price, created, fill_h, fill_px, end) in enumerate(BACKTEST, start=1):
        typ = "market" if ex == "Market" else "limit"
        qty = size if side == "Buy" else -size
        sub = created + 1  # LEAN acts at the end of the bar Backtrader labels by its open
        # a resting order fills at the end of the next bar; a market order on the bar it is placed on
        at = None if fill_h is None else (sub if typ == "market" else fill_h + 1)
        tif = {"$type": "QuantConnect.Orders.TimeInForces.GoodTilDateTimeInForce", "expiry": _utc(25)} if end else None
        status = "filled" if at is not None else "canceled"
        o = _lean_order(oid, typ, qty, status, sub, limit=price, tif=tif, last_fill=at,
                        price=fill_px if fill_px is not None else price or 0.0)  # fmt: skip
        if end:
            o["canceledTime"] = _utc(25)
        orders[str(oid)] = o
        events.append(_lean_event(oid, 1, typ, qty, "submitted", sub, limit=price))
        if at is not None:
            assert fill_px is not None
            events.append(_lean_event(oid, 2, typ, qty, "filled", at, fill=(qty, fill_px), limit=price))
        else:
            events.append(_lean_event(oid, 2, typ, qty, "canceled", 25, limit=price,
                                      message="GoodTilDate time in force expired"))  # fmt: skip
    result = {
        "orders": orders,
        "statistics": {"Total Orders": str(len(orders))},
        "algorithmConfiguration": {"name": "QtFixtureAlgorithm", "accountCurrency": "USDT"},
    }
    buf = io.StringIO()
    cols: list[str] = []
    for e in events:
        cols += [k for k in e if k not in cols]
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for e in events:
        w.writerow({k: ("true" if v is True else "false" if v is False else v) for k, v in e.items()})
    return json.dumps(result, indent=2) + "\n", json.dumps(events) + "\n", buf.getvalue()


def _pascal(e: dict[str, object]) -> dict[str, object]:
    """A `SerializedOrderEvent` with the C# property names of `OrderEvent`: enums as numbers, `UtcTime` text,
    `OrderFee` as `{Value: {Amount, Currency}}`, `Symbol` as a symbol object."""
    t = dt.datetime.fromtimestamp(float(e["time"]), dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # type: ignore[arg-type]
    out: dict[str, object] = {
        "OrderId": e["orderId"], "Id": e["orderEventId"],
        "Symbol": {"Value": e["symbolValue"], "ID": e["symbol"], "Permtick": e["symbolPermtick"]},
        "UtcTime": t, "Status": LEAN_STATUS[str(e["status"])],
        "OrderFee": {"Value": {"Amount": e.get("orderFeeAmount", 0.0), "Currency": e.get("orderFeeCurrency", "USDT")}},
        "FillPrice": e["fillPrice"], "FillPriceCurrency": "USDT", "FillQuantity": e["fillQuantity"],
        "Direction": 0 if e["direction"] == "buy" else 1, "OrderType": LEAN_TYPE[str(e["orderType"])],
        "Message": e.get("message", ""), "IsAssignment": False, "Quantity": e["quantity"],
    }  # fmt: skip
    for k in ("stopPrice", "limitPrice"):
        if k in e:
            out[k[0].upper() + k[1:]] = e[k]
    return out


def lean_second_run() -> tuple[str, str]:
    """A second LEAN backtest earlier that day (order ids restart at 1), its events with the C# property names."""
    day = {"$type": "QuantConnect.Orders.TimeInForces.DayTimeInForce"}
    gtd = {"$type": "QuantConnect.Orders.TimeInForces.GoodTilDateTimeInForce", "expiry": _utc(5)}
    orders = [
        _lean_order(1, "limit", 0.03, "filled", 2, limit=60500.0, tif=day, last_fill=4, price=60500.0),
        _lean_order(2, "limit", -0.01, "canceled", 2, limit=62400.0, extra={}),
        _lean_order(3, "stopMarket", -0.01, "filled", 3, stop=60000.0, last_fill=4, price=59990.0),
        _lean_order(4, "stopLimit", 0.01, "canceled", 3, stop=61000.0, limit=61050.0, tif=gtd),
        _lean_order(5, "limit", 5.0, "invalid", 4, limit=60000.0),
        _lean_order(6, "trailingStop", -0.01, "filled", 4, stop=59000.0, last_fill=5, price=59100.0,
                    extra={"trailingAmount": 500.0, "trailingAsPercentage": False}),
        _lean_order(7, "marketOnOpen", 0.01, "filled", 4, last_fill=5, price=60100.0),
    ]  # fmt: skip
    ev = _lean_event
    events = [
        ev(1, 1, "limit", 0.03, "submitted", 2, limit=60500.0),
        ev(1, 2, "limit", 0.03, "partiallyFilled", 3, fill=(0.01, 60500.0), limit=60500.0),
        ev(1, 3, "limit", 0.03, "partiallyFilled", 3, fill=(0.005, 60500.0), limit=60500.0),
        ev(1, 4, "limit", 0.03, "filled", 4, fill=(0.015, 60500.0), limit=60500.0),
        ev(2, 1, "limit", -0.01, "submitted", 2, limit=62500.0),
        ev(2, 2, "limit", -0.01, "updateSubmitted", 3, limit=62400.0),
        ev(2, 3, "limit", -0.01, "canceled", 5, limit=62400.0, message="Canceled by the algorithm"),
        ev(3, 1, "stopMarket", -0.01, "submitted", 3, stop=60000.0),
        ev(3, 2, "stopMarket", -0.01, "filled", 4, fill=(-0.01, 59990.0), stop=60000.0),
        ev(4, 1, "stopLimit", 0.01, "submitted", 3, stop=61000.0, limit=61050.0),
        ev(4, 2, "stopLimit", 0.01, "canceled", 5, stop=61000.0, limit=61050.0,
           message="GoodTilDate time in force expired"),
        ev(5, 1, "limit", 5.0, "invalid", 4, limit=60000.0,
           message="Order Error: id: 5, Insufficient buying power to complete order"),
        ev(6, 1, "trailingStop", -0.01, "submitted", 4, stop=59000.0),
        ev(6, 2, "trailingStop", -0.01, "filled", 5, fill=(-0.01, 59100.0), stop=59000.0),
        ev(7, 1, "marketOnOpen", 0.01, "submitted", 4),
        ev(7, 2, "marketOnOpen", 0.01, "filled", 5, fill=(0.01, 60100.0)),
    ]  # fmt: skip
    result = {"orders": {str(o["id"]): o for o in orders}}
    return json.dumps(result, indent=2) + "\n", json.dumps([_pascal(e) for e in events], indent=1) + "\n"


IMAP = (
    "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\n"
    "binance,BTCUSDT,1,,-2,-5,0.01,0.00001\n"
)


def main() -> None:
    (HERE / "binance_user_stream.jsonl").write_text(binance())
    (HERE / "instrument_map.csv").write_text(IMAP)
    bt_lines, bt_txn = backtrader()
    (HERE / "bt_orders.jsonl").write_text(bt_lines)
    (HERE / "bt_transactions.json").write_text(bt_txn)
    v_csv, v_json, v_trades = vectorbt()
    (HERE / "vbt_orders.csv").write_text(v_csv)
    (HERE / "vbt_orders.json").write_text(v_json)
    (HERE / "vbt_trades.csv").write_text(v_trades)
    (HERE / "bt_orders_run2.jsonl").write_text(backtrader_second_run())
    (HERE / "vbt_sweep_orders.csv").write_text(vectorbt_sweep())
    (HERE / "vbt_orders_naive_ny.json").write_text(vectorbt_naive_json())
    l_result, l_events, l_csv = lean()
    (HERE / "lean_result.json").write_text(l_result)
    (HERE / "lean_order_events.json").write_text(l_events)
    (HERE / "lean_order_events.csv").write_text(l_csv)
    l2_result, l2_events = lean_second_run()
    (HERE / "lean_run2_result.json").write_text(l2_result)
    (HERE / "lean_run2_order_events.json").write_text(l2_events)


if __name__ == "__main__":
    main()
