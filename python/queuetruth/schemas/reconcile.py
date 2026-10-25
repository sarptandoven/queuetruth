"""The trading-bot reconciliation document `qt.bot_reconcile` (`reconcile.json` of `reconcile_freqtrade` /
`reconcile_hummingbot`, docs/design/extensions/freqtrade-hummingbot.md) and its parts. The pairing and the
explanations are computed in `queuetruth.live.importers.freqtrade`, which re-exports every name here.

This module imports only Pydantic and `queuetruth.schemas.models` base classes, so `queuetruth.schemas.models` can
register the document in `DOC_MODELS` at import time.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from queuetruth.schemas.models import QtDoc, QtModel, _tag

Category = Literal[
    "match",
    "missed_fill",
    "extra_live_fill",
    "partial_fill",
    "price",
    "timing",
    "both_unfilled",
    "stop_not_triggered",
    "no_sim_twin",
    "no_live_twin",
]


class ReconcileOrder(QtModel):
    """One row of the reconciliation: a live order, a simulator order, or a paired pair of them."""

    live_key: str | None
    sim_key: str | None
    instrument: str
    side: Literal["buy", "sell"]
    role: str
    category: Category
    issues: list[str]
    score_class: str | None = None
    live_status: str | None = None
    sim_status: str | None = None
    qty: str
    live_limit_px: str | None = None
    sim_limit_px: str | None = None
    live_filled_qty: str | None = None
    sim_filled_qty: str | None = None
    live_avg_px: str | None = None
    sim_avg_px: str | None = None
    live_placed: str | None = None
    live_first_fill: str | None = None
    sim_first_fill: str | None = None
    #: live first fill minus simulator first fill (ns; null unless both filled).
    fill_delay_ns: int | None = None
    #: price advantage of the simulator in basis points of the live price (positive = the simulator bought cheaper
    #: or sold dearer than live); null unless both filled with prices.
    sim_price_advantage_bps: float | None = None
    #: the same advantage in quote currency over the jointly filled quantity (decimal text).
    sim_price_advantage_quote: str | None = None
    live_note: str | None = None
    sim_note: str | None = None
    explanation: str


class ReconcileScore(QtModel):
    """The `score` result the reconciliation ran (sim arm = the simulator log); `path` is relative to the
    reconciliation's output directory."""

    path: str
    run_id: str
    headline: str
    n_orders: int
    n_matched: int
    n_unmatched_live: int
    n_unmatched_sim: int
    two_by_two: dict[str, int]
    fill_rate_bias: float | None
    #: clusters of the scorer's cluster-robust discordance test (null when it did not run).
    n_clusters: int | None = None
    #: false when the score rests on fewer than `MIN_SCORE_CLUSTERS` clusters or `MIN_SCORE_MATCHED` matched orders
    #: (`queuetruth.live.importers.freqtrade`).
    interpretable: bool = True


class ReconcileReport(QtDoc):
    """`reconcile.json` (`qt.bot_reconcile`): counts, price advantage, the score result and every order row (sorted).
    `run_id` hashes the spec (`options`: every input as a sha256, every option) with the engine version (SPEC 7.3);
    the log paths are relative to the output directory, so the document does not depend on where it was written."""

    SCHEMA: ClassVar[str] = "qt.bot_reconcile"
    schema_: Literal["qt.bot_reconcile"] = _tag("qt.bot_reconcile")
    schema_version: Literal[1] = 1
    kind: Literal["freqtrade", "hummingbot"]
    run_id: str
    live_log: str
    sim_log: str
    n_live_orders: int
    n_sim_orders: int
    n_paired: int
    #: n_paired / n_live_orders (null without live orders): low coverage means the two runs diverged.
    pairing_coverage: float | None = None
    counts: dict[str, int]
    #: simulator price advantage in quote currency summed per instrument over jointly filled quantity.
    sim_price_advantage_quote: dict[str, str]
    #: notional (qty * limit price) of missed fills per instrument: orders the simulator filled and live did not.
    missed_fill_notional: dict[str, str]
    score: ReconcileScore | None = None
    score_error: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str]
    orders: list[ReconcileOrder]


#: The reconciliation documents by schema name (merged into `queuetruth.schemas.models.DOC_MODELS`).
RECONCILE_DOC_MODELS: dict[str, type[QtDoc]] = {ReconcileReport.SCHEMA: ReconcileReport}

__all__ = ["RECONCILE_DOC_MODELS", "Category", "ReconcileOrder", "ReconcileReport", "ReconcileScore"]
