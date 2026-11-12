"""`queuetruth compare`: my backtest against my live run, order by order.

`qt.compare("backtest.zip", live="tradesv3.sqlite")` reads which backtester wrote the backtest and what the live
run is (`queuetruth.compare.detect`), then runs the existing comparison for that pair (`queuetruth.compare.run`):
the backtester's reconciler for trading bots and bar backtesters, the scorer with the simulator arm for
event-driven simulators, and with `data` also the markouts, the P&L gap per order and the exact arm. It answers
first: how many of the backtest's fills happened live, the fills only one side made and why, the P&L gap, time to
fill, markouts and latency. `queuetruth.compare.watch` runs it again as live records arrive and reports the drift
since a baseline window (`queuetruth compare --watch`).
"""

from __future__ import annotations

from queuetruth.compare.detect import LiveKind, detect_backtest, detect_live
from queuetruth.compare.run import CompareResult, compare

__all__ = ["CompareResult", "LiveKind", "compare", "detect_backtest", "detect_live"]
