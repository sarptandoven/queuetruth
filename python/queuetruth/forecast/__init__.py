"""Forecast referee (`queuetruth forecast`): timing truth and edge truth for forecasts of any outcome.

- `ledger`: hash-chained, timestamped commitments of forecasts and resolutions (`qt.forecast_entry`), plain-JSON
  head commitments (`qt.forecast_commitment`) for external anchoring, and verification.
- `engine.score`: the leakage guard (no forecast recorded after its question's resolution or information cutoff
  counts), proper scores (Brier, log, spherical, RPS, CRPS), reliability and ECE with CIs, skill against the
  market, climatology and uniform baselines, Diebold-Mariano and question-clustered bootstrap comparisons, power
  analysis and executable fractional-Kelly PnL against quotes with spread, depth and fees (`qt.forecast_scorecard`).
- `scores`, `calibration`, `inference`, `power`, `pnl`, `simulate`: the building blocks.

Submodules load on first attribute access, so `import queuetruth` stays light.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "append": "ledger",
    "check_commitment": "ledger",
    "commitment": "ledger",
    "commitment_digest": "ledger",
    "normalize_forecast": "ledger",
    "normalize_resolution": "ledger",
    "verify_ledger": "ledger",
    "score": "engine",
    "parse_spec": "engine",
    "ForecastScoreSpec": "schemas",
    "ForecastScorecard": "schemas",
    "ForecastCommitment": "schemas",
    "ForecastEntry": "schemas",
    "ForecastLedgerSummary": "schemas",
    "PnlSpec": "schemas",
    "FeeSchedule": "schemas",
    "binary_scores": "scores",
    "categorical_scores": "scores",
    "crps_normal": "scores",
    "crps_quantiles": "scores",
    "crps_samples": "scores",
    "dm_test": "inference",
    "paired_cluster_bootstrap": "inference",
    "required_n": "power",
    "power_at": "power",
    "run_pnl": "pnl",
    "binary_world": "simulate",
}
_SUBMODULES = ("calibration", "cli", "engine", "inference", "io", "ledger", "pnl", "power", "schemas", "scores")
_SUBMODULES += ("simulate", "tables")

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        return getattr(importlib.import_module(f"queuetruth.forecast.{_EXPORTS[name]}"), name)
    if name in _SUBMODULES:
        return importlib.import_module(f"queuetruth.forecast.{name}")
    raise AttributeError(f"module 'queuetruth.forecast' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*_EXPORTS, *_SUBMODULES])
