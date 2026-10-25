"""Implementation-shortfall decomposition of the sim-versus-live gap (`qt.score_shortfall`).

Each arm's result on an order is the markout P&L of its fills at horizon `h`, net of fees: for a fill of `q` at
price `P` and time `t`, `v = d (M(t+h) - P) q k - fee`, with `d = +1` buy / `-1` sell, `M` the prevailing venue mid
(the `mid` reference of SPEC 5.7.5, past-end rule `markout_past_end`) and `k = 10^price_exp 10^size_exp multiplier`.
`R_a = sum of v over the arm's fills in the evaluation window W` (the fills of `qt.score_fills`, maker and taker).
The gap of an order is `G = R_sim - R_live`: positive when the simulator overstates what live earned (a cost live
paid relative to the simulation). Unfilled quantity carries no P&L, so a fill one arm got and the other did not is
the other arm's opportunity cost, valued at its markout.

The gap splits into six buckets that sum to it exactly. Units are matched first-in-first-out: the k-th unit of
quantity filled by the live arm (fills in time order) is paired with the k-th unit filled by the sim arm, up to
`m = min(q_live, q_sim)`. With `M-` the mid of the last quote strictly before the fill (`mm_ticks` of SPEC 5.7.5),
the per-unit value difference `d ((M_sim(t+h) - P_sim) - (M_live(t+h) - P_live))` of a matched pair splits into a
mid-at-fill part, a price-against-that-mid part and a mark part (Perold's implementation shortfall, with the sim
fill as the benchmark):

- `delay`: over matched units, `d (M-_live - M-_sim) k`: how far the mid moved between the sim's fill and live's
  (positive when it moved against live, e.g. a late taker buying after the mid rose).
- `spread`: over matched units, `d ((P_live - M-_live) - (P_sim - M-_sim)) k`: the effective half-spread live paid
  minus the one the sim paid (a taker fill against a maker fill at the same mid is the full quoted spread; for two
  passive fills at the same price it is minus `delay`).
- `adverse_selection`: over matched units, `d (M_sim(t+h) - M_live(t+h)) k`: the difference of the marks. For two
  passive fills at the same price this is the whole difference: the mid ran further against the arm marked lower.
- `missed_fill`: orders filled in one arm only, `+R_sim` gross of fees (live missed fills the sim claimed) or
  `-R_live` gross (the sim missed fills live got).
- `partial_fill`: orders filled in both arms with different quantities, the gross value of the units beyond `m` of
  the arm that filled more (`+` sim, `-` live).
- `fees`: `sum live fees - sum sim fees` over every fill (rebates are negative fees); in the scorer the sim arm
  carries the live fee rate per liquidity (`markouts.apply_fee_rates`). A fee in a currency other than the
  instrument's is left out and a null fee counts as 0; both are counted in `note` (null fees only on orders that
  have a known fee on some fill).

Because the matched-unit terms telescope, `delay + spread + adverse_selection + missed_fill + partial_fill + fees =
G` by construction. Every market term is computed in integers of half price units times size units (`2 M = bid +
ask` is integral), so the identity is exact there; the currency columns are those integers times `k / 2`, each
correctly rounded once (`int / int` true division), and agree with `total_ccy` to floating-point rounding.
`total_ccy` is computed independently, from each arm's fills.

An order is valued at a horizon when every fill has a price and a mark at `t + h`, and every fill with a matched
unit also has `M-`; otherwise its row has `valued = false`, null components and the reason in `note`, and it is left
out of the totals. The scorer passes the quantity of the fills it dropped for having no price (maker and taker,
`unpriced_qty`); without it, unpriced maker quantity is inferred from `qt.score_orders` filled quantities and fills
with a null price are detected directly.

Totals per (horizon, currency): `sum` and `mean` over valued orders, and 95% percentile CIs of the per-order mean
by the scorer's clustered block bootstrap (block kind chosen once over all scored orders as in
`statistics.assemble`: session dates with >= 5 dates, else 30-minute local blocks; `bootstrap.b`,
`bootstrap.seed`). Every component of one (horizon, currency) uses the same draws (stream
`score_shortfall:<currency>:<h>`), so the components of each replicate sum to the total of that replicate; the CI
bounds themselves do not add.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, NamedTuple

import numpy as np
import pyarrow as pa

from queuetruth import stats
from queuetruth.schemas.arrow import DICT_VALUES as _ALL_DICT_VALUES
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.score._io import config_error, input_format, make_table, rows
from queuetruth.score.markouts import PAST_END, InstrumentDays, side_sign
from queuetruth.score.statistics import block_labels

NAME = "qt.score_shortfall"
#: Market buckets (integer half-price x size units) in output order, then `fees` (currency).
MARKET = ("delay", "spread", "missed_fill", "adverse_selection", "partial_fill")
COMPONENTS = (*MARKET, "fees")
STATISTICS = ("value", "sum", "mean", "ci_lo", "ci_hi")
#: `qt.score_shortfall` (registered in `queuetruth.schemas.arrow.TABLES`). Scope `order`: one row per scored order
#: and horizon (`statistic = value`); scope `total`: rows `sum`, `mean`, `ci_lo`, `ci_hi` per (horizon, currency).
SCHEMA: pa.Schema = SCHEMAS[NAME]
DICT_VALUES: dict[str, tuple[str, ...]] = _ALL_DICT_VALUES[NAME]
_SUMMED = ("total_ccy", "notional_ccy", "live_notional_ccy", "sim_notional_ccy")
_GROUP_COLS = (*(f"{c}_ccy" for c in COMPONENTS), *_SUMMED)


class _Unvalued(Exception):
    """An order that cannot be valued at a horizon: `reason` (counted in the totals) and the fill time."""

    def __init__(self, reason: str, ts: int) -> None:
        super().__init__(f"{reason} (fill at {ts})")
        self.reason = reason


@dataclass(frozen=True)
class ShortfallFill:
    """One fill of one arm: time, quantity and price in the dataset's units, liquidity, fee in `fee_ccy`."""

    ts: int
    qty: int
    px: int | None
    liquidity: str = "unknown"
    fee: float | None = None
    fee_ccy: str | None = None


