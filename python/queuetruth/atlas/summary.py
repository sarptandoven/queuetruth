"""`atlas/v1/summary.json` (`qt.atlas_summary`): every fill model of the atlas against the exact replay, per mode,
shadow population, liquidity tier and horizon, in human units (percent, percentage points, ticks, `1min`)."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from queuetruth.atlas._parts import Interval, Library, Verdict
from queuetruth.schemas.models import QtDoc, QtModel, _tag


class SummaryRow(QtModel):
    """One model in one (mode, shadow population, tier, horizon)."""

    library: Library
    library_name: str
    #: the human label, e.g. "NautilusTrader default (touch fill)"
    label: str
    model_id: str
    #: a library setting a reader of an L2 backtest can pick (not a bound, not hftbacktest's order-level model)
    is_setting: bool
    mode: str
    shadow_population: str
    tier: str
    tier_name: str
    #: `1s`, `10s`, `1min`
    horizon: str
    horizon_seconds: float
    #: the model's fill rate minus the exact replay's, in percentage points
    fill_rate_bias_pp: Interval
    exact_fill_rate_pct: Interval
    model_fill_rate_pct: float | None
    verdict: Verdict
    #: shadow orders, symbols and trading days behind the row
    n: int
    n_symbols: int | None
    n_dates: int | None
    #: simulated minus exact P&L per order placed, in ticks, each first fill marked to the mid `horizon` later
    #: (null when the atlas has no P&L at this horizon)
    pnl_bias_ticks: Interval | None
    #: share of the row's orders on flagged symbol-days (0 under the published flag policy `exclude`)
    flagged_share: float | None


class AtlasSummary(QtDoc):
    """`summary.json`: every row of the atlas's leaderboard, sorted by mode, population, tier, library, model id and
    horizon."""

    SCHEMA: ClassVar[str] = "qt.atlas_summary"
    schema_: Literal["qt.atlas_summary"] = _tag("qt.atlas_summary")
    schema_version: Literal[1] = 1
    venue: str
    atlas_root: str
    #: the horizon the site's chart, sentences and configs use
    headline_horizon: str
    rows: list[SummaryRow] = Field(default_factory=list)


DOC_MODELS = {AtlasSummary.SCHEMA: AtlasSummary}

__all__ = ["DOC_MODELS", "AtlasSummary", "SummaryRow"]
