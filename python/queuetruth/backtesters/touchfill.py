"""The fill probability of a bar backtest's limit order, from the atlas touch tables, for the fill models of
`queuetruth.backtrader` and `queuetruth.lean`.

When a bar's prices reach a resting limit order (the `touch` rule, or `penetrate_<k>`: k ticks through it), a bar
backtester fills it. The touch tables (`queuetruth.atlas.touch`, docs/concepts/touch-tables.md) measure how often
the real queue had filled such an order by the end of that bar: `P(real | rule)`, by liquidity tier, side and bar
length. `TouchFill` looks that number up and draws, for each order and bar the rule fires on, whether the fill
happens. It never invents a probability: a tier, bar length, rule or side the table lacks, or a cell without a
value, is refused with `QT-CONFIG-001`.

The draws are reproducible: each is a hash of the seed, the order's identity and the bar, not a random stream, so
the same seed and the same orders give the same fills in any order of calls.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError

#: bar length in seconds -> the touch tables' name for it
BAR_NAMES = {60: "1m", 300: "5m", 3600: "1h"}


@dataclass(frozen=True)
class FillDecision:
    """One draw: the order, the bar, the probability used and whether it filled."""

    order: str
    bar_time: str
    side: str
    limit: float
    p: float
    u: float
    filled: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_tables(table: Any) -> Any:
    """A `TouchTables` from a `touch.json` path, a `TouchTables` or its dict (`QT-INPUT-001` / `QT-CONFIG-001`)."""
    from pydantic import ValidationError

    from queuetruth.atlas.touch import TouchTables

    if isinstance(table, TouchTables):
        return table
    if table is None:
        raise QtError(
            "QT-CONFIG-001",
            "touch fills need the atlas touch tables: pass table= a touch.json file or a TouchTables",
            "the atlas publishes them as atlas/v1/touch.json (docs/concepts/touch-tables.md)",
        )
    try:
        if isinstance(table, dict):
            return TouchTables.model_validate(table)
        p = Path(table)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{p}: {e}") from e
        return TouchTables.model_validate_json(text)
    except ValidationError as e:
        raise QtError("QT-CONFIG-001", f"touch tables: not a qt.atlas.touch document ({e.errors()[0]['msg']})") from e


def bar_name(seconds: float | None) -> str:
    """`1m`, `5m` or `1h` for a bar length in seconds (`QT-CONFIG-001` for any other)."""
    s = round(seconds) if seconds else None
    if s not in BAR_NAMES:
        shown = "unknown" if seconds is None else f"{seconds:g} s"
        raise QtError(
            "QT-CONFIG-001",
            f"touch fills: the touch tables have 1m, 5m and 1h bars, not {shown}",
            "run the backtest on 1-minute, 5-minute or 1-hour bars, or pass bar='1m' / '5m' / '1h' if it is one",
        )
    return BAR_NAMES[s]


def rule_ticks(rule: str) -> int:
    """0 for `touch`, k for `penetrate_<k>` (`QT-CONFIG-001` otherwise)."""
    if rule == "touch":
        return 0
    head, _, k = rule.partition("_")
    if head == "penetrate" and k.isdigit() and int(k) > 0:
        return int(k)
    raise QtError("QT-CONFIG-001", f"touch fills: rule must be touch or penetrate_<k>, got {rule!r}")


class TouchFill:
    """`P(real | rule)` lookups and seeded draws for one tier and rule of a touch table.

    `table`: a `touch.json` path, a `TouchTables` or its dict. `tier`: a liquidity tier of the table (`ALL` pools
    them). `rule`: `touch` (default) or `penetrate_<k>`. `seed`: the draws' seed. `tick`: the tick size, needed for a
    `penetrate_<k>` rule (the order fills only when the bar goes k ticks through its limit)."""

    def __init__(self, table: Any, *, tier: str, rule: str = "touch", seed: int = 0, tick: float | None = None) -> None:
        self.tables = load_tables(table)
        self.tier = str(tier)
        self.rule = rule
        self.k = rule_ticks(rule)
        self.seed = int(seed)
        if self.k and not (tick and tick > 0):
            raise QtError("QT-CONFIG-001", f"touch fills: rule {rule} needs the tick size (tick=...)")
        self.tick = float(tick) if tick else None
        rows = self.tables.rows
        tiers = sorted({r.tier for r in rows})
        if self.tier not in tiers:
            raise QtError(
                "QT-CONFIG-001",
                f"touch fills: the touch table has no tier {self.tier!r} (tiers: {', '.join(tiers) or 'none'})",
            )
        rules = sorted({r.rule for r in rows if r.tier == self.tier})
        if rule not in rules:
            raise QtError(
                "QT-CONFIG-001",
                f"touch fills: the touch table has no rule {rule!r} for tier {self.tier} (rules: {', '.join(rules)})",
            )
        self.decisions: list[FillDecision] = []
        self._p: dict[tuple[str, str], float] = {}

    @property
    def bars(self) -> list[str]:
        """Bar lengths the table has for this tier and rule."""
        return sorted({r.bar for r in self.tables.rows if r.tier == self.tier and r.rule == self.rule})

    def probability(self, bar: str, side: str) -> float:
        """`P(real | rule)` for a bar length (`1m`, `5m`, `1h`) and side (`buy`, `sell`); refuses what the table
        lacks."""
        key = (bar, side)
        if key in self._p:
            return self._p[key]
        if bar not in self.bars:
            raise QtError(
                "QT-CONFIG-001",
                f"touch fills: the touch table has no {bar} bars for tier {self.tier}, rule {self.rule} "
                f"(bars: {', '.join(self.bars) or 'none'})",
            )
        row = self.tables.row(tier=self.tier, side=side, bar=bar, rule=self.rule)
        if row is None:
            raise QtError(
                "QT-CONFIG-001",
                f"touch fills: the touch table has no {side} row for tier {self.tier}, {bar} bars, rule {self.rule}",
            )
        p = row.p_real_given_rule.value
        if p is None or not 0.0 <= p <= 1.0:
            raise QtError(
                "QT-CONFIG-001",
                f"touch fills: the touch table has no P(real | {self.rule}) for tier {self.tier}, {side}, {bar} bars "
                f"({row.orders} orders); no probability is assumed",
            )
        self._p[key] = float(p)
        return float(p)

    def uniform(self, order: str, bar_time: str) -> float:
        """The seeded uniform number in [0, 1) of one order and bar."""
        h = hashlib.sha256(f"{self.seed}|{order}|{bar_time}".encode()).digest()
        return int.from_bytes(h[:8], "big") / 2.0**64

    def penetrated(self, side: str, limit: float, low: float, high: float) -> bool:
        """Whether a bar with this low and high fires the rule for a limit order (always true for `touch`, whose
        reach the backtester has checked)."""
        if not self.k:
            return True
        assert self.tick is not None
        edge = self.k * self.tick - 1e-9 * self.tick
        return low <= limit - edge if side == "buy" else high >= limit + edge

    def decide(self, *, order: str, bar_time: str, side: str, limit: float, bar: str) -> FillDecision:
        """Draws whether the real queue fills `order` in this bar, records it and returns it."""
        p = self.probability(bar, side)
        u = self.uniform(order, bar_time)
        d = FillDecision(order, bar_time, side, float(limit), p, u, u < p)
        self.decisions.append(d)
        return d

    def summary(self) -> dict[str, Any]:
        """Draws made and fills given, overall."""
        n = len(self.decisions)
        filled = sum(d.filled for d in self.decisions)
        return {
            "tier": self.tier,
            "rule": self.rule,
            "seed": self.seed,
            "draws": n,
            "filled": filled,
            "declined": n - filled,
            "atlas_root": self.tables.atlas_root,
            "source": self.tables.source,
        }


__all__ = ["BAR_NAMES", "FillDecision", "TouchFill", "bar_name", "load_tables", "rule_ticks"]
