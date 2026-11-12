"""A hand-built `TouchTables` (synthetic numbers, no market data) for the fill-model tests."""

from __future__ import annotations

from queuetruth.atlas.touch import BARS, Estimate, TouchRow, TouchTables

#: P(real | rule) of each synthetic cell: (tier, side, bar, rule) -> value; None leaves the cell without a value
P = {
    ("T1", "buy", "1m", "touch"): 0.25,
    ("T1", "sell", "1m", "touch"): 0.40,
    ("T1", "buy", "5m", "touch"): 0.50,
    ("T1", "sell", "5m", "touch"): 0.60,
    ("T1", "buy", "1m", "penetrate_1"): 0.90,
    ("T1", "sell", "1m", "penetrate_1"): 0.95,
    ("T2", "buy", "1m", "touch"): None,
    ("T2", "sell", "1m", "touch"): 0.10,
}


def _est(v: float | None) -> Estimate:
    return Estimate(value=v, ci_lo=None if v is None else v * 0.9, ci_hi=None if v is None else min(1.0, v * 1.1))


def tables(p: dict[tuple[str, str, str, str], float | None] | None = None) -> TouchTables:
    rows = [
        TouchRow(
            tier=tier,
            side=side,  # type: ignore[arg-type]
            bar=bar,
            bar_s=BARS[bar],
            rule=rule,
            orders=1000,
            dates=5,
            symbols=20,
            real_fill_rate=_est(0.3),
            rule_fill_rate=_est(0.6),
            p_real_given_rule=_est(v),
            p_rule_given_real=_est(0.9),
            real_markout_ticks=_est(-1.0),
            rule_markout_ticks=_est(0.5),
            pnl_overstated_ticks=_est(0.4),
            flagged_share=0.0,
        )
        for (tier, side, bar, rule), v in (P if p is None else p).items()
    ]
    return TouchTables(
        atlas_root="0" * 64,
        source="synthetic",
        population="synthetic",
        days=["2000-01-03"],
        flag_policy="exclude",
        definition="docs/concepts/touch-tables.md",
        rows=rows,
    )
