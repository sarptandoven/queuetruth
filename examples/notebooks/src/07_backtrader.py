# %% [markdown]
# # Backtrader: realistic limit fills in one line
#
# Backtrader fills a limit order as soon as a bar reaches its price. In the real queue many of those orders would not
# have filled. queuetruth's `QueueFiller` keeps Backtrader's own limit logic and fills each touched order with the
# probability the atlas touch tables give for its tier, side and bar length; `OrderLog` writes the orders so
# `qt.check` can say how wrong the run was:
#
# ```python
# cerebro.broker.set_filler(qt.backtrader.QueueFiller(tier="T1"))
# cerebro.addanalyzer(qt.backtrader.OrderLog)
# ```
#
# Data: queuetruth's **synthetic** dataset and the repository's **synthetic** atlas (invented numbers). The numbers
# only show the workflow; they say nothing about a real venue.
# %%
import importlib.util
import json
from pathlib import Path

import pandas as pd
from IPython.display import display

import queuetruth as qt

pd.set_option("display.width", 140)
WORK = Path("qt-notebooks") / "07_backtrader"
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
# ## 1. What the filler draws from
#
# The touch tables give `P(real | touch)`: the probability that a resting limit order the bar touched really filled
# in the queue, by tier, side and bar length. The filler is made from the atlas's tables (here the synthetic atlas;
# without `atlas=` the published one, after asking) and needs no Backtrader to inspect.

# %%
filler = qt.backtrader.QueueFiller(tier="T1", atlas=ATLAS, yes=True)
display(
    pd.DataFrame(
        [
            {"bar": b, "side": s, "P(real | touch)": filler.touch.probability(b, s)}
            for b in filler.touch.bars
            for s in ("buy", "sell")
        ]
    )
)

# %% [markdown]
# ## 2. The one line
#
# A small strategy on synthetic 5-minute bars quotes one tick under the close every hour. With Backtrader installed
# the cell runs it with the filler and the order log; without it, it says so.

# %%
if have("backtrader"):
    import backtrader as bt
    import numpy as np

    idx = pd.date_range("2026-06-15 14:00", periods=288, freq="5min")
    close = 5000 + np.cumsum(np.random.default_rng(0).normal(0, 0.5, len(idx)))
    bars = pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 100}, index=idx
    )

    class QuoteUnderTheClose(bt.Strategy):
        def next(self):
            if len(self) % 12 == 0:
                self.buy(exectype=bt.Order.Limit, price=self.data.close[0] - 0.25, size=1)

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=bars, timeframe=bt.TimeFrame.Minutes, compression=5), name="QTAU6")
    cerebro.addstrategy(QuoteUnderTheClose)
    run_filler = qt.backtrader.QueueFiller(tier="T1", atlas=ATLAS, yes=True)
    cerebro.broker.set_filler(run_filler)
    cerebro.addanalyzer(qt.backtrader.OrderLog, path=str(WORK / "bt_run.jsonl"))
    cerebro.run()
    print(run_filler.summary())

# %% [markdown]
# ## 3. How wrong was the run?
#
# `qt.check` reads the order log. Here it is twelve orders on the synthetic futures, written as `OrderLog` writes
# them (one line per notification); use your own log. Without order-level data the answer is the atlas estimate:

# %%
log = WORK / "bt_orders.jsonl"


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S")


with log.open("w") as f:
    for k, o in enumerate(ORDERS.itertuples(), start=1):
        size = o.qty if o.side == "buy" else -o.qty
        base = {"event": "order", "ref": k, "data": o.symbol, "ordtype": o.side.title(), "exectype": "Limit",
                "size": size, "price": o.price, "pricelimit": o.price, "created": stamp(o.placed), "valid": None,
                "order_key": None}  # fmt: skip
        lines = [{**base, "status": s, "dt": stamp(o.placed), "exbits": []} for s in ("Submitted", "Accepted")]
        if o.filled:
            bit = {"dt": stamp(o.done), "size": size, "price": o.price, "comm": 0}
            lines.append({**base, "status": "Completed", "dt": stamp(o.done), "exbits": [bit]})
        else:
            lines.append({**base, "status": "Canceled", "dt": stamp(o.done), "exbits": []})
        f.writelines(json.dumps(x) + "\n" for x in lines)

estimate = qt.check(log, bar="5m", tier="T1", atlas=ATLAS, yes=True, out=WORK / "estimate")
display(estimate)

# %% [markdown]
# With order-level data of the same days, each order is replayed in the exact queue instead:

# %%
res = qt.check(log, data=DATA, horizon="1s", window="10s", out=WORK / "check")
display(res)

# %%
display(res.table()[["symbol", "side", "price", "entry_local", "backtest_filled", "real_filled", "outcome"]])