@dataclass
class OrderShortfall:
    """Decomposition of one order at one horizon. `units` holds the market buckets and `total` (half price units x
    size units, exact integers); `fees` is in currency (live minus sim)."""

    valued: bool
    live_qty: int = 0
    sim_qty: int = 0
    matched_qty: int = 0
    units: dict[str, int] = field(default_factory=dict)
    fees: float = 0.0
    note: str | None = None
    foreign_fees: int = 0
    #: Fills per arm whose fee is null (counted as 0) on an order with a known fee on some fill.
    missing_fees: dict[str, int] = field(default_factory=dict)
    #: Why the order is not valued (`note` without the fill time); None when valued.
    reason: str | None = None


def _sorted(fills: Sequence[ShortfallFill]) -> list[ShortfallFill]:
    """Fills with positive quantity in time order (ties keep their input order)."""
    return sorted((f for f in fills if f.qty > 0), key=lambda f: f.ts)


def _fees(fills: Sequence[ShortfallFill], currency: str | None) -> tuple[float, int, int]:
    """(sum of fees in the instrument currency, fills with a fee in another currency, fills with a null fee)."""
    keep: list[float] = []
    foreign = missing = 0
    for f in fills:
        if f.fee is None or not math.isfinite(f.fee):
            missing += 1
            continue
        if f.fee_ccy is not None and currency is not None and f.fee_ccy.upper() != currency.upper():
            foreign += 1
            continue
        keep.append(float(f.fee))
    return math.fsum(keep), foreign, missing


def _need(v: int | None, what: str, f: ShortfallFill) -> int:
    if v is None:
        raise _Unvalued(f"no {what}", f.ts)
    return v


@dataclass
class _Plan:
    """The horizon-independent part of an order: fills in time order, fees, the FIFO matched segments `(i, j, q)`
    (live fill `i`, sim fill `j`, `q` units) and the units beyond `m` (`extra`: fill index and units of the arm with
    more, `extra_sign` +1 sim / -1 live)."""

    side: int
    lv: list[ShortfallFill]
    sv: list[ShortfallFill]
    ql: int
    qs: int
    fees: float
    foreign: int
    missing: dict[str, int]
    segs: list[tuple[int, int, int]]
    extra: list[tuple[int, int]]
    extra_sign: int


def _plan(side: int, live: Sequence[ShortfallFill], sim: Sequence[ShortfallFill], currency: str | None) -> _Plan:
    if side not in (1, -1):
        raise input_format("side", f"side sign must be +1 or -1, got {side!r}")
    lv, sv = _sorted(live), _sorted(sim)
    ql, qs = sum(f.qty for f in lv), sum(f.qty for f in sv)
    fl, ol, ml = _fees(lv, currency)
    fs, os_, ms = _fees(sv, currency)
    known = len(lv) + len(sv) - ml - ms
    missing = {a: n for a, n in (("live", ml), ("sim", ms)) if n and known}
    segs: list[tuple[int, int, int]] = []
    extra: list[tuple[int, int]] = []
    sign = 0
    if ql and qs:
        m = min(ql, qs)
        i = j = done = 0
        rl, rs = lv[0].qty, sv[0].qty
        while done < m:
            q = min(rl, rs, m - done)
            segs.append((i, j, q))
            done += q
            rl -= q
            rs -= q
            if rl == 0 and i + 1 < len(lv):
                i += 1
                rl = lv[i].qty
            if rs == 0 and j + 1 < len(sv):
                j += 1
                rs = sv[j].qty
        # units beyond m of the arm that filled more: its current fill's remainder, then its later fills
        if qs > m:
            sign, extra = 1, [(j, rs), *((k, sv[k].qty) for k in range(j + 1, len(sv)))]
        elif ql > m:
            sign, extra = -1, [(i, rl), *((k, lv[k].qty) for k in range(i + 1, len(lv)))]
    return _Plan(side, lv, sv, ql, qs, fl - fs, ol + os_, missing, segs, extra, sign)


