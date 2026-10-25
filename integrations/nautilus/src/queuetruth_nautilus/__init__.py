"""Calibrated NautilusTrader fill models from queuetruth calibrations.

queuetruth fits NautilusTrader's `prob_fill_on_limit` (and, from live logs, one-way latencies) against an exact
order-book replay and scores the fit on a held-out window. This package turns that `qt.calibration_result` (or the
`queuetruth export nautilus` text) into the objects NautilusTrader 1.231.x and 2.0.x take:

    import queuetruth_nautilus as qtn

    cal = qtn.load_calibration("cal/calibration.json")
    engine.add_venue(venue=..., oms_type=..., account_type=..., starting_balances=[...], **qtn.venue_options(cal))

A calibration whose trust verdict is `refused` raises `CalibrationRefused` (`QT-CALIB-001`) unless `force=True`.
"""

from __future__ import annotations

from queuetruth_nautilus.calibration import (
    HeldOutScores,
    Latency,
    NautilusCalibration,
    Trust,
    from_document,
    from_snippet,
    load_calibration,
)
from queuetruth_nautilus.errors import CalibrationRefused, CalibrationWarning, QtNautilusError
from queuetruth_nautilus.snippet import render as render_snippet
from queuetruth_nautilus.venue import (
    check_trust,
    fill_model,
    fill_model_kwargs,
    latency_model,
    latency_model_kwargs,
    venue_config,
    venue_options,
)

__version__ = "0.1.0"

__all__ = [
    "CalibrationRefused",
    "CalibrationWarning",
    "HeldOutScores",
    "Latency",
    "NautilusCalibration",
    "QtNautilusError",
    "Trust",
    "__version__",
    "check_trust",
    "fill_model",
    "fill_model_kwargs",
    "from_document",
    "from_snippet",
    "latency_model",
    "latency_model_kwargs",
    "load_calibration",
    "render_snippet",
    "venue_config",
    "venue_options",
]
