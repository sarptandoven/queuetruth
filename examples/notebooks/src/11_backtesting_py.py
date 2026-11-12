# %% [markdown]
# # backtesting.py: how many of a backtest's fills were real
#
# backtesting.py fills a limit order in full when a bar reaches its price. For backtesting.py the one line is
# `queuetruth check` on the run's trades, in Python `qt.check(trades)`: each entry and exit is checked as a limit
# order resting at its fill price, against the atlas touch tables or, with order-level data, in the exact queue:
#
# ```python
# stats._trades.to_csv("trades.csv")   # then: queuetruth check trades.csv --bar 5m
# ```
#
# Data: queuetruth's **synthetic** dataset and the repository's **synthetic** atlas (invented numbers). The numbers
# only show the workflow; they say nothing about a real venue.
# %%
import importlib.util
from pathlib import Path

import pandas as pd
from IPython.display import display

import queuetruth as qt

pd.set_option("display.width", 140)
WORK = Path("qt-notebooks") / "11_backtesting_py"
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
# ## 1. The trades
#
# With backtesting.py installed, a small strategy on synthetic 5-minute bars writes its trades the usual way. The
# check below uses trades of the synthetic future `QTAU6` in the same layout, so it can also replay them in the exact
# queue; use your own trades.

# %%
if have("backtesting"):
    import numpy as np
    from backtesting import Backtest, Strategy

    idx = pd.date_range("2026-06-15 14:00", periods=288, freq="5min")
    close = 5000 + np.cumsum(np.random.default_rng(0).normal(0, 0.5, len(idx)))
    ohlc = pd.DataFrame({"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close}, index=idx)

    class EveryHour(Strategy):
        def init(self):
            pass

        def next(self):
            if len(self.data) % 12 == 0:
                self.position.close()
                self.buy(size=1, limit=self.data.Close[-1] - 0.25)

    stats = Backtest(ohlc, EveryHour, cash=1_000_000, commission=0.0002, finalize_trades=True).run()
    stats._trades.to_csv(WORK / "run_trades.csv")
    print(len(stats._trades), "trades")

trades = pd.DataFrame(
    [
        (-1, 0, 1, 5001.5, 5002.5, -1.02, 0.02, "2026-06-15 15:06:40", "2026-06-15 15:39:26"),
        (-3, 2, 3, 5002.75, 5004.5, -5.27, 0.02, "2026-06-15 15:32:12", "2026-06-15 17:59:24"),
        (-1, 4, 5, 5003.25, 5003.5, -0.27, 0.02, "2026-06-15 17:13:28", "2026-06-15 18:39:00"),
        (-2, 6, 7, 5005.0, 5004.25, 1.48, 0.02, "2026-06-15 17:37:58", "2026-06-15 18:58:54"),
    ],
    columns=["Size", "EntryBar", "ExitBar", "EntryPrice", "ExitPrice", "PnL", "Commission", "EntryTime", "ExitTime"],
)
trades["ReturnPct"] = trades["PnL"] / (trades["EntryPrice"] * trades["Size"].abs())
trades["Duration"] = pd.to_datetime(trades["ExitTime"]) - pd.to_datetime(trades["EntryTime"])
path = WORK / "trades.csv"
trades.to_csv(path)
display(trades)

# %% [markdown]
# ## 2. The one line
#
# The trades file does not name its instrument, so `options={"instrument": ...}` names it. Without order-level data,
# the atlas estimate (say the bar length; `tier` for symbols the atlas does not hold):

# %%
estimate = qt.check(
    path, bar="5m", tier="ALL", atlas=ATLAS, yes=True, options={"instrument": "QTAU6"}, out=WORK / "estimate"
)
display(estimate)

# %% [markdown]
# With order-level data of the same day, each entry and exit is replayed in the exact queue at its price and time:

# %%
res = qt.check(path, data=DATA, horizon="1s", window="10s", options={"instrument": "QTAU6"}, out=WORK / "check")
display(res)

# %%
display(res.table()[["symbol", "side", "price", "entry_local", "backtest_filled", "real_filled", "outcome"]])