def _value(
    p: _Plan,
    pre_l: Sequence[int | None],
    pre_s: Sequence[int | None],
    post_l: Sequence[int | None],
    post_s: Sequence[int | None],
) -> OrderShortfall:
    """The decomposition of a planned order at one horizon from `2 M(t-)` and `2 M(t + h)` of each fill (aligned
    with `p.lv` / `p.sv`; None when undefined)."""
    out = OrderShortfall(
        valued=True,
        live_qty=p.ql,
        sim_qty=p.qs,
        matched_qty=min(p.ql, p.qs),
        fees=p.fees,
        foreign_fees=p.foreign,
        missing_fees=p.missing,
    )
    d, lv, sv = p.side, p.lv, p.sv
    try:
        for f in (*lv, *sv):
            if f.px is None:
                raise _Unvalued("fill without price", f.ts)
        for fs_, marks in ((lv, post_l), (sv, post_s)):
            for f, v in zip(fs_, marks, strict=True):
                _need(v, "mark at fill + horizon", f)
        val_l = [d * (v - 2 * f.px) for f, v in zip(lv, post_l, strict=True)]  # type: ignore[operator]
        val_s = [d * (v - 2 * f.px) for f, v in zip(sv, post_s, strict=True)]  # type: ignore[operator]
        total = sum(v * f.qty for v, f in zip(val_s, sv, strict=True)) - sum(
            v * f.qty for v, f in zip(val_l, lv, strict=True)
        )
        u = dict.fromkeys(MARKET, 0)
        if not p.ql or not p.qs:
            u["missed_fill"] = total
        else:
            delay = spread = adverse = 0
            for i, j, q in p.segs:
                a = _need(pre_l[i], "quote before the fill", lv[i])
                b = _need(pre_s[j], "quote before the fill", sv[j])
                delay += q * (a - b)
                spread += q * ((2 * lv[i].px - a) - (2 * sv[j].px - b))  # type: ignore[operator]
                adverse += q * (post_s[j] - post_l[i])  # type: ignore[operator]
            u["delay"], u["spread"], u["adverse_selection"] = d * delay, d * spread, d * adverse
            vals = val_s if p.extra_sign > 0 else val_l
            u["partial_fill"] = p.extra_sign * sum(vals[k] * q for k, q in p.extra)
        if sum(u.values()) != total:  # pragma: no cover - guarded by construction; kept as a hard check
            raise AssertionError(f"shortfall identity broken: {u} vs {total}")
        u["total"] = total
        out.units = u
    except _Unvalued as e:
        out.valued = False
        out.note = str(e)
        out.reason = e.reason
    return out


def decompose_order(
    side: int,
    live: Sequence[ShortfallFill],
    sim: Sequence[ShortfallFill],
    pre: Callable[[int], int | None],
    post: Callable[[int], int | None],
    currency: str | None = None,
) -> OrderShortfall:
    """Decomposition of one order (module docstring). `side` is +1 buy / -1 sell; `pre(t)` returns `2 M(t-)` (bid +
    ask of the last quote strictly before `t`) and `post(t)` returns `2 M(t + h)`, both None when undefined. Fees
    count when their currency is null or equals `currency` (case-insensitive)."""
    p = _plan(side, live, sim, currency)
    return _value(
        p, [pre(f.ts) for f in p.lv], [pre(f.ts) for f in p.sv], [post(f.ts) for f in p.lv], [post(f.ts) for f in p.sv]
    )


