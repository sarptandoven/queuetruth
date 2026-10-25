"""Trading fees of Kalshi and Polymarket, exactly as their public documentation states them (read 2026-10-24).

Kalshi (sources in `SOURCES`: the get-series API reference for `fee_type` and `fee_multiplier`; the fee-rounding
page; the general formula of the fee schedule filed with the CFTC on 2022-09-09, `fees = round up(0.07 x C x P x
(1-P))`, whose index markets at `0.035` are the same formula with a multiplier of 0.5):

- model fee = `0.07 x fee_multiplier x share x C x P x (1 - P)`, `C` contracts at price `P` dollars; `share` = 1
  for a taker fill; for a maker fill 0 under `quadratic` (the General Trading Fees Table, which charges takers
  only), 0.25 under `quadratic_with_maker_fees` and 0.5 under `quadratic_with_combo_maker_fees` (get-series:
  "the same maker-fee structure with a 0.5 maker multiplier instead of 0.25"). The current fee schedule PDF
  (kalshi.com/docs/kalshi-fee-schedule.pdf, its Maker Fees section) could not be fetched (HTTP 429), so the 0.25
  rests on that sentence. `flat` is "the Specific Trading Fees Table", which is not published in machine-readable
  form: pass the per-contract amounts yourself (`flat_taker`, `flat_maker`); without them a `flat` series is
  `QT-CONFIG-001`. A market's `fee_waiver_expiration_time` makes fills before it free.
- rounding, per fill: `trade_fee = ceil_6dp(model_fee)`; `aligned = floor_p(revenue - trade_fee)`;
  `rounding_fee = (revenue - trade_fee) - aligned` (precision `p` = $0.0001 for direct members, $0.01 otherwise);
  the rounding fee joins the order's accumulator, which rebates whole multiples of `p` capped so the fill's net fee
  is never negative; `net = trade_fee + rounding_fee - rebate`. Revenue is signed (negative for a buyer). With whole
  contracts at whole-cent prices a non-direct member pays `ceil_cent(model_fee)` per fill before rebates, the old
  "round up to the next cent".

Polymarket (sources in `SOURCES`: the trading-fees, maker-rebates and market-details pages):

- taker fee = `C x rate x p x (1 - p)` in pUSD, rounded to 5 decimals ("Fees are rounded to 5 decimal places. The
  smallest fee charged is 0.00001 USDC. Anything smaller rounds to zero"): the direction is not stated, and only
  rounding down makes every fee under 0.00001 zero, so fees are rounded down. "Makers are never charged fees." A
  market's `feeSchedule.exponent` is documented only as "applied to the price component"; values other than 1 are
  applied to `p x (1 - p)` and flagged.
- maker rebate: per filled maker order `fee_equivalent = C x rate x p x (1 - p)`; daily
  `rebate = your_fee_equivalent / total_fee_equivalent x rebate_pool`, the pool being `rebateRate` (the category's
  maker-rebate share) of the market's taker fees; computed per market; paid at $1 accrued or more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import Literal

from queuetruth.errors import QtError

#: Sources of the formulas (URL, what it gives, date read).
SOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "https://docs.kalshi.com/api-reference/market/get-series",
        "fee_type (its enum and the 0.5 / 0.25 maker multipliers) and fee_multiplier",
        "2026-10-24",
    ),
    (
        "https://docs.kalshi.com/getting_started/fee_rounding",
        "trade fee, rounding fee, the per-order accumulator and rebates, with worked examples",
        "2026-10-24",
    ),
    (
        "https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Kalshi%20Fee%20Schedule%20Filing%20for%20Posting.pdf",
        "fee schedule filed with the CFTC 2022-09-09: fees = round up(0.07 x C x P x (1-P)), the 100-contract table",
        "2026-10-24",
    ),
    (
        "https://docs.polymarket.com/trading/fees",
        "taker fee formula, category rates and rebate shares, 5-decimal precision, 100-share fee tables",
        "2026-10-24",
    ),
    (
        "https://docs.polymarket.com/programs/maker-rebates",
        "fee_equivalent of filled maker orders and the daily rebate share, per market",
        "2026-10-24",
    ),
    (
        "https://docs.polymarket.com/market-data/market-details",
        "a market's feesEnabled and feeSchedule (rate, exponent, takerOnly, rebateRate)",
        "2026-10-24",
    ),
)

KalshiFeeType = Literal["quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees", "flat"]
KALSHI_FEE_TYPES: tuple[str, ...] = (
    "quadratic",
    "quadratic_with_maker_fees",
    "quadratic_with_combo_maker_fees",
    "flat",
)
KALSHI_TAKER_RATE = Decimal("0.07")
#: Maker share of the taker formula per fee type (docs.kalshi.com get-series: 0.25, and 0.5 for combo maker fees).
KALSHI_MAKER_SHARE: dict[str, Decimal] = {
    "quadratic": Decimal(0),
    "quadratic_with_maker_fees": Decimal("0.25"),
    "quadratic_with_combo_maker_fees": Decimal("0.5"),
}
KALSHI_FEE_QUANTUM = Decimal("0.000001")
#: Balance precision: direct members $0.0001, non-direct (FCM-cleared) members $0.01.
KALSHI_PRECISION = {"direct": Decimal("0.0001"), "non_direct": Decimal("0.01")}
POLY_FEE_QUANTUM = Decimal("0.00001")

#: Polymarket category fee rates and maker-rebate shares (docs.polymarket.com/trading/fees, read 2026-10-24).
POLY_CATEGORY_RATES: dict[str, tuple[Decimal, Decimal]] = {
    "crypto": (Decimal("0.07"), Decimal("0.20")),
    "sports": (Decimal("0.05"), Decimal("0.15")),
    "finance": (Decimal("0.04"), Decimal("0.25")),
    "politics": (Decimal("0.04"), Decimal("0.25")),
    "economics": (Decimal("0.05"), Decimal("0.25")),
    "culture": (Decimal("0.05"), Decimal("0.25")),
    "weather": (Decimal("0.05"), Decimal("0.25")),
    "other": (Decimal("0.05"), Decimal("0.25")),
    "mentions": (Decimal("0.04"), Decimal("0.25")),
    "tech": (Decimal("0.04"), Decimal("0.25")),
    "geopolitics": (Decimal(0), Decimal(0)),
    "combos": (Decimal("0.05"), Decimal(0)),
}


def _ceil(x: Decimal, q: Decimal) -> Decimal:
    return (x / q).to_integral_value(rounding=ROUND_CEILING) * q


def _floor(x: Decimal, q: Decimal) -> Decimal:
    return (x / q).to_integral_value(rounding=ROUND_FLOOR) * q


def _check_price(price: Decimal) -> None:
    if not Decimal(0) <= price <= Decimal(1):
        raise QtError("QT-INPUT-002", f"fee price {price} outside [0, 1]")


@dataclass(frozen=True)
class KalshiFeeSchedule:
    """A Kalshi series' fee parameters (`GET /series/{ticker}`: `fee_type`, `fee_multiplier`), the market's fee
    waiver end, and the per-contract amounts of a `flat` series."""

    fee_type: str = "quadratic"
    fee_multiplier: Decimal = Decimal(1)
    flat_taker: Decimal | None = None
    flat_maker: Decimal | None = None
    fee_waiver_until_ns: int | None = None

    def __post_init__(self) -> None:
        if self.fee_type not in KALSHI_FEE_TYPES:
            raise QtError("QT-CONFIG-001", f"kalshi fee_type {self.fee_type!r}: expected one of {KALSHI_FEE_TYPES}")
        object.__setattr__(self, "fee_multiplier", Decimal(str(self.fee_multiplier)))
        if self.fee_multiplier < 0:
            raise QtError("QT-CONFIG-001", f"kalshi fee_multiplier {self.fee_multiplier} is negative")
        if self.fee_type == "flat" and (self.flat_taker is None or self.flat_maker is None):
            raise QtError(
                "QT-CONFIG-001",
                "kalshi fee_type 'flat' is the fee schedule's Specific Trading Fees Table, which queuetruth does not "
                "ship: pass the per-contract flat_taker and flat_maker amounts",
            )

    def model_fee(self, count: Decimal, price: Decimal, *, maker: bool, ts_ns: int | None = None) -> Decimal:
        """Unrounded fee of one fill of `count` contracts at `price` dollars (either leg: `P (1 - P)` is
        symmetric)."""
        _check_price(price)
        if count < 0:
            raise QtError("QT-INPUT-002", f"fee count {count} is negative")
        if self.fee_waiver_until_ns is not None and ts_ns is not None and ts_ns < self.fee_waiver_until_ns:
            return Decimal(0)
        if self.fee_type == "flat":
            per = self.flat_maker if maker else self.flat_taker
            assert per is not None  # checked in __post_init__
            return per * count
        share = KALSHI_MAKER_SHARE[self.fee_type] if maker else Decimal(1)
        return KALSHI_TAKER_RATE * self.fee_multiplier * share * count * price * (Decimal(1) - price)


@dataclass(frozen=True)
class KalshiFillFee:
    """Fee components of one Kalshi fill (dollars)."""

    model_fee: Decimal
    trade_fee: Decimal
    rounding_fee: Decimal
    rebate: Decimal
    net_fee: Decimal
    balance_change: Decimal


@dataclass
class KalshiFeeAccumulator:
    """The per-order fee accumulator of Kalshi's fee rounding (one instance per order; taker and maker fills of the
    order share it)."""

    member: Literal["direct", "non_direct"] = "direct"
    accumulated: Decimal = field(default=Decimal(0))

    def __post_init__(self) -> None:
        if self.member not in KALSHI_PRECISION:
            raise QtError("QT-CONFIG-001", f"kalshi member {self.member!r}: expected 'direct' or 'non_direct'")

    @property
    def precision(self) -> Decimal:
        """Target balance precision in dollars."""
        return KALSHI_PRECISION[self.member]

    def fill(self, revenue: Decimal, model_fee: Decimal) -> KalshiFillFee:
        """Fee of the next fill of the order: `revenue` is the fill's signed cash flow before fees (negative for a
        buyer), `model_fee` the unrounded fee (`KalshiFeeSchedule.model_fee`)."""
        if model_fee < 0:
            raise QtError("QT-INPUT-002", f"model fee {model_fee} is negative")
        p = self.precision
        trade_fee = _ceil(model_fee, KALSHI_FEE_QUANTUM)
        gross = revenue - trade_fee
        aligned = _floor(gross, p)
        rounding_fee = gross - aligned
        self.accumulated += rounding_fee
        cap = trade_fee + rounding_fee
        rebate = _floor(min(self.accumulated, cap), p) if self.accumulated >= p else Decimal(0)
        self.accumulated -= rebate
        net = trade_fee + rounding_fee - rebate
        return KalshiFillFee(model_fee, trade_fee, rounding_fee, rebate, net, aligned + rebate)


@dataclass(frozen=True)
class PolymarketFeeSchedule:
    """A Polymarket market's fee configuration (Gamma `feesEnabled` and `feeSchedule`)."""

    fees_enabled: bool = True
    rate: Decimal = Decimal(0)
    exponent: Decimal = Decimal(1)
    taker_only: bool = True
    rebate_rate: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name in ("rate", "exponent", "rebate_rate"):
            object.__setattr__(self, name, Decimal(str(getattr(self, name))))
        if self.rate < 0 or not Decimal(0) <= self.rebate_rate <= Decimal(1) or self.exponent <= 0:
            raise QtError(
                "QT-CONFIG-001",
                f"polymarket fee schedule rate={self.rate} exponent={self.exponent} rebate_rate={self.rebate_rate}: "
                "rate >= 0, exponent > 0 and rebate_rate in [0, 1] required",
            )

    @classmethod
    def for_category(cls, category: str) -> PolymarketFeeSchedule:
        """The documented taker rate and maker-rebate share of a category (`POLY_CATEGORY_RATES`)."""
        words = re.findall(r"[a-z]+", category.lower())
        key = words[0] if words else ""
        if key == "general":
            key = "other"
        if key not in POLY_CATEGORY_RATES:
            raise QtError(
                "QT-CONFIG-001", f"polymarket category {category!r}: expected one of {sorted(POLY_CATEGORY_RATES)}"
            )
        rate, rebate = POLY_CATEGORY_RATES[key]
        return cls(fees_enabled=rate > 0, rate=rate, rebate_rate=rebate)

    @property
    def documented(self) -> bool:
        """False when `exponent` is not 1 (the documented formula)."""
        return self.exponent == 1

    def fee_equivalent(self, size: Decimal, price: Decimal) -> Decimal:
        """Unrounded `C x rate x (p (1 - p))^exponent` (the taker fee before rounding, and a maker's fee
        equivalent for rebates)."""
        _check_price(price)
        if not self.fees_enabled or self.rate == 0:
            return Decimal(0)
        core = price * (Decimal(1) - price)
        if self.exponent != 1:
            core = core**self.exponent
        return size * self.rate * core

    def taker_fee(self, size: Decimal, price: Decimal) -> Decimal:
        """Taker fee in pUSD rounded down to 5 decimals."""
        return self.fee_equivalent(size, price).quantize(POLY_FEE_QUANTUM, rounding=ROUND_DOWN)

    def maker_fee(self, size: Decimal, price: Decimal) -> Decimal:
        """0 when `taker_only` ("Makers are never charged fees"), else the taker formula."""
        return Decimal(0) if self.taker_only else self.taker_fee(size, price)


def maker_rebate(your_fee_equivalent: Decimal, total_fee_equivalent: Decimal, pool: Decimal) -> Decimal:
    """`your / total x pool` (0 when the market had no maker fee equivalent)."""
    if your_fee_equivalent < 0 or total_fee_equivalent < 0 or pool < 0:
        raise QtError("QT-INPUT-002", "maker rebate inputs must be non-negative")
    if total_fee_equivalent == 0:
        return Decimal(0)
    if your_fee_equivalent > total_fee_equivalent:
        raise QtError(
            "QT-INPUT-002",
            f"your fee equivalent {your_fee_equivalent} exceeds the market total {total_fee_equivalent}",
        )
    return your_fee_equivalent / total_fee_equivalent * pool


__all__ = [
    "KALSHI_FEE_TYPES",
    "KALSHI_MAKER_SHARE",
    "KALSHI_PRECISION",
    "KALSHI_TAKER_RATE",
    "POLY_CATEGORY_RATES",
    "SOURCES",
    "KalshiFeeAccumulator",
    "KalshiFeeSchedule",
    "KalshiFillFee",
    "PolymarketFeeSchedule",
    "maker_rebate",
]
