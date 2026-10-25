"""Writes the Freqtrade backtest-result fixtures of this directory (deterministic; run from anywhere):

    python tests/fixtures/live/freqtrade/make_fixtures.py

The files follow Freqtrade's export code at commit 1c0d709401b5 (`optimize_reports/bt_storage.py`
`store_backtest_results`, `data/btanalysis/bt_fileutils.py` `BT_DATA_COLUMNS`, `persistence/trade_model.py`
`Order.to_json(minified=True)`): a zip `backtest-result-2024-03-01_14-00-00.zip` holding
`backtest-result-2024-03-01_14-00-00.json` (`{"strategy": {...}, "strategy_comparison": [...]}`) and
`backtest-result-2024-03-01_14-00-00_config.json`, the metadata file `backtest-result-2024-03-01_14-00-00.meta.json`
and `.last_result.json`. Dates are written as `str(pandas.Timestamp)` (orjson `default=str`), numbers as JSON
floats. The trades are invented for the reconciliation scenario of docs/how-to/reconcile-freqtrade-hummingbot.md;
`tradesv3.dryrun.sql` and `freqtrade.log` are the dry-run bot of the same scenario, `tradesv3.live.sql` the live bot
(hand-written SQL, see their headers); `tradesv3.futures.sql` is a separate one-trade futures database.
"""

from __future__ import annotations

import datetime as dt
import json
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEM = "backtest-result-2024-03-01_14-00-00"
STRATEGY = "SampleStrategy"


def _ms(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return int(dt.datetime(2024, 3, 1, h, m, tzinfo=dt.UTC).timestamp() * 1000)


def _date(ms: int) -> str:
    return str(dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC))


def _trade(
    pair: str,
    amount: float,
    open_at: str,
    open_rate: float,
    entry_fill_at: str,
    close_at: str,
    close_rate: float,
    exit_reason: str,
    enter_tag: str,
) -> dict[str, object]:
    fee = 0.001
    o, c, ef = _ms(open_at), _ms(close_at), _ms(entry_fill_at)
    profit_abs = round(amount * (close_rate - open_rate) - fee * amount * (open_rate + close_rate), 8)
    stake = round(amount * open_rate, 8)
    return {
        "pair": pair,
        "stake_amount": stake,
        "max_stake_amount": stake,
        "amount": amount,
        "open_date": _date(o),
        "close_date": _date(c),
        "open_rate": open_rate,
        "close_rate": close_rate,
        "fee_open": fee,
        "fee_close": fee,
        "trade_duration": (c - o) // 60_000,
        "profit_ratio": round(profit_abs / stake, 8),
        "profit_abs": profit_abs,
        "exit_reason": exit_reason,
        "initial_stop_loss_abs": round(open_rate * 0.99, 8),
        "initial_stop_loss_ratio": -0.01,
        "stop_loss_abs": round(open_rate * 0.99, 8),
        "stop_loss_ratio": -0.01,
        "min_rate": min(open_rate, close_rate),
        "max_rate": max(open_rate, close_rate),
        "is_open": False,
        "enter_tag": enter_tag,
        "leverage": 1.0,
        "is_short": False,
        "open_timestamp": o,
        "close_timestamp": c,
        "orders": [
            {
                "amount": amount,
                "safe_price": open_rate,
                "ft_order_side": "buy",
                "order_filled_timestamp": ef,
                "ft_is_entry": True,
                "ft_order_tag": enter_tag,
                "cost": amount * open_rate * (1 + fee),
            },
            {
                "amount": amount,
                "safe_price": close_rate,
                "ft_order_side": "sell",
                "order_filled_timestamp": c,
                "ft_is_entry": False,
                "ft_order_tag": exit_reason,
                "cost": amount * close_rate * (1 + fee),
            },
        ],
        "funding_fees": 0.0,
    }


TRADES = [
    _trade("BTC/USDT", 0.0016, "10:05", 61000.0, "10:05", "10:40", 61610.0, "roi", "rsi_cross"),
    _trade("ETH/USDT", 0.03, "10:15", 3400.0, "10:15", "11:00", 3425.5, "exit_signal", "rsi_cross"),
    # 60588.00000000001: a stop rate computed in floating point (open_rate * 0.99), as Freqtrade exports it
    _trade("BTC/USDT", 0.0016, "11:30", 61200.0, "11:30", "12:10", 60588.00000000001, "stop_loss", "bb_lower"),
    _trade("ETH/USDT", 0.03, "12:00", 3390.0, "12:00", "12:25", 3423.9000000000005, "roi", "rsi_cross"),
    _trade("BTC/USDT", 0.0016, "13:00", 61500.0, "13:10", "13:45", 61800.0, "roi", "bb_lower"),
]


def results() -> dict[str, object]:
    total = round(sum(float(t["profit_abs"]) for t in TRADES), 8)  # type: ignore[arg-type]
    strat = {
        "trades": TRADES,
        "locks": [],
        "total_trades": len(TRADES),
        "profit_total_abs": total,
        "backtest_start": _date(_ms("10:00")),
        "backtest_start_ts": _ms("10:00"),
        "backtest_end": _date(_ms("14:00")),
        "backtest_end_ts": _ms("14:00"),
        "stake_currency": "USDT",
        "stake_currency_decimals": 3,
        "starting_balance": 1000.0,
        "dry_run_wallet": 1000.0,
        "rejected_signals": 0,
        "timedout_entry_orders": 1,
        "timedout_exit_orders": 0,
        "canceled_trade_entries": 0,
        "canceled_entry_orders": 0,
        "replaced_entry_orders": 0,
        "max_open_trades": 3,
        "timeframe": "5m",
        "timeframe_detail": "",
        "timerange": "20240301-20240302",
        "strategy_name": STRATEGY,
        "stoploss": -0.01,
        "minimal_roi": {"0": 0.01},
        "trading_mode": "spot",
        "margin_mode": "",
    }
    return {
        "strategy": {STRATEGY: strat},
        "strategy_comparison": [{"key": STRATEGY, "trades": len(TRADES), "profit_total_abs": total}],
    }


def config() -> dict[str, object]:
    return {
        "max_open_trades": 3,
        "stake_currency": "USDT",
        "stake_amount": 100,
        "timeframe": "5m",
        "dry_run": True,
        "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes"},
        "order_types": {"entry": "limit", "exit": "limit", "stoploss": "limit", "stoploss_on_exchange": True},
        "order_time_in_force": {"entry": "GTC", "exit": "GTC"},
        "exchange": {"name": "binance", "key": "", "secret": "", "pair_whitelist": ["BTC/USDT", "ETH/USDT"]},
        "strategy": STRATEGY,
    }


def _dump(doc: object) -> str:
    return json.dumps(doc, separators=(",", ":"))


def main() -> None:
    body = _dump(results())
    cfg = _dump(config())
    (HERE / f"{STEM}.json").write_text(body, encoding="utf-8")
    (HERE / f"{STEM}_config.json").write_text(cfg, encoding="utf-8")
    meta = {STRATEGY: {"run_id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0", "backtest_start_time": 1709301600}}
    (HERE / f"{STEM}.meta.json").write_text(_dump(meta), encoding="utf-8")
    (HERE / ".last_result.json").write_text(_dump({"latest_backtest": f"{STEM}.zip"}), encoding="utf-8")
    with zipfile.ZipFile(HERE / f"{STEM}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in ((f"{STEM}.json", body), (f"{STEM}_config.json", cfg)):
            info = zipfile.ZipInfo(name, date_time=(2024, 3, 1, 14, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text)


if __name__ == "__main__":
    main()
