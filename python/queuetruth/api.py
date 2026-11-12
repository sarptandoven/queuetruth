# ruff: noqa: F401  (a re-export module: every import below is a name of `queuetruth.api`)
"""High-level Python API (SPEC 4.3), used by the CLI, the service worker and MCP local mode.

Every function accepts a Spec model or a plain dict (validated; invalid specs raise `QtError("QT-CONFIG-001")`) and
returns the Pydantic summary model. The operations live in `queuetruth.ops`, one module each (`ops.ingest`,
`ops.replay`, `ops.shadows`, `ops.zoo`, `ops.calibrate`, `ops.score`, `ops.live`, `ops.report`, `ops.dq`,
`ops.referee`, `ops.why`, ...); this module re-exports every name it had before they moved, so `qt.<name>` and
`queuetruth.api.<name>` keep working. The verbs `check` and `fit` run in `queuetruth.check` and `queuetruth.fit`.

The private names re-exported here (`_call`, `_validate`, `_planned_life`, ...) are imported by other packages and
scripts today; they stay until the import-layer allow-list (`tools/check_layers_allow.txt`) lists none of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from queuetruth import units
from queuetruth.dataset import open_dataset, read_manifest_doc
from queuetruth.errors import QtError
from queuetruth.ops._call import _call, _native, _qt_error, _validate
from queuetruth.ops.calibrate import LATENCY_FIT_KEYS, calibrate, calibrate_live
from queuetruth.ops.dq import dq
from queuetruth.ops.export import REPLAY_SCHEDULE_FUNCTIONS, export, export_replay_schedule
from queuetruth.ops.features import extract_features
from queuetruth.ops.fit_hazard import fit_hazard
from queuetruth.ops.fixtures import fixtures_synth
from queuetruth.ops.ingest import ingest
from queuetruth.ops.live import import_live, validate_live
from queuetruth.ops.referee import referee, referee_fixtures
from queuetruth.ops.replay import _expand_order_ids, replay, resolve_order_ids
from queuetruth.ops.report import report
from queuetruth.ops.run_id import run_id
from queuetruth.ops.score import score
from queuetruth.ops.session import Session
from queuetruth.ops.shadows import mirror_real_orders, sample_shadows, shadows_from_live
from queuetruth.ops.tables import fill_liquidity, read_table
from queuetruth.ops.why import (
    WhyExplanation,
    _model_matches,
    dataset_kind,
    fmt_dur,
    fmt_scaled,
    fmt_utc,
    why,
    why_explain,
    why_suggestion,
)
from queuetruth.ops.zoo import _planned_life, run_zoo
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s
from queuetruth.schemas.arrow import ENGINE_VERSION, SCHEMAS
from queuetruth.schemas.io import write_table
from queuetruth.schemas.runid import spec_echo


def check(backtest: Any, data: str | Path | None = None, **kwargs: Any) -> Any:
    """`check`: how wrong a backtest is, its own resting limit orders replayed in the exact queue of `data`
    (`queuetruth.check.check`; returns a `queuetruth.check.CheckResult`)."""
    from queuetruth.check.run import check as _check

    return _check(backtest, data, **kwargs)


def compare(backtest: Any, live: Any, **kwargs: Any) -> Any:
    """`compare`: a backtest against the live run of the same strategy, order by order: pairs, reasons, scorecard,
    latency (`queuetruth.compare.compare`; returns a `queuetruth.compare.CompareResult`)."""
    from queuetruth.compare.run import compare as _compare

    return _compare(backtest, live, **kwargs)


def fit(backtester: str, **kwargs: Any) -> Any:
    """`fit`: the parameter that fixes `backtester`'s fill model, compared with the library default on held-out data;
    the snippet is written only when the fit passes (`queuetruth.fit.fit`; returns a `queuetruth.fit.FitResult`)."""
    from queuetruth.fit.run import fit as _fit

    return _fit(backtester, **kwargs)


__all__ = [
    "LATENCY_FIT_KEYS",
    "REPLAY_SCHEDULE_FUNCTIONS",
    "Session",
    "WhyExplanation",
    "calibrate",
    "calibrate_live",
    "dataset_kind",
    "dq",
    "export",
    "export_replay_schedule",
    "extract_features",
    "fill_liquidity",
    "fit_hazard",
    "fixtures_synth",
    "import_live",
    "ingest",
    "mirror_real_orders",
    "read_table",
    "referee",
    "referee_fixtures",
    "replay",
    "report",
    "resolve_order_ids",
    "run_id",
    "run_zoo",
    "sample_shadows",
    "score",
    "shadows_from_live",
    "validate_live",
    "why",
    "why_explain",
    "why_suggestion",
]
