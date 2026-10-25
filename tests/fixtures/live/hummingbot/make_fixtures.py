"""Writes the Hummingbot fixtures of this directory (deterministic; run from anywhere):

    python tests/fixtures/live/hummingbot/make_fixtures.py

Two `pure_market_making` bots on BTC-USDT run side by side for two minutes: a live one (market `binance`) and a
paper-trade one (market `binance_PaperTrade`), both refreshing a 0.001 BTC bid and ask every 30 s. Outputs:

- `conf_pure_mm_1.sql` and `conf_pure_mm_1_paper.sql`: the bots' databases (`data/<config>.sqlite`) as SQL, tables
  `Order`, `OrderStatus` and `TradeFill` as Hummingbot's SQLAlchemy models declare them (`hummingbot/model/`,
  commit 9af100d6822d): `price`, `amount` and `trade_fee_in_quote` are `SqliteDecimal(6)` integers (value * 10^6),
  times are epoch ms, `trade_fee` is `TradeFeeBase.to_json()` JSON. Build a database with
  `python -c "import sqlite3,sys; sqlite3.connect(sys.argv[2]).executescript(open(sys.argv[1]).read())" X.sql X.sqlite`.
- `trades_conf_pure_mm_1.csv`: the live bot's trades CSV (`MarketsRecorder.append_to_csv`: the
  `TradeFill.attribute_names_for_file_export` columns plus `age`, `trade_fee` written as a Python dict by pandas).

The orders and fills are invented for the reconciliation scenario of docs/how-to/reconcile-freqtrade-hummingbot.md.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = "conf_pure_mm_1.yml"
T0 = int(dt.datetime(2024, 3, 1, 10, 0, tzinfo=dt.UTC).timestamp() * 1000)

# (client id suffix, side, price, created ms offset, exchange order id, [(fill ms, qty, trade id)], end status, end ms)
LIVE = [
    ("1", "BUY", "61990.0", 120, "28457312001", [(12_402, "0.001", "3051001")], "completed", 12_402),
    ("2", "SELL", "62010.0", 121, "28457312002", [], "cancelled", 30_105),
    ("3", "BUY", "62000.5", 30_110, "28457312045", [], "cancelled", 60_101),
    ("4", "SELL", "62020.5", 30_111, "28457312046", [], "cancelled", 60_103),
    ("5", "BUY", "61980.0", 60_115, "28457312090", [(78_300, "0.0004", "3051088")], "cancelled", 90_110),
    (
        "6",
        "SELL",
        "62000.0",
        60_116,
        "28457312091",
        [(63_500, "0.0006", "3051042"), (69_020, "0.0004", "3051051")],
        "completed",
        69_020,
    ),
    ("7", "BUY", "61975.0", 90_120, "28457312133", [], "cancelled", 120_100),
    ("8", "SELL", "61995.0", 90_121, "28457312134", [(101_700, "0.001", "3051140")], "completed", 101_700),
    ("9", "SELL", "MARKET", 125_000, "28457312170", [(125_180, "0.0005", "3051177")], "completed", 125_180),
]
PAPER = [
    ("1", "BUY", "61990.0", 118, None, [(5_250, "0.001", "1709287205250000")], "completed", 5_250),
    ("2", "SELL", "62010.0", 119, None, [(20_880, "0.001", "1709287220880000")], "completed", 20_880),
    ("3", "BUY", "62000.5", 30_095, None, [(44_004, "0.001", "1709287244004000")], "completed", 44_004),
    ("4", "SELL", "62020.5", 30_096, None, [], "cancelled", 60_090),
    ("5", "BUY", "61980.0", 60_097, None, [(85_640, "0.001", "1709287285640000")], "completed", 85_640),
    ("6", "SELL", "62000.0", 60_098, None, [(61_200, "0.001", "1709287261200000")], "completed", 61_200),
    ("7", "BUY", "61975.0", 90_300, None, [], "cancelled", 120_090),
    ("8", "SELL", "61995.0", 90_301, None, [], "failure", 90_301),
    ("9", "BUY", "61960.0", 120_300, None, [], "cancelled", 150_090),
]

SCHEMA = """CREATE TABLE "Order" (
    id TEXT NOT NULL, config_file_path TEXT NOT NULL, strategy TEXT NOT NULL, market TEXT NOT NULL,
    symbol TEXT NOT NULL, base_asset TEXT NOT NULL, quote_asset TEXT NOT NULL, creation_timestamp BIGINT NOT NULL,
    order_type TEXT NOT NULL, amount BIGINT NOT NULL, leverage INTEGER NOT NULL, price BIGINT NOT NULL,
    last_status TEXT NOT NULL, last_update_timestamp BIGINT NOT NULL, exchange_order_id TEXT, position TEXT,
    PRIMARY KEY (id)
);
CREATE TABLE "OrderStatus" (
    id INTEGER NOT NULL, order_id TEXT NOT NULL, timestamp BIGINT NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (id), FOREIGN KEY(order_id) REFERENCES "Order" (id)
);
CREATE TABLE "TradeFill" (
    config_file_path TEXT NOT NULL, strategy TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
    base_asset TEXT NOT NULL, quote_asset TEXT NOT NULL, timestamp BIGINT NOT NULL, order_id TEXT NOT NULL,
    trade_type TEXT NOT NULL, order_type TEXT NOT NULL, price BIGINT NOT NULL, amount BIGINT NOT NULL,
    leverage INTEGER NOT NULL, trade_fee JSON NOT NULL, trade_fee_in_quote BIGINT, exchange_trade_id TEXT NOT NULL,
    position TEXT, PRIMARY KEY (market, order_id, exchange_trade_id), FOREIGN KEY(order_id) REFERENCES "Order" (id)
);
"""


def _d6(x: str | Decimal) -> int:
    return int(Decimal(x) * 1_000_000)


def _q(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _rows(market: str, orders: list[tuple], paper: bool) -> tuple[list[str], list[dict[str, object]]]:
    sql: list[str] = []
    fills_out: list[dict[str, object]] = []
    sid = 0
    fee = (
        {"fee_type": "AddedToCost", "percent": "0", "percent_token": None, "flat_fees": []}
        if paper
        else {"fee_type": "DeductedFromReturns", "percent": "0.001", "percent_token": "USDT", "flat_fees": []}
    )
    for suffix, side, price, created, xid, fills, end, end_ms in orders:
        cid = f"{'buy' if side == 'BUY' else 'sell'}-BTC-USDT-{T0 + created}{suffix.zfill(3)}"
        otype = "MARKET" if price == "MARKET" else "LIMIT"
        amount = "0.0005" if otype == "MARKET" else "0.001"
        px = 0 if otype == "MARKET" else _d6(price)
        created_ms = T0 + created
        last = {"completed": f"{side.title()}OrderCompleted", "cancelled": "OrderCancelled", "failure": "OrderFailure"}
        sql.append(
            'INSERT INTO "Order" VALUES ('
            + ",".join(
                _q(v)
                for v in (
                    cid,
                    CONFIG if not paper else "conf_pure_mm_1_paper.yml",
                    "pure_market_making",
                    market,
                    "BTC-USDT",
                    "BTC",
                    "USDT",
                    created_ms,
                    otype,
                    _d6(amount),
                    1,
                    px,
                    last[end],
                    T0 + end_ms,
                    xid,
                    "NIL",
                )
            )
            + ");"
        )
        statuses = [(created_ms, f"{side.title()}OrderCreated")]
        for ms, qty, tid in fills:
            statuses.append((T0 + ms, "OrderFilled"))
            fpx = Decimal("61970.0") if otype == "MARKET" else Decimal(price)
            fee_q = Decimal(0) if paper else (fpx * Decimal(qty) * Decimal("0.001"))
            row = {
                "exchange_trade_id": tid,
                "config_file_path": CONFIG if not paper else "conf_pure_mm_1_paper.yml",
                "strategy": "pure_market_making",
                "market": market,
                "symbol": "BTC-USDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "timestamp": T0 + ms,
                "order_id": cid,
                "trade_type": side,
                "order_type": otype,
                "price": fpx,
                "amount": Decimal(qty),
                "leverage": 1,
                "trade_fee": fee,
                "trade_fee_in_quote": fee_q,
                "position": "NIL",
                "age": f"00:00:{(ms - created) // 1000:02d}",
            }
            fills_out.append(row)
            sql.append(
                'INSERT INTO "TradeFill" VALUES ('
                + ",".join(
                    _q(v)
                    for v in (
                        row["config_file_path"],
                        "pure_market_making",
                        market,
                        "BTC-USDT",
                        "BTC",
                        "USDT",
                        T0 + ms,
                        cid,
                        side,
                        otype,
                        _d6(fpx),
                        _d6(qty),
                        1,
                        json.dumps(fee),
                        _d6(fee_q),
                        tid,
                        "NIL",
                    )
                )
                + ");"
            )
        statuses.append((T0 + end_ms, last[end]))
        for ts, st in statuses:
            sid += 1
            sql.append(f'INSERT INTO "OrderStatus" VALUES ({sid},{_q(cid)},{ts},{_q(st)});')
    return sql, fills_out


def _csv(fills: list[dict[str, object]]) -> str:
    cols = [
        "exchange_trade_id",
        "config_file_path",
        "strategy",
        "market",
        "symbol",
        "base_asset",
        "quote_asset",
        "timestamp",
        "order_id",
        "trade_type",
        "order_type",
        "price",
        "amount",
        "leverage",
        "trade_fee",
        "trade_fee_in_quote",
        "position",
        "age",
    ]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in fills:
        w.writerow([str(r[c]) for c in cols])
    return buf.getvalue()


def main() -> None:
    head = "-- Generated by make_fixtures.py (see its docstring). Hummingbot {} bot database as SQL.\n"
    live_sql, live_fills = _rows("binance", LIVE, paper=False)
    paper_sql, _ = _rows("binance_PaperTrade", PAPER, paper=True)
    (HERE / "conf_pure_mm_1.sql").write_text(head.format("live") + SCHEMA + "\n".join(live_sql) + "\n")
    (HERE / "conf_pure_mm_1_paper.sql").write_text(head.format("paper-trade") + SCHEMA + "\n".join(paper_sql) + "\n")
    (HERE / "trades_conf_pure_mm_1.csv").write_text(_csv(live_fills))


if __name__ == "__main__":
    main()
