"""`queuetruth fit`: the parameter that fixes a backtester's fill model, with its verdict.

`qt.fit("nautilus", data="dataset")` picks the existing calibration for the backtester and the input (order-level
data: `calibrate alloc` or `calibrate nt-touch`; live fills: `calibrate-live`), splits the data in time
(`split="70%"` or `test_after="09:42"`, `queuetruth.fit.split`), compares the fit with the library default on the
held-out part and writes the paste-ready snippet only when it passes (`queuetruth.fit.run`).
"""

from __future__ import annotations

from queuetruth.fit.run import BACKTESTERS, FAMILIES, FitResult, fit

__all__ = ["BACKTESTERS", "FAMILIES", "FitResult", "fit"]
