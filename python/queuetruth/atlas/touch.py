"""`atlas/v1/touch.json`: the touch tables for bar-based backtests (docs/concepts/touch-tables.md).

For the atlas's shadow orders resting as day orders, a bar rule (`touch`: the bar's low reaches a buy's limit, or
its high a sell's; `penetrate_k`: it goes `k` ticks through) against the exact replay of the real queue, by
liquidity tier, side and bar length. Rates are fractions of orders (0 to 1), markouts are in ticks. Every value has
a 95% interval from the atlas's bootstrap over dates and symbols; a value or bound is null when it is undefined
(no order, or a single date and symbol).

    from queuetruth.atlas.touch import TouchTables
    doc = TouchTables.model_validate_json(path.read_text())
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from queuetruth.schemas.models import QtDoc, QtModel

SCHEMA = "qt.atlas.touch"
#: bar lengths as they are written (`bar`) and in seconds (`bar_s`)
BARS = {"1m": 60, "5m": 300, "1h": 3600}


class Estimate(QtModel):
    """A value with its 95% interval (null when undefined)."""

    value: float | None
    ci_lo: float | None
    ci_hi: float | None


class TouchRow(QtModel):
    """One (tier, side, bar length, rule) cell."""

    tier: str = Field(description="liquidity tier, or ALL")
    side: Literal["buy", "sell", "all"]
    bar: str = Field(description="bar length: 1m, 5m or 1h")
    bar_s: int = Field(description="bar length in seconds")
    rule: str = Field(description="touch, or penetrate_<k> (k ticks through the limit)")
    orders: int = Field(description="orders placed")
    dates: int
    symbols: int
    real_fill_rate: Estimate = Field(description="share of orders the real queue filled before the close")
    rule_fill_rate: Estimate = Field(description="share of orders the rule filled before the close")
    p_real_given_rule: Estimate = Field(
        description="of the rule's fills, the share the real queue had filled by the end of the same bar"
    )
    p_rule_given_real: Estimate = Field(
        description="of the real fills, the share the rule had filled by the end of the same bar"
    )
    real_markout_ticks: Estimate = Field(description="average markout of the real fills one bar later, in ticks")
    rule_markout_ticks: Estimate = Field(description="average markout of the rule's fills one bar later, in ticks")
    pnl_overstated_ticks: Estimate = Field(
        description="the rule's markout P&L minus the real one per order placed, in ticks (positive: too high)"
    )
    flagged_share: float | None = Field(
        description="share of the orders on symbol-days that are not exact_ok (0 under flag policy exclude)"
    )


class TouchTables(QtDoc):
    """The document: the atlas it comes from, the days it used and every row."""

    SCHEMA = SCHEMA
    schema_: Literal["qt.atlas.touch"] = Field(default=SCHEMA, alias="schema")
    schema_version: Literal[1] = 1
    atlas_root: str
    source: str = Field(description="venue whose prints and queue are used: iex or nasdaq")
    population: str = Field(description="the atlas shadow population")
    days: list[str] = Field(description="the days the tables pool (ISO dates); days reduced before them are left out")
    flag_policy: str
    definition: str = Field(description="docs page with the exact definitions")
    attribution: str | None = None
    rows: list[TouchRow]

    def row(self, *, tier: str = "ALL", side: str = "all", bar: str = "1m", rule: str = "touch") -> TouchRow | None:
        """The row of one cell, or None."""
        return next(
            (r for r in self.rows if (r.tier, r.side, r.bar, r.rule) == (tier, side, bar, rule)),
            None,
        )


#: Registered in `queuetruth.schemas.DOC_MODELS` by `queuetruth.atlas._registry`; `make schemas` writes its schema.
DOC_MODELS = {SCHEMA: TouchTables}

__all__ = ["BARS", "DOC_MODELS", "SCHEMA", "Estimate", "TouchRow", "TouchTables"]
