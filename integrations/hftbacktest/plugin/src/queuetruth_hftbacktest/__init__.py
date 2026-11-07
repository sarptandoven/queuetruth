"""Score hftbacktest's fills against exact-queue truth and calibrate its ProbQueue model, with queuetruth.

queuetruth replays order-level (L3 / MBO) data with an exact FIFO queue. This package wraps its commands for an
hftbacktest user, in the queuetruth environment (hftbacktest itself runs in its own venv, called by path):

    import queuetruth_hftbacktest as qthbt

    # 1. how far are hftbacktest's fills from the exact queue, for my orders and my asset settings?
    prep = qthbt.prepare("dataset", "orders.parquet", "hbt-check")
    qthbt.run(prep, python=".venv-hbt/bin/python", snippet="my_asset.py")
    print(qthbt.score(prep).text())

    # 2. fit the ProbQueue n on one window, check it on another, get the code to paste
    cal = qthbt.calibrate("dataset", train="2026-06-15", test="2026-06-16", out="hbt-cal")
    print(cal.text())
    print(cal.code)   # CalibrationRefused (QT-CALIB-001) when no fit passes queuetruth's trust check

`queuetruth-hftbacktest prepare|run|score|calibrate` is the same from the command line.
"""

from __future__ import annotations

from queuetruth_hftbacktest.calibrate import FamilyFit, HbtCalibration, calibrate, select, window
from queuetruth_hftbacktest.errors import CalibrationRefused, QtHbtError
from queuetruth_hftbacktest.replay import HbtScore, HorizonScore, Prepared, prepare, run, score

__version__ = "0.1.0"

__all__ = [
    "CalibrationRefused",
    "FamilyFit",
    "HbtCalibration",
    "HbtScore",
    "HorizonScore",
    "Prepared",
    "QtHbtError",
    "__version__",
    "calibrate",
    "prepare",
    "run",
    "score",
    "select",
    "window",
]
