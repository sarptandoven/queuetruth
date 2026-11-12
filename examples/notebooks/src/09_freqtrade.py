# %% [markdown]
# # Freqtrade: how many of a backtest's fills were real
#
# Freqtrade fills a limit order when a candle reaches its price. For Freqtrade the one line is `queuetruth check`
# on the backtest result, in Python `qt.check(result)`: how many of its limit fills the real queue would have given,
# and the P&L those fills add, from the atlas touch tables (no order-level data needed):
#
# ```sh
# queuetruth check user_data/backtest_results/backtest-result-2026-06-16_09-00-00.zip
# ```
#
# Data: the repository's **synthetic** atlas (invented numbers) and a **synthetic** backtest result. The numbers only
# show the workflow; they say nothing about a real venue.
# %%
import importlib.util
import json
from pathlib import Path

import pandas as pd
from IPython.display import display

import queuetruth as qt

pd.set_option("display.width", 140)
WORK = Path("qt-notebooks") / "09_freqtrade"
WORK.mkdir(parents=True, exist_ok=True)

# queuetruth's synthetic order-level dataset: two futures (QTAU6, QTBU6), two days, invented prices.
DATA = WORK / "synth"
if not (DATA / "manifest.json").exists():
    qt.fixtures_synth(str(DATA))

# The repository's synthetic atlas (invented numbers, no market data): the checkout's copy, else the one on GitHub.
# Leave `source=` out to use the published atlas (it asks before downloading anything).
_local = Path(qt.__file__).resolve().parents[2] / "tests" / "fixtures" / "atlas_synth"
ATLAS = (
    str(_local)
    if (_local / "atlas" / "v1" / "manifest.json").exists()
    else "https://raw.githubusercontent.com/sarptandoven/queuetruth/main/tests/fixtures/atlas_synth/atlas/v1/"
)

# Twelve resting limit orders on the synthetic futures, as a bar backtest reports them: placed at `placed` (UTC),
# and filled 5 s later when `filled` (the touch rule), else cancelled 30 s later.
ORDERS = pd.DataFrame(
    [
        ("O-1", "QTBU6", "sell", 1, 5008.50, "2026-06-16 14:38:42", True),
        ("O-2", "QTAU6", "buy", 2, 4999.75, "2026-06-16 14:38:05", False),
        ("O-3", "QTBU6", "sell", 3, 5010.25, "2026-06-15 17:09:27", True),
        ("O-4", "QTAU6", "buy", 1, 4999.50, "2026-06-16 17:32:15", False),
        ("O-5", "QTAU6", "sell", 2, 5002.75, "2026-06-15 15:39:13", True),
        ("O-6", "QTAU6", "buy", 3, 5004.00, "2026-06-15 19:04:05", False),
        ("O-7", "QTBU6", "buy", 1, 5008.75, "2026-06-15 15:36:19", True),
        ("O-8", "QTBU6", "sell", 2, 5008.50, "2026-06-16 16:58:59", False),
        ("O-9", "QTAU6", "buy", 3, 4999.50, "2026-06-16 14:39:20", True),
        ("O-10", "QTAU6", "buy", 1, 4999.50, "2026-06-16 17:25:05", False),
        ("O-11", "QTBU6", "buy", 2, 5008.25, "2026-06-16 16:12:29", True),
        ("O-12", "QTAU6", "buy", 3, 5001.25, "2026-06-15 15:05:05", False),
    ],
    columns=["order", "symbol", "side", "qty", "price", "placed", "filled"],
)
ORDERS["placed"] = pd.to_datetime(ORDERS["placed"], utc=True)
ORDERS["done"] = ORDERS["placed"] + pd.to_timedelta(ORDERS["filled"].map({True: 5, False: 30}), unit="s")


def have(*modules, hint=None):
    """True when every module is importable; else prints which are missing and how to get them."""
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        print(f"skipped: {', '.join(missing)} not installed here ({hint or 'pip install ' + ' '.join(missing)})")
    return not missing


print("queuetruth", qt.__version__, "| SYNTHETIC data and a SYNTHETIC atlas: the numbers show the workflow only")

# %% [markdown]
# ## 1. The backtest result
#
# `freqtrade backtesting` writes its result to `user_data/backtest_results/` (a `.zip`, or a `.json` in older
# releases). `qt.check` reads either; the bar length comes from the result's `timeframe`. Here six trades are written
# in the result's layout; use your own file.

# %%
have("freqtrade", hint="pip install freqtrade, then run freqtrade backtesting for a real result")


def order(side, price, qty, when, entry):
    """One filled order of a Freqtrade trade."""
    ms = int(when.timestamp() * 1000)
    return {"amount": float(qty), "safe_price": price, "ft_order_side": side, "ft_is_entry": entry,
            "order_filled_timestamp": ms, "ft_order_tag": "" if entry else "roi", "cost": price * qty}  # fmt: skip


trades = []
for o in ORDERS[ORDERS["filled"]].itertuples():
    entry = o.placed.floor("5min")
    exit_ = entry + pd.Timedelta(minutes=30)
    short = o.side == "sell"
    close = o.price - 0.5 if short else o.price + 0.5
    trade = {"pair": f"{o.symbol}/USD", "stake_amount": o.price * o.qty, "amount": float(o.qty),
             "open_date": str(entry), "close_date": str(exit_), "open_rate": o.price, "close_rate": close,
             "fee_open": 0.0005, "fee_close": 0.0005, "trade_duration": 30, "profit_abs": 0.5 * o.qty,
             "exit_reason": "roi", "is_open": False, "leverage": 1.0, "is_short": short}  # fmt: skip
    trade["open_timestamp"], trade["close_timestamp"] = int(entry.timestamp() * 1000), int(exit_.timestamp() * 1000)
    trade["orders"] = [
        order(o.side, o.price, o.qty, entry, True),
        order("buy" if short else "sell", close, o.qty, exit_, False),
    ]
    trades.append(trade)
strategy = {"trades": trades, "timeframe": "5m", "stake_currency": "USD", "trading_mode": "spot",
            "strategy_name": "Synthetic", "max_open_trades": 6}  # fmt: skip
result = WORK / "backtest-result-2026-06-16_09-00-00.json"
result.write_text(json.dumps({"strategy": {"Synthetic": strategy}, "strategy_comparison": []}))
print(len(trades), "trades,", 2 * len(trades), "orders")

# %% [markdown]
# ## 2. The one line
#
# The pairs are not in the atlas, so `tier="ALL"` pools every liquidity tier (with your own pairs, the atlas looks
# each symbol up and needs no tier):

# %%
res = qt.check(result, tier="ALL", atlas=ATLAS, yes=True, out=WORK / "check")
display(res)

# %%
display(res.table())

# %% [markdown]
# The page to share, and the document:

# %%
print(res.save(WORK / "check.html"))
print(res.save(WORK / "check.json"))
