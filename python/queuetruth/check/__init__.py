"""`queuetruth check`: how wrong is a backtest, in one command.

`qt.check("backtest.jsonl", data="dataset")` replays the backtest's own resting limit orders in the exact queue of
an order-level dataset and compares them order by order (`queuetruth.check.run`). The backtester is read from the
file (`queuetruth.check.detect`) and its symbols are mapped from the dataset's symbology
(`queuetruth.check.symbols`). Without `data` there is no number yet: the estimate from the atlas touch tables comes in
a later release.
"""

from __future__ import annotations

from queuetruth.check.detect import BACKTESTERS, detect
from queuetruth.check.run import CheckResult, check

__all__ = ["BACKTESTERS", "CheckResult", "check", "detect"]
