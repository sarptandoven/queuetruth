"""Shared result types of the venue readers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from queuetruth.live.builder import LogBuilder
from queuetruth.pm.batches import BookMsg, TradeMsg
from queuetruth.pm.fees import KalshiFeeSchedule, PolymarketFeeSchedule
from queuetruth.pm.rewards import LipProgram, PriceGrid


@dataclass
class MarketInfo:
    """What the captures say about one market (instrument)."""

    symbol: str
    venue: str
    size_exp: int
    currency: str
    grid: PriceGrid | None = None
    kalshi_fee: KalshiFeeSchedule | None = None
    poly_fee: PolymarketFeeSchedule | None = None
    settlement: Decimal | None = None
    settled_ns: int | None = None
    lip: list[LipProgram] = field(default_factory=list)
    poly_rewards: list[dict[str, Any]] = field(default_factory=list)
    yes_asset: str | None = None
    no_asset: str | None = None
    condition_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        """JSON-ready description (`markets.json`)."""
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "venue": self.venue,
            "size_exp": self.size_exp,
            "currency": self.currency,
            "grid": [list(b) for b in self.grid.bands] if self.grid else None,
            "settlement": None if self.settlement is None else format(self.settlement, "f"),
            "settled_ns": self.settled_ns,
            "yes_asset": self.yes_asset,
            "no_asset": self.no_asset,
            "condition_id": self.condition_id,
        }
        if self.kalshi_fee is not None:
            f = self.kalshi_fee
            out["kalshi_fee"] = {
                "fee_type": f.fee_type,
                "fee_multiplier": format(f.fee_multiplier, "f"),
                "flat_taker": None if f.flat_taker is None else format(f.flat_taker, "f"),
                "flat_maker": None if f.flat_maker is None else format(f.flat_maker, "f"),
                "fee_waiver_until_ns": f.fee_waiver_until_ns,
            }
        if self.poly_fee is not None:
            p = self.poly_fee
            out["poly_fee"] = {
                "fees_enabled": p.fees_enabled,
                "rate": format(p.rate, "f"),
                "exponent": format(p.exponent, "f"),
                "taker_only": p.taker_only,
                "rebate_rate": format(p.rebate_rate, "f"),
            }
        out["lip"] = [
            {
                "program_id": g.program_id,
                "start_ns": g.start_ns,
                "end_ns": g.end_ns,
                "period_reward": format(g.period_reward, "f"),
                "target_size": g.target_size,
                "discount_factor": format(g.discount_factor, "f"),
                "max_reward_per_account": None
                if g.max_reward_per_account is None
                else format(g.max_reward_per_account, "f"),
            }
            for g in self.lip
        ]
        out["poly_rewards"] = list(self.poly_rewards)
        out["meta"] = dict(self.meta)
        return out


#: Reader counters that are data-quality anomalies of the written datasets (`qt.data_quality.source_specific`).
ANOMALY_COUNTERS = ("duplicate_subscription_messages",)


@dataclass(frozen=True)
class QueueSample:
    """One official queue position (Kalshi `queue_position_fp`) of an own resting order, in size units."""

    order_id: str
    market: str | None
    queue_position: int
    recv_ns: int
    send_ns: int | None
    location: str


@dataclass
class VenueCapture:
    """Everything a venue reader extracted from the user's captures."""

    venue: str
    book: dict[str, list[BookMsg]] = field(default_factory=dict)
    trades: dict[str, list[TradeMsg]] = field(default_factory=dict)
    markets: dict[str, MarketInfo] = field(default_factory=dict)
    samples: list[QueueSample] = field(default_factory=list)
    builder: LogBuilder | None = None
    counters: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    inputs: list[Path] = field(default_factory=list)
    sequence_gaps: int = 0
    sequence_regressions: int = 0
    cids: dict[str, set[str]] = field(default_factory=dict)
    first_ns: int | None = None

    def count(self, key: str, n: int = 1) -> None:
        """Adds to a counter."""
        self.counters[key] = self.counters.get(key, 0) + n

    def warn(self, msg: str) -> None:
        """Adds a warning once."""
        if msg not in self.warnings:
            self.warnings.append(msg)


__all__ = ["ANOMALY_COUNTERS", "MarketInfo", "QueueSample", "VenueCapture"]