def _int_column(t: pa.Table, name: str, n: int, fill: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """(int64 values with nulls as `fill`, validity mask) of a column (all-null when absent); timestamps as ns."""
    if name not in t.column_names:
        return np.full(n, fill, dtype=np.int64), np.zeros(n, dtype=bool)
    col = t.column(name).combine_chunks()
    if pa.types.is_dictionary(col.type):
        col = col.cast(col.type.value_type)
    if pa.types.is_timestamp(col.type):
        col = col.cast(pa.timestamp("ns", tz=col.type.tz)).cast(pa.int64())
    valid = np.asarray(col.is_valid().to_numpy(zero_copy_only=False), dtype=bool)
    vals = np.asarray(col.cast(pa.int64()).fill_null(fill).to_numpy(zero_copy_only=False), dtype=np.int64)
    return vals, valid


@dataclass(frozen=True)
class Book:
    """Quotes of one instrument-day in (`ts_event`, file) order: times, `2 M = bid + ask` (exact: int64, or Python
    ints when a price is beyond 2^62) and whether both sides are present."""

    ts: np.ndarray
    mid2: np.ndarray
    ok: np.ndarray


def _midnights(instruments: pa.Table) -> dict[int, tuple[np.ndarray, list[int]]]:
    """Per instrument: midnights in ascending order (stable) and the session date of each, as `InstrumentDays`."""
    by: dict[int, list[tuple[int, int]]] = {}
    for r in rows(instruments):
        by.setdefault(int(r["instrument_id"]), []).append(
            (int(r.get("midnight_utc_ns") or 0), int(r.get("session_date") or 0))
        )
    out = {}
    for i, v in by.items():
        v.sort(key=lambda x: x[0])
        out[i] = (np.array([m for m, _ in v], dtype=np.int64), [d for _, d in v])
    return out


def _day_index(mids: np.ndarray, ts: np.ndarray) -> np.ndarray:
    """Instrument-day of each time: latest midnight <= ts, else the first (`InstrumentDays.lookup`)."""
    return np.maximum(np.searchsorted(mids, ts, side="right") - 1, 0)


def quote_groups(bbo: pa.Table, instruments: pa.Table, default_id: int | None) -> dict[tuple[int, int], Book]:
    """The quote groups of `markouts.compute_markouts`, vectorized: `qt.bbo` rows per (instrument_id, session_date)
    of the row's instrument-day, sorted by (`ts_event`, file order); rows of instruments without `qt.instruments`
    rows are dropped; a missing `instrument_id` column needs exactly one instrument (`QT-INPUT-002` otherwise)."""
    n = bbo.num_rows
    ts, _ = _int_column(bbo, "ts_event", n)
    if "instrument_id" in bbo.column_names:
        iid, iid_ok = _int_column(bbo, "instrument_id", n)
        if not iid_ok.all():
            raise input_format("bbo", "null instrument_id")
    elif default_id is None:
        raise input_format("bbo", "instrument_id column required with several instruments")
    else:
        iid = np.full(n, int(default_id), dtype=np.int64)
    bid, bid_ok = _int_column(bbo, "bid_px", n)
    ask, ask_ok = _int_column(bbo, "ask_px", n)
    if n and max(int(np.abs(bid).max()), int(np.abs(ask).max())) >= 2**62:  # bid + ask could overflow int64
        mid2_all = bid.astype(object) + ask.astype(object)
    else:
        mid2_all = bid + ask
    days = _midnights(instruments)
    out: dict[tuple[int, int], Book] = {}
    for i in np.unique(iid).tolist():
        if int(i) not in days:
            continue
        mids, dates = days[int(i)]
        rows_i = np.flatnonzero(iid == i)
        day_idx = _day_index(mids, ts[rows_i])
        for d in np.unique(day_idx).tolist():
            sel = rows_i[day_idx == d]
            sel = sel[np.lexsort((sel, ts[sel]))]
            out[(int(i), int(dates[d]))] = Book(ts=ts[sel], mid2=mid2_all[sel], ok=bid_ok[sel] & ask_ok[sel])
    return out


class Marks:
    """Venue-mid lookups shared with `compute_markouts`: the instrument-day of a time, `2 M(t-)` and `2 M(t + h)`
    (the prevailing quote at `t + h`, null past the instrument-day's last event unless `past_end = "last_quote"`).
    `pre_many` / `post_many` answer many fills at once (`locate` gives each fill's quote group)."""

    def __init__(
        self,
        bbo: pa.Table,
        instruments: pa.Table,
        *,
        last_event_ts: Mapping[tuple[int, int], int] | None = None,
        past_end: str | None = None,
    ) -> None:
        if past_end not in PAST_END:
            raise config_error(f"markout_past_end: unknown value {past_end!r} (null or 'last_quote')")
        self.days = InstrumentDays(instruments)
        self.default_id = self.days.only_id()
        self._mid = _midnights(instruments)
        groups = quote_groups(bbo, instruments, self.default_id)
        self.keys = list(groups)
        self.books = [groups[k] for k in self.keys]
        self._index = {k: n for n, k in enumerate(self.keys)}
        last = dict(last_event_ts or {})
        self.ends = [last.get(k, int(b.ts[-1])) for k, b in zip(self.keys, self.books, strict=True)]
        self.past_end = past_end

    def inst(self, iid: int | None, ts: int) -> Any:
        """Instrument-day row of `ts` (`markouts.InstrumentDays.lookup`), None when unknown."""
        i = self.default_id if iid is None else int(iid)
        return None if i is None else self.days.lookup(i, ts)

    def locate(self, iids: Sequence[int | None], ts: np.ndarray) -> np.ndarray:
        """Quote group of each (instrument, time): an index into `books`, -1 when there is none."""
        ids = np.array([-1 if i is None else int(i) for i in iids], dtype=np.int64)
        if self.default_id is not None:
            ids[ids == -1] = self.default_id
        out = np.full(len(ts), -1, dtype=np.int64)
        for i in np.unique(ids).tolist():
            if i not in self._mid:
                continue
            mids, dates = self._mid[i]
            sel = np.flatnonzero(ids == i)
            day = _day_index(mids, ts[sel])
            for d in np.unique(day).tolist():
                b = self._index.get((i, dates[d]), -1)
                out[sel[day == d]] = b
        return out

    def _gather(self, book: np.ndarray, ts: np.ndarray, at: Callable[[int, np.ndarray], np.ndarray]) -> list:
        """`2 M` at quote index `at(b, ts)` of each fill's group (None for -1, an empty side or no group)."""
        out: list[int | None] = [None] * len(ts)
        if not len(ts):
            return out
        order = np.argsort(book, kind="stable")
        bounds = np.flatnonzero(np.diff(book[order])) + 1
        for sel in np.split(order, bounds):
            b = int(book[sel[0]])
            if b < 0:
                continue
            q = self.books[b]
            idx = at(b, ts[sel])
            good = idx >= 0
            safe = np.where(good, idx, 0)
            good &= q.ok[safe]
            for n, v, g in zip(sel.tolist(), q.mid2[safe].tolist(), good.tolist(), strict=True):
                if g:
                    out[n] = int(v)
        return out

    def pre_many(self, book: np.ndarray, ts: np.ndarray) -> list[int | None]:
        """`2 M(t-)` of each fill: the last quote strictly before `t` (no end-of-data check)."""
        return self._gather(book, ts, lambda b, t: np.searchsorted(self.books[b].ts, t, side="left") - 1)

    def post_many(self, book: np.ndarray, ts: np.ndarray, h: int) -> list[int | None]:
        """`2 M(t + h)` of each fill by the markout rule."""

        def at(b: int, t: np.ndarray) -> np.ndarray:
            q, end = self.books[b], self.ends[b]
            th = t + int(h)
            idx = np.searchsorted(q.ts, th, side="right") - 1
            if self.past_end == "last_quote":
                return np.where(th <= end, idx, np.searchsorted(q.ts, end, side="right") - 1)
            return np.where(th <= end, idx, -1)

        return self._gather(book, ts, at)

    def pre(self, iid: int | None, t: int) -> int | None:
        """`2 M(t-)` of one time (`pre_many`)."""
        ts = np.array([t], dtype=np.int64)
        return self.pre_many(self.locate([iid], ts), ts)[0]

    def post(self, iid: int | None, t: int, h: int) -> int | None:
        """`2 M(t + h)` of one fill time (`post_many`)."""
        ts = np.array([t], dtype=np.int64)
        return self.post_many(self.locate([iid], ts), ts, h)[0]


def unit_value(inst: Any) -> tuple[int, int]:
    """Currency value of one half price unit times one size unit, `10^price_exp 10^size_exp multiplier / 2`, as an
    exact (numerator, denominator) of integers (the float multiplier is taken at its exact binary value)."""
    v = Fraction(10) ** inst.price_exp * Fraction(10) ** inst.size_exp * Fraction(inst.mult) / 2
    return v.numerator, v.denominator


def _ccy(units: int, scale: tuple[int, int]) -> float:
    """`units x scale` correctly rounded to a float (Python's int / int true division is correctly rounded)."""
    return (units * scale[0]) / scale[1]


def _fill(f: Mapping[str, Any]) -> ShortfallFill:
    return ShortfallFill(
        ts=int(f["ts"]),
        qty=int(f["qty"]),
        px=None if f.get("px") is None else int(f["px"]),
        liquidity=str(f.get("liquidity") or "unknown"),
        fee=None if f.get("fee") is None else float(f["fee"]),
        fee_ccy=None if f.get("fee_ccy") is None else str(f["fee_ccy"]),
    )


def _fill_order(f: Mapping[str, Any]) -> tuple[int, int]:
    return int(f["ts"]), int(f.get("fill_seq") or 0)


def _rows(x: Any) -> list[dict[str, Any]]:
    return rows(x) if isinstance(x, pa.Table) else [dict(r) for r in x]


def _bootstrap_args(bootstrap: Any) -> tuple[int, str, int]:
    if bootstrap is None:
        return 1000, "auto", 0
    get = bootstrap.get if isinstance(bootstrap, Mapping) else lambda k, d=None: getattr(bootstrap, k, d)
    return int(get("b", 1000)), str(get("block", "auto")), int(get("seed", 0))


def _notional(fills: list[ShortfallFill], scale: tuple[int, int] | None) -> float | None:
    """Filled notional `sum |P| q k` of one arm (None without a scale or with an unpriced fill)."""
    if scale is None or any(f.px is None for f in fills):
        return None
    return _ccy(2 * sum(abs(int(f.px)) * f.qty for f in fills), scale)  # type: ignore[arg-type]


class _Planned(NamedTuple):
    """A planned order and the offset of its fills (live, then sim) in the batched mark lookups."""

    plan: _Plan
    offset: int


@dataclass
class _Group:
    """Accumulator of one (horizon, currency): valued orders' columns and unvalued / fee counts."""

    orders: list[dict[str, Any]] = field(default_factory=list)
    cols: dict[str, list[float | None]] = field(default_factory=dict)
    qty: dict[str, int] = field(default_factory=lambda: dict.fromkeys(("qty", "live_qty", "sim_qty", "matched_qty"), 0))
    unvalued: dict[str, int] = field(default_factory=dict)
    missing_fees: dict[str, int] = field(default_factory=dict)
    foreign_fees: int = 0


def shortfall_columns(
    orders: Any,
    fills: Any,
    bbo: pa.Table | None,
    instruments: pa.Table | None,
    horizons_ns: Sequence[int],
    *,
    last_event_ts: Mapping[tuple[int, int], int] | None = None,
    past_end: str | None = None,
    bootstrap: Any = None,
    unpriced_qty: Mapping[tuple[str, str], int] | None = None,
    n_unmatched_live: int = 0,
) -> dict[str, list[Any]]:
    """`qt.score_shortfall` as columns (total rows first, by horizon, currency and statistic; then order rows by
    horizon and `order_key`). `orders`: `qt.score_orders` rows or table (optionally with `local_ns`); `fills`:
    `qt.score_fills` 1.1 rows or table (fees as charged); `bbo` / `instruments`: the dataset's quotes and
    `qt.instruments` (None: every order unvalued); `bootstrap`: a `BootstrapSpec` or mapping (`b`, `block`, `seed`);
    `unpriced_qty`: quantity of fills without a price per (`order_key`, arm), as the scorer dropped them from
    `fills`; `n_unmatched_live`: live orders the scorer could not pair with a sim outcome (reported in the notes)."""
    order_rows = _rows(orders)
    fill_rows = _rows(fills)
    hs = sorted({int(h) for h in horizons_ns})
    if any(h < 0 for h in hs):
        raise config_error("score_shortfall: horizons must be >= 0 ns")
    b, block, seed = _bootstrap_args(bootstrap)
    kind = block_labels(order_rows, block)[1] if order_rows else "day"
    marks = (
        None
        if bbo is None or instruments is None
        else Marks(bbo, instruments, last_event_ts=last_event_ts, past_end=past_end)
    )
    by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in fill_rows:
        by.setdefault((str(f["order_key"]), str(f["arm"])), []).append(f)
    keys = {str(o["order_key"]) for o in order_rows}
    outside = [f for f in fill_rows if str(f["order_key"]) not in keys]
    scope_notes = []
    if outside:
        n_o = len({str(f["order_key"]) for f in outside})
        arms = ", ".join(sorted({str(f["arm"]) for f in outside}))
        scope_notes.append(f"not decomposed: {len(outside)} {arms} fill(s) of {n_o} order(s) outside the scored orders")
    if n_unmatched_live:
        scope_notes.append(f"not decomposed: {n_unmatched_live} live order(s) without a sim outcome")
    out: dict[str, list[Any]] = {f.name: [] for f in SCHEMA}  # order rows, column by column (no per-row dicts)
    groups: dict[tuple[int, str | None], _Group] = {}
    # pass 1, once per order: instrument-day, scale, notionals, unpriced arms and the horizon-independent plan
    prepared: list[tuple[dict[str, Any], dict[str, Any], tuple[int, int] | None, OrderShortfall | _Planned]] = []
    f_iid: list[int | None] = []
    f_ts: list[int] = []
    for o in sorted(order_rows, key=lambda r: str(r["order_key"])):
        key = str(o["order_key"])
        iid = None if o.get("instrument_id") is None else int(o["instrument_id"])
        d = side_sign(o["side"])
        inst = None if marks is None else marks.inst(iid, int(o["ts_entry"]))
        scale = None if inst is None else unit_value(inst)
        lim = o.get("limit_px")
        notional = None
        if scale is not None and o.get("qty") is not None and lim is not None and int(lim) != 0:
            notional = _ccy(2 * int(o["qty"]) * abs(int(lim)), scale)
        fl = {a: [_fill(f) for f in sorted(by.get((key, a), []), key=_fill_order)] for a in ("live", "sim")}
        if unpriced_qty is not None:
            unpriced = [a for a in ("live", "sim") if int(unpriced_qty.get((key, a), 0) or 0) > 0]
        else:
            priced_maker = {
                a: sum(f.qty for f in v if f.liquidity != "taker" and f.px is not None) for a, v in fl.items()
            }
            unpriced = [
                a
                for a, col in (("live", "live_filled_qty"), ("sim", "sim_filled_qty"))
                if o.get(col) is not None and int(o[col]) > priced_maker[a]
            ]
        base = {
            "scope": "order",
            "statistic": "value",
            "order_key": key,
            "instrument_id": iid,
            "session_date": None if o.get("session_date") is None else int(o["session_date"]),
            "side": o.get("side"),
            "class": o.get("class"),
            "currency": None if inst is None else inst.currency,
            "qty": None if o.get("qty") is None else int(o["qty"]),
            "notional_ccy": notional,
            "live_notional_ccy": _notional(fl["live"], scale),
            "sim_notional_ccy": _notional(fl["sim"], scale),
        }
        work: OrderShortfall | _Planned
        if marks is None or scale is None or unpriced:
            if marks is None or scale is None:
                why = note = "no dataset quotes" if marks is None else "instrument not in the dataset"
            else:
                why, note = "fill without price", f"fill without price ({', '.join(unpriced)} arm)"
            work = OrderShortfall(
                valued=False,
                live_qty=sum(f.qty for f in fl["live"]),
                sim_qty=sum(f.qty for f in fl["sim"]),
                note=note,
                reason=why,
            )
        else:
            plan = _plan(d, fl["live"], fl["sim"], base["currency"])
            work = _Planned(plan, len(f_ts))
            f_ts += [f.ts for f in (*plan.lv, *plan.sv)]
            f_iid += [iid] * (len(plan.lv) + len(plan.sv))
        prepared.append((o, base, scale, work))
    # every fill's quote group, M(t-) once and M(t + h) per horizon, vectorized
    ts_arr = np.asarray(f_ts, dtype=np.int64)
    book = marks.locate(f_iid, ts_arr) if marks is not None else np.zeros(0, dtype=np.int64)
    pre_all = marks.pre_many(book, ts_arr) if marks is not None else []
    for h in hs:
        post_all = marks.post_many(book, ts_arr, h) if marks is not None else []
        for o, base, scale, work in prepared:
            if isinstance(work, OrderShortfall):
                res = work
            else:
                p, off = work
                a, z = off + len(p.lv), off + len(p.lv) + len(p.sv)
                res = _value(p, pre_all[off:a], pre_all[a:z], post_all[off:a], post_all[a:z])
            notes = [] if res.note is None else [res.note]
            if res.foreign_fees:
                notes.append(f"{res.foreign_fees} fee(s) in another currency left out")
            for arm, n_m in sorted(res.missing_fees.items()):
                notes.append(f"{n_m} {arm} fill(s) without a fee counted as 0")
            row = dict(
                base,
                horizon_ns=h,
                live_qty=res.live_qty,
                sim_qty=res.sim_qty,
                matched_qty=res.matched_qty,
                valued=res.valued,
                n=1 if res.valued else 0,
                note="; ".join(notes) or None,
            )
            g = groups.setdefault((h, base["currency"]), _Group())
            if res.valued and scale is not None:
                for c in MARKET:
                    row[f"{c}_ccy"] = _ccy(res.units[c], scale)
                row["fees_ccy"] = res.fees
                row["total_ccy"] = _ccy(res.units["total"], scale) + res.fees
                g.orders.append(o)
                for c in _GROUP_COLS:
                    g.cols.setdefault(c, []).append(row[c])
                for c in g.qty:
                    g.qty[c] += int(row[c] or 0)
                g.foreign_fees += res.foreign_fees
                for arm, n_m in res.missing_fees.items():
                    g.missing_fees[arm] = g.missing_fees.get(arm, 0) + n_m
            else:
                for c in (*COMPONENTS, "total"):
                    row[f"{c}_ccy"] = None
                why = res.reason or "unvalued"
                g.unvalued[why] = g.unvalued.get(why, 0) + 1
            for name, col in out.items():  # horizons ascending, orders by key: already in output order
                col.append(row.get(name))
    totals: list[dict[str, Any]] = []
    for h, cur in sorted(groups, key=lambda k: (k[0], k[1] or "")):
        totals += _total_rows(h, cur, groups[(h, cur)], b, kind, seed, scope_notes)
    return {name: [t.get(name) for t in totals] + col for name, col in out.items()}


def shortfall_rows(
    orders: Any,
    fills: Any,
    bbo: pa.Table | None,
    instruments: pa.Table | None,
    horizons_ns: Sequence[int],
    **kw: Any,
) -> list[dict[str, Any]]:
    """`qt.score_shortfall` rows (`shortfall_columns`, same arguments)."""
    cols = shortfall_columns(orders, fills, bbo, instruments, horizons_ns, **kw)
    return [dict(zip(cols, vals, strict=True)) for vals in zip(*cols.values(), strict=True)]


def _total_rows(
    h: int, cur: str | None, g: _Group, b: int, kind: str, seed: int, scope_notes: list[str]
) -> list[dict[str, Any]]:
    cols = list(_GROUP_COLS)
    n = len(g.orders)
    base: dict[str, Any] = {"scope": "total", "horizon_ns": int(h), "currency": cur, "valued": n > 0, "n": n}
    n_un = sum(g.unvalued.values())
    notes = (
        [f"{n_un} of {n + n_un} order(s) unvalued ({'; '.join(f'{k}: {v}' for k, v in sorted(g.unvalued.items()))})"]
        if n_un
        else []
    )
    if g.foreign_fees:
        notes.append(f"{g.foreign_fees} fee(s) in another currency left out")
    if g.missing_fees:
        notes.append(
            "fills without a fee counted as 0: " + ", ".join(f"{a} {v}" for a, v in sorted(g.missing_fees.items()))
        )
    notes += scope_notes
    sums: dict[str, float | None] = {}
    means: dict[str, float | None] = {}
    lo: dict[str, float | None] = dict.fromkeys(cols)
    hi: dict[str, float | None] = dict.fromkeys(cols)
    for c in cols:
        vals = g.cols.get(c, [])
        if not n or any(v is None for v in vals):
            sums[c] = means[c] = None
            continue
        sums[c] = math.fsum(vals)  # type: ignore[arg-type]
        means[c] = sums[c] / n  # type: ignore[operator]
    if n:
        labels, kind_ = block_labels(g.orders, kind)
        _, inv = np.unique(labels, return_inverse=True)
        k = int(inv.max()) + 1
        test_id = f"score_shortfall:{cur or '-'}:{h}"
        counts = stats.block_counts(k, b, stats.bootstrap_seed(seed, test_id)).astype(float)
        den = counts @ np.bincount(inv, minlength=k).astype(float)
        with np.errstate(all="ignore"):
            for c in cols:
                if means[c] is None:
                    continue
                x = np.asarray(g.cols[c], dtype=float)
                reps = (counts @ np.bincount(inv, weights=x, minlength=k)) / den
                a, z = stats.percentile_ci(reps, 0.05)
                lo[c] = a if math.isfinite(a) else None
                hi[c] = z if math.isfinite(z) else None
        notes.append(
            f"95% percentile CI of the per-order mean; block bootstrap over {k} {kind_} block(s), B = {b}, "
            f"stream {test_id}"
        )
    note = "; ".join(notes) or None
    out = []
    for stat, vals, q in (("sum", sums, g.qty), ("mean", means, None), ("ci_lo", lo, None), ("ci_hi", hi, None)):
        r = dict(base, statistic=stat, note=note, **{c: vals[c] for c in cols})
        if q is not None:
            r.update(q)
        out.append(r)
    return out


def shortfall_table(
    orders: Any,
    fills: Any,
    bbo: pa.Table | None,
    instruments: pa.Table | None,
    horizons_ns: Sequence[int],
    *,
    last_event_ts: Mapping[tuple[int, int], int] | None = None,
    past_end: str | None = None,
    bootstrap: Any = None,
    unpriced_qty: Mapping[tuple[str, str], int] | None = None,
    n_unmatched_live: int = 0,
) -> pa.Table:
    """`qt.score_shortfall` as an Arrow table (`shortfall_columns`)."""
    return make_table(
        NAME,
        shortfall_columns(
            orders,
            fills,
            bbo,
            instruments,
            horizons_ns,
            last_event_ts=last_event_ts,
            past_end=past_end,
            bootstrap=bootstrap,
            unpriced_qty=unpriced_qty,
            n_unmatched_live=n_unmatched_live,
        ),
    )


def score_shortfall(
    order_rows: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    bbo: Any,
    ds: Any,
    spec: Any,
    *,
    unpriced_qty: Mapping[tuple[str, str], int] | None = None,
    n_unmatched_live: int = 0,
) -> pa.Table:
    """The scorer's `qt.score_shortfall` (`build_scorecard` hook): its per-order rows and score fills (fees as
    charged), the dataset's BBO, instruments and partition end times, `markout_horizons_ns`, `markout_past_end` and
    `bootstrap` of the spec, the quantity of the fills it dropped for having no price and its unmatched live
    orders."""
    return shortfall_table(
        order_rows,
        fills,
        bbo,
        None if ds is None else ds.instruments,
        list(spec.markout_horizons_ns),
        last_event_ts=None if ds is None else ds.ts_max,
        past_end=spec.markout_past_end,
        bootstrap=spec.bootstrap,
        unpriced_qty=unpriced_qty,
        n_unmatched_live=n_unmatched_live,
    )
