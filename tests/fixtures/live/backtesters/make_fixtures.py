"""Writes the Backtrader and VectorBT fixtures of this directory (deterministic; run from anywhere):

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
- `vbt_orders.csv`, `vbt_orders.json`, `vbt_trades.csv`: `pf.orders.records_readable.to_csv(...)`, the same
  records with `.to_json(..., orient="records")` (epoch milliseconds, pandas' default) and
  `pf.trades.records_readable.to_csv(...)` (VectorBT 0.28.1 column titles). The market sell carries slippage, so
  its price is not on the 0.01 tick.

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
        order_line(ref, "Submitted", b, created, [])
        order_line(ref, "Accepted", b, created, [])
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


if __name__ == "__main__":
    main()
