"""Maker-reward formulas: Kalshi's Liquidity Incentive Program and Polymarket's liquidity rewards (read 2026-10-24).

Kalshi LIP, transcribed from the program terms filed with the CFTC ("Liquidity Incentive Program Amendment",
filed 2026-10-14, effective no earlier than 2026-10-28) and the LIP help page (URLs in `SOURCES`):

- A snapshot is taken once per second at a uniformly random moment. Per side (YES bids; NO bids, i.e. YES asks
  counted as NO bids), walk the price levels from the best bid down: add the level's whole size to the qualifying
  total and every bid at that price to the qualifying bids; the first level where the total reaches one fifth of the
  Target Size is the Reference Price; stop when the total reaches the Target Size. If the levels run out first the
  side has no qualifying bids. A snapshot is excluded unless both sides reach the Target Size (and the market is
  open).
- `score(bid) = DF^N x size(bid)`, `N` = ticks from the Reference Price down to the bid's price (0 at or above it);
  scores are normalized per side; a user's snapshot score is the sum of its normalized YES and NO scores (at most
  2.0 over all users).
- Time-period score = user's snapshot scores / all users' snapshot scores; payout = that x period reward x
  (non-excluded / total snapshots), paid when at least $1.00, rounded down to the cent. Every included snapshot
  sums to exactly 2 over users, so `payout = reward x sum(user snapshot scores) / (2 x total snapshots)`: linear in
  the snapshot scores. With uniformly random snapshot times the expected payout is that sum with each snapshot
  replaced by the time average of the score over its second (`kalshi_expected_payout`).

Polymarket liquidity rewards (the liquidity-rewards page, `SOURCES`):

- `S(v, s) = ((v - s) / v)^2 x b` for an order `s` cents from the size-cutoff-adjusted midpoint, `v` the market's
  max spread in cents, `b` the in-game multiplier; orders below the min size or at `s >= v` score 0.
- `Q_one` = sum of `S x size` over bids on the market and asks on its complement, `Q_two` over asks and complement
  bids (in YES terms: YES bids and YES asks). `Q_min = max(min(Q_one, Q_two), max(Q_one / c, Q_two / c))` when the
  midpoint is in [0.10, 0.90], else `min(Q_one, Q_two)`; `c = 3.0`. Normalized per sample over makers; the book is
  sampled once a minute at a random offset (1,440 samples per UTC day); the epoch share is the sum over samples
  divided by the sum over all makers, times the market's daily reward.
- From an L2 book only the user's own `Q_min` is exact; other makers' liquidity is aggregated. With `Q1`, `Q2` the
  aggregated others' side scores (`Q_long`, `Q_short` the larger and smaller) and any split of them over makers, the
  sum of the other makers' `Q_min` lies in `[max(Q_long / c, (Q1 + Q2) / (c + 1)), max(Q_short + (Q_long -
  Q_short) / c, (Q1 + Q2) / c)]` in range and in `[0, Q_short]` out of range (`competition_bounds`), assuming all
  of their resting size meets the min size. Both ends are reached: the lower by makers whose sides are `c` to 1
  apart, the upper by one maker holding a balanced part plus one-sided makers for the rest (`c >= 2`) or by
  one-sided makers only (`c < 2`). The point estimate treats the rest of the book as one maker. The adjusted
  midpoint is taken between the best bid and ask levels whose aggregated size reaches the min size (an L2 reading
  of "size-cutoff-adjusted").
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from functools import lru_cache

from queuetruth.errors import QtError
from queuetruth.pm.units import ONE

#: Sources of the formulas (URL, what it gives, date read).
SOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Liquidity%20Incentive%20Program%20Amendment.pdf",
        "LIP terms filed 2026-10-14: snapshots, reference price, discount factor, normalization, payout",
        "2026-10-24",
    ),
    (
        "https://help.kalshi.com/incentive-programs/liquidity-incentive-program",
        "the LIP help page",
        "2026-10-24",
    ),
    (
        "https://docs.kalshi.com/api-reference/incentive-programs/get-incentives",
        "program fields: period_reward and max_reward_per_account in centi-cents, target_size_fp, discount_factor_bps",
        "2026-10-24",
    ),
    (
        "https://docs.polymarket.com/programs/liquidity-rewards",
        "S(v, s), Q_one, Q_two, Q_min with c = 3.0, 1,440 samples a day, the worked example",
        "2026-10-24",
    ),
)
#: Polymarket's one-sided scaling factor "c" ("currently 3.0 on all markets").
POLY_C = 3.0
#: Polymarket samples per UTC day (once a minute).
POLY_SAMPLES_PER_DAY = 1440
POLY_SAMPLE_NS = 60_000_000_000
KALSHI_SNAPSHOT_NS = 1_000_000_000
#: Polymarket's two-sided range of the midpoint, [0.10, 0.90], in price units.
_MID_LO, _MID_HI = ONE // 10, 9 * ONE // 10


@dataclass(frozen=True)
class PriceGrid:
    """A market's price grid as bands `(start, end, step)` in price units (Kalshi `price_ranges`; a Polymarket
    market is one band of its tick size). Ticks are counted on this grid."""

    bands: tuple[tuple[int, int, int], ...]

    def __post_init__(self) -> None:
        if not self.bands:
            raise QtError("QT-CONFIG-001", "price grid has no bands")
        prev_end = None
        for start, end, step in self.bands:
            if step <= 0 or end <= start or (end - start) % step:
                raise QtError("QT-CONFIG-001", f"price band ({start}, {end}, {step}) is not a whole number of steps")
            if prev_end is not None and start != prev_end:
                raise QtError("QT-CONFIG-001", "price bands must be contiguous and in increasing order")
            prev_end = end

    @classmethod
    def uniform(cls, tick: int) -> PriceGrid:
        """One band `[0, 1]` with step `tick`."""
        if tick <= 0 or ONE % tick:
            raise QtError("QT-CONFIG-001", f"tick {tick} does not divide $1.00 in price units")
        return cls(((0, ONE, tick),))

    @property
    def finest(self) -> int:
        """The smallest step."""
        return min(b[2] for b in self.bands)

    def index(self, price: int) -> int:
        """Number of grid steps from the grid start to `price` (`QT-INPUT-002` if `price` is off the grid)."""
        n = 0
        for start, end, step in self.bands:
            if price <= end:
                d = price - start
                if d < 0 or d % step:
                    raise QtError("QT-INPUT-002", f"price {price} is not on the market's price grid")
                return n + d // step
            n += (end - start) // step
        raise QtError("QT-INPUT-002", f"price {price} is above the market's price grid")

    def on_grid(self, price: int) -> bool:
        """True when `price` is a grid point."""
        try:
            self.index(price)
        except QtError:
            return False
        return True

    def ticks(self, hi: int, lo: int) -> int:
        """Grid steps from `lo` up to `hi` (0 when `hi <= lo`)."""
        return max(self.index(hi) - self.index(lo), 0)


# ---- Kalshi LIP -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LipProgram:
    """One Kalshi liquidity incentive time period of one market (`GET /incentive_programs` item, or given by hand).
    `target_size` is in size units; `period_reward` and `max_reward_per_account` in dollars."""

    market_ticker: str
    start_ns: int
    end_ns: int
    period_reward: Decimal
    target_size: int
    discount_factor: Decimal
    max_reward_per_account: Decimal | None = None
    program_id: str = ""

    def __post_init__(self) -> None:
        if self.end_ns <= self.start_ns:
            raise QtError("QT-CONFIG-001", f"LIP program {self.program_id}: end must be after start")
        if self.target_size <= 0:
            raise QtError("QT-CONFIG-001", f"LIP program {self.program_id}: target size must be positive")
        if not Decimal(0) <= Decimal(self.discount_factor) <= Decimal(1):
            raise QtError("QT-CONFIG-001", f"LIP program {self.program_id}: discount factor must be in [0, 1]")
        if self.period_reward < 0:
            raise QtError("QT-CONFIG-001", f"LIP program {self.program_id}: reward must be non-negative")

    @property
    def total_snapshots(self) -> int:
        """Snapshots in the time period (one per second; periods start and end on whole seconds)."""
        return -(-(self.end_ns - self.start_ns) // KALSHI_SNAPSHOT_NS)


@dataclass(frozen=True)
class LipSide:
    """One side of one LIP snapshot: the user's score, the side's total score, whether it reached the target, and
    the Reference Price (in that side's own leg prices)."""

    user: float
    total: float
    qualified: bool
    reference: int | None

    @property
    def share(self) -> float:
        """The user's normalized score on this side."""
        return self.user / self.total if self.qualified and self.total > 0 else 0.0


def lip_side(
    levels: Sequence[tuple[int, int]],
    own: dict[int, int],
    target: int,
    discount: float,
    grid: PriceGrid,
) -> LipSide:
    """One side of a LIP snapshot. `levels` are `(bid price, total size)` in that side's own leg prices, best
    (highest) first, totals including the user's own resting size `own[price]`."""
    total = 0
    ref: int | None = None
    qual: list[tuple[int, int]] = []
    reached = False
    for px, size in levels:
        if size <= 0:
            continue
        total += size
        qual.append((px, size))
        if ref is None and 5 * total >= target:
            ref = px
        if total >= target:
            reached = True
            break
    if not reached or ref is None:
        return LipSide(0.0, 0.0, False, None)
    score_total = 0.0
    score_user = 0.0
    for px, size in qual:
        n = grid.ticks(ref, px) if px < ref else 0
        w = discount**n
        score_total += w * size
        mine = own.get(px, 0)
        if mine > size:
            raise QtError("QT-INPUT-002", f"own resting size {mine} exceeds the level size {size} at price {px}")
        score_user += w * mine
    return LipSide(score_user, score_total, True, ref)


def lip_snapshot(
    yes_bids: Sequence[tuple[int, int]],
    yes_asks: Sequence[tuple[int, int]],
    own_bids: dict[int, int],
    own_asks: dict[int, int],
    program: LipProgram,
    grid: PriceGrid,
) -> tuple[float, bool]:
    """(user's snapshot score, included) of a canonical YES book: `yes_bids` best (highest) first, `yes_asks`
    best (lowest) first, both `(YES price, total size)`; `own_*` the user's resting size per YES price. YES asks
    are the NO side's bids at NO price `ONE - p`."""
    df = float(program.discount_factor)
    yes = lip_side(yes_bids, own_bids, program.target_size, df, grid)
    no_levels = [(ONE - p, s) for p, s in yes_asks]
    no_own = {ONE - p: s for p, s in own_asks.items()}
    no = lip_side(no_levels, no_own, program.target_size, df, _mirror(grid))
    included = yes.qualified and no.qualified
    return (yes.share + no.share if included else 0.0), included


@lru_cache(maxsize=64)
def _mirror(grid: PriceGrid) -> PriceGrid:
    """The NO-leg grid: YES band `(a, b, s)` becomes `(ONE - b, ONE - a, s)` (cached: every snapshot asks)."""
    return PriceGrid(tuple((ONE - e, ONE - s, st) for s, e, st in reversed(grid.bands)))


def kalshi_expected_payout(score_seconds: float, program: LipProgram) -> Decimal:
    """Expected LIP payout of a user whose snapshot score integrates to `score_seconds` (score x seconds) over the
    captured part of the period: `reward x score_seconds / (2 x total snapshots)`, before the $1 minimum, the floor
    to the cent and any per-account cap (`kalshi_paid`)."""
    if score_seconds < 0:
        raise QtError("QT-INPUT-002", "LIP score integral is negative")
    return program.period_reward * Decimal(repr(score_seconds)) / Decimal(2 * program.total_snapshots)


def kalshi_paid(expected: Decimal, program: LipProgram) -> Decimal:
    """What Kalshi pays for a whole period's accrual: capped at `max_reward_per_account`, floored to the cent,
    0 under $1.00."""
    x = expected if program.max_reward_per_account is None else min(expected, program.max_reward_per_account)
    x = (x / Decimal("0.01")).to_integral_value(rounding=ROUND_FLOOR) * Decimal("0.01")
    return x if x >= Decimal(1) else Decimal(0)


# ---- Polymarket liquidity rewards -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolyRewardParams:
    """A market's liquidity-reward settings: max spread `v` (cents), min size (size units), daily reward (pUSD),
    in-game multiplier `b`, one-sided factor `c`."""

    max_spread_cents: float
    min_size: int
    daily_reward: Decimal
    multiplier: float = 1.0
    c: float = POLY_C

    def __post_init__(self) -> None:
        if self.max_spread_cents <= 0 or self.min_size < 0 or self.daily_reward < 0 or self.multiplier < 0:
            raise QtError("QT-CONFIG-001", "polymarket reward params: max spread > 0, min size, reward >= 0 required")
        if self.c < 1:
            raise QtError("QT-CONFIG-001", "polymarket reward params: c must be >= 1")


def poly_order_score(v_cents: float, s_cents: float, b: float = 1.0) -> float:
    """`S(v, s) = ((v - s) / v)^2 x b`, 0 at or beyond the max spread."""
    if s_cents < 0:
        raise QtError("QT-INPUT-002", f"spread {s_cents} is negative")
    if s_cents >= v_cents:
        return 0.0
    return ((v_cents - s_cents) / v_cents) ** 2 * b


def poly_q_min(q_one: float, q_two: float, mid: float, c: float = POLY_C) -> float:
    """`Q_min` of one maker for a midpoint `mid` (price units)."""
    if _MID_LO <= mid <= _MID_HI:
        return max(min(q_one, q_two), max(q_one / c, q_two / c))
    return min(q_one, q_two)


def competition_bounds(q1: float, q2: float, mid: float, c: float = POLY_C) -> tuple[float, float, float]:
    """(lower, point, upper) of the summed `Q_min` of the other makers whose aggregated side scores are `q1`, `q2`
    (see the module docstring; point = one maker holding all of it)."""
    point = poly_q_min(q1, q2, mid, c)
    if _MID_LO <= mid <= _MID_HI:
        hi_q, lo_q = max(q1, q2), min(q1, q2)
        lower = max(hi_q / c, (q1 + q2) / (c + 1))
        upper = max(lo_q + (hi_q - lo_q) / c, (q1 + q2) / c)
    else:
        lower, upper = 0.0, min(q1, q2)
    return lower, point, upper


def adjusted_mid(bids: Iterable[tuple[int, int]], asks: Iterable[tuple[int, int]], min_size: int) -> float | None:
    """Midpoint (price units) between the best bid and the best ask among levels whose size is at least `min_size`;
    None when a side has no such level. Inputs best first."""
    bb = next((p for p, s in bids if s >= max(min_size, 1)), None)
    ba = next((p for p, s in asks if s >= max(min_size, 1)), None)
    if bb is None or ba is None:
        return None
    return (bb + ba) / 2.0


@dataclass(frozen=True)
class PolySample:
    """The user's reward state at one instant: own `Q_min`, the others' bounds and the share band."""

    mid: float | None
    q_user: float
    rest_lower: float
    rest_point: float
    rest_upper: float

    def share(self) -> tuple[float, float, float]:
        """(lower, point, upper) share of the sample's normalized score."""
        if self.q_user <= 0:
            return 0.0, 0.0, 0.0
        q = self.q_user
        return q / (q + self.rest_upper), q / (q + self.rest_point), q / (q + self.rest_lower)


def poly_sample(
    bids: Sequence[tuple[int, int]],
    asks: Sequence[tuple[int, int]],
    own_bids: dict[int, list[int]],
    own_asks: dict[int, list[int]],
    params: PolyRewardParams,
) -> PolySample:
    """Reward state of a canonical YES book (`bids` highest first, `asks` lowest first, totals including the
    user's own orders) with the user's own resting orders `own_*` (price -> list of order sizes; each order is
    checked against the min size on its own)."""
    mid = adjusted_mid(bids, asks, params.min_size)
    if mid is None:
        return PolySample(None, 0.0, 0.0, 0.0, 0.0)
    v, b = params.max_spread_cents, params.multiplier

    def side_scores(levels: Sequence[tuple[int, int]], own: dict[int, list[int]]) -> tuple[float, float]:
        user = rest = 0.0
        for px, size in levels:
            s = abs(px - mid) * 100.0 / ONE
            w = poly_order_score(v, s, b)
            if w == 0.0:
                continue
            mine = own.get(px, [])
            mine_total = sum(mine)
            if mine_total > size:
                raise QtError("QT-INPUT-002", f"own resting size {mine_total} exceeds the level size {size} at {px}")
            user += w * sum(q for q in mine if q >= params.min_size)
            rest += w * (size - mine_total)
        return user, rest

    u1, r1 = side_scores(bids, own_bids)
    u2, r2 = side_scores(asks, own_asks)
    q_user = poly_q_min(u1, u2, mid, params.c)
    lo, pt, hi = competition_bounds(r1, r2, mid, params.c)
    return PolySample(mid, q_user, lo, pt, hi)


def poly_expected_reward(share_seconds: float, params: PolyRewardParams, samples_per_day: int = 1440) -> Decimal:
    """Expected reward of a share that integrates to `share_seconds` (share x seconds) over the captured window:
    each minute's sample is the time average of the share over that minute, and the epoch share divides by the
    day's samples with liquidity (`samples_per_day`, 1,440 when every minute had some)."""
    if share_seconds < 0 or samples_per_day <= 0:
        raise QtError("QT-INPUT-002", "reward share integral must be >= 0 and samples per day > 0")
    if not math.isfinite(share_seconds):
        raise QtError("QT-INPUT-002", "reward share integral is not finite")
    return params.daily_reward * Decimal(repr(share_seconds / 60.0)) / Decimal(samples_per_day)


__all__ = [
    "KALSHI_SNAPSHOT_NS",
    "POLY_C",
    "POLY_SAMPLES_PER_DAY",
    "POLY_SAMPLE_NS",
    "SOURCES",
    "LipProgram",
    "LipSide",
    "PolyRewardParams",
    "PolySample",
    "PriceGrid",
    "adjusted_mid",
    "competition_bounds",
    "kalshi_expected_payout",
    "kalshi_paid",
    "lip_side",
    "lip_snapshot",
    "poly_expected_reward",
    "poly_order_score",
    "poly_q_min",
    "poly_sample",
]
