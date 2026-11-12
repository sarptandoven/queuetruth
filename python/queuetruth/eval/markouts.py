"""Markouts: the one implementation (SPEC 5.7.5, oracle O-SCORE-3). Every caller that reports a markout computes it
here: the scorer (`queuetruth.score.compute_markouts`), the toxicity evaluation (`queuetruth.eval.toxicity`), the
prediction-market score (`queuetruth.pm.score`) and the sandbox (its in-session markouts and its scorecard).

A markout compares a fill with a reference price some time after it. For a fill at time `t`, price `P`, side `d`
(`+1` buy, `-1` sell) and a horizon `h`:

- `mo = d (M(t+h) - P)`: what the fill is worth at the later price; positive = good for the side that filled.
- `mm = d (M(t+h) - M(t-))`: how the reference moved across the fill.

`M(x)` reads a `PricePath`: the last row with `ts <= x` (equal timestamps: the last in path order), `M(t-)` the last
row strictly before `t`. A row may be undefined (`ok` false: an empty side, a crossed book where the caller says
so), and so is any time before the first row. The path's `end` censors the horizon: `M(t+h)` is undefined when
`t + h > end` (equality is valid) unless `past_end = "last_quote"`, which reads `M(end)` instead. `M(t-)` has no end
check. A path without `end` never censors (the sandbox: it only asks for a horizon once it is visible).

The reference at a row (`ref`):
- `mid`: `(bid + ask) / 2`;
- `microprice`: `(Qb Pa + Qa Pb) / (Qb + Qa)`, undefined when both sizes are 0;
- a last-print path (`PricePath.prints`): the print price.

Prices are integers. Every value is differenced against `P` as integers before any float operation
(`M - P = (bid - P) + (ask - bid) w` with `w = 1/2` or the size weight), then `mo = d (M(t+h) - P)` and
`mm = d ((M(t+h) - P) - (M(t-) - P))`. Below 2^51 price units these are exact, so the result does not depend on how
the difference is grouped (the toxicity evaluation and the prediction-market score used to group it differently;
their numbers are the same bits).

The sign convention (`sign`): `"fill"` is the one above. `"adverse"` flips it (positive = the price moved against
the filled side), which is what the prediction-market score reports as adverse selection.

Units are the caller's choice and stay explicit (`ticks`, `bps`, `ccy` below): the scorer and the sandbox report
ticks, bps and quote currency; the toxicity evaluation ticks and bps, with `mm` in bps over `M(t-)`; the
prediction-market score sums `adverse x qty` in its own price units and turns the sum into dollars itself
(`queuetruth.pm.units.ONE`).

Which caller uses which settings:

- `score.compute_markouts`: `qt.bbo` per instrument-day; mid and microprice; `end` = the instrument-day's last event,
  `past_end` null or `"last_quote"` (`markout_past_end`); sign `fill`; ticks, quote currency, bps, net of fees.
- `score.shortfall`: the same paths and end rule (`PricePath.mark_index`), with its marks kept as exact integers
  (`2 M = bid + ask`) for its exact decomposition.
- `eval.toxicity.mid_markouts`: `qt.bbo` per instrument-day; mid; `end` = the last event, censored; sign `fill`;
  ticks and bps, `mm` also in bps over `M(t-)`.
- `pm.score`: the published book's mid after each batch (a crossed or one-sided book is undefined); `end` = the
  last batch, censored; `mm` with sign `adverse`; price units times quantity.
- the sandbox session: the visible best-quote history; mid; no `end`; sign `fill`; ticks, bps, quote currency.
- the sandbox scorecard: through `score.compute_markouts`, `end` = the partition's last event.
"""

from __future__ import annotations

import bisect
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

REFS = ("mid", "microprice")
PAST_END = (None, "last_quote")
SIGNS = ("fill", "adverse")

Ref = Literal["mid", "microprice"]
Sign = Literal["fill", "adverse"]


class PricePath:
    """A reference price over time (one instrument-day). Columns are numpy arrays or plain lists (the sandbox's
    growing history is read in place, without a copy); `ts` must be non-decreasing.

    `bid` / `ask` are integer prices; with lists, `None` marks an empty side. `ok` (optional) says where a row is
    defined; by default a row is defined when both sides are present. `bid_sz` / `ask_sz` are needed only for the
    microprice. `end` is the censoring time (module docstring)."""

    __slots__ = ("ask", "ask_sz", "bid", "bid_sz", "end", "ok", "ts")

    def __init__(
        self,
        ts: Sequence[int] | np.ndarray,
        bid: Sequence[int | None] | np.ndarray,
        ask: Sequence[int | None] | np.ndarray,
        *,
        ok: Sequence[bool] | np.ndarray | None = None,
        bid_sz: Sequence[float] | np.ndarray | None = None,
        ask_sz: Sequence[float] | np.ndarray | None = None,
        end: int | None = None,
    ) -> None:
        self.ts = ts
        self.bid = bid
        self.ask = ask
        self.ok = ok
        self.bid_sz = bid_sz
        self.ask_sz = ask_sz
        self.end = None if end is None else int(end)

    @classmethod
    def prints(
        cls, ts: Sequence[int] | np.ndarray, px: Sequence[int | None] | np.ndarray, *, end: int | None = None
    ) -> PricePath:
        """A last-print path: the reference at a row is its print price."""
        return cls(ts, px, px, end=end)

    def __len__(self) -> int:
        return len(self.ts)

    def index(self, at: np.ndarray, *, strict: bool) -> np.ndarray:
        """Row of the last `ts <= at` (`strict`: `ts < at`) for each time, -1 when there is none."""
        side = "left" if strict else "right"
        if isinstance(self.ts, np.ndarray):
            return np.searchsorted(self.ts, at, side=side).astype(np.int64) - 1
        find = bisect.bisect_left if strict else bisect.bisect_right
        return np.fromiter((find(self.ts, int(x)) - 1 for x in at), dtype=np.int64, count=len(at))

    def mark_index(self, at: np.ndarray, past_end: str | None = None) -> np.ndarray:
        """Row of `M(at)` by the markout rule: the last `ts <= at`; past `end`, -1 (`past_end` None) or the row of
        `M(end)` (`"last_quote"`); -1 when there is none."""
        idx = self.index(at, strict=False)
        if self.end is None:
            return idx
        past = at > self.end
        if past_end == "last_quote":
            last = int(self.index(np.array([self.end], dtype=np.int64), strict=False)[0])
            return np.where(past, last, idx)
        return np.where(past, -1, idx)

    def rows(self, idx: np.ndarray, ref: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(bid, ask, weight of the ask - bid gap, defined) at rows `idx` (-1 = undefined) of a non-empty path."""
        n = idx.size
        has = idx >= 0
        j = np.where(has, idx, 0)
        if isinstance(self.bid, np.ndarray):
            bid = self.bid[j].astype(np.int64)
            ask = np.asarray(self.ask)[j].astype(np.int64)
            ok = np.asarray(self.ok, dtype=bool)[j] if self.ok is not None else np.ones(n, dtype=bool)
        else:
            bl = [self.bid[k] if h else None for k, h in zip(j.tolist(), has.tolist(), strict=True)]
            al = [self.ask[k] if h else None for k, h in zip(j.tolist(), has.tolist(), strict=True)]
            bid = np.array([0 if b is None else int(b) for b in bl], dtype=np.int64)
            ask = np.array([0 if a is None else int(a) for a in al], dtype=np.int64)
            ok = np.array([b is not None and a is not None for b, a in zip(bl, al, strict=True)], dtype=bool)
            if self.ok is not None:
                ok &= np.array([bool(self.ok[k]) for k in j.tolist()], dtype=bool)
        ok &= has
        if ref == "microprice":
            if self.bid_sz is None or self.ask_sz is None:
                raise ValueError("microprice: the path has no sizes")
            bsz = np.asarray(self.bid_sz, dtype=np.float64)[j]
            asz = np.asarray(self.ask_sz, dtype=np.float64)[j]
            tot = bsz + asz
            ok &= tot > 0
            w = np.divide(bsz, tot, out=np.zeros(n), where=tot > 0)
        else:
            w = np.full(n, 0.5)
        return bid, ask, w, ok


@dataclass(frozen=True)
class Markouts:
    """Markouts of `n` fills at `H` horizons, in price units, NaN where undefined (module docstring).

    `mo` and `mm` are `(n, H)`; `pre` is `M(t-)` itself, `(n,)`, for the callers that scale `mm` by it."""

    mo: np.ndarray
    mm: np.ndarray
    pre: np.ndarray


def markouts(
    path: PricePath | Mapping[Hashable, PricePath] | None,
    ts: Sequence[int] | np.ndarray,
    side: Sequence[int] | np.ndarray,
    px: Sequence[int] | np.ndarray,
    horizons_ns: Sequence[int],
    *,
    keys: Sequence[Hashable] | None = None,
    ref: Ref = "mid",
    past_end: Literal["last_quote"] | None = None,
    px_valid: Sequence[bool] | np.ndarray | None = None,
    sign: Sign = "fill",
) -> Markouts:
    """Markouts of fills (time `ts`, side `+1` buy / `-1` sell, integer price `px`) against `path` at each horizon
    (module docstring).

    `path` is one `PricePath` for every fill, or a mapping from a key (the scorer's (instrument_id, session_date)) to
    a path, with each fill's key in `keys`; a fill whose key has no path has no markout. `px_valid` marks fills with
    a usable price (default all); `mo` is undefined without one, `mm` does not need it. `sign`: `"fill"` or
    `"adverse"`."""
    if ref not in REFS:
        raise ValueError(f"unknown markout reference {ref!r}")
    if past_end not in PAST_END:
        raise ValueError(f"unknown past_end {past_end!r} (None or 'last_quote')")
    if sign not in SIGNS:
        raise ValueError(f"unknown sign convention {sign!r} ('fill' or 'adverse')")
    t_all = np.asarray(ts, dtype=np.int64)
    n, nh = t_all.size, len(horizons_ns)
    mo = np.full((n, nh), np.nan)
    mm = np.full((n, nh), np.nan)
    pre = np.full(n, np.nan)
    if n == 0 or path is None:
        return Markouts(mo, mm, pre)
    d_all = np.asarray(side, dtype=np.float64)
    if sign == "adverse":
        d_all = -d_all
    p_all = np.asarray(px, dtype=np.int64)
    pv_all = np.ones(n, dtype=bool) if px_valid is None else np.asarray(px_valid, dtype=bool)
    if isinstance(path, PricePath):
        groups: list[tuple[PricePath | None, np.ndarray]] = [(path, np.arange(n))]
    else:
        if keys is None:
            raise ValueError("markouts: keys are required with several paths")
        by: dict[Hashable, list[int]] = {}
        for i, k in enumerate(keys):
            by.setdefault(k, []).append(i)
        groups = [(path.get(k), np.asarray(g, dtype=np.int64)) for k, g in by.items()]
    for pth, g in groups:
        if pth is None or len(pth) == 0:
            continue
        t, d, p, pv = t_all[g], d_all[g], p_all[g], pv_all[g]
        b0, a0, w0, ok0 = pth.rows(pth.index(t, strict=True), ref)
        rel0 = (b0 - p).astype(np.float64) + (a0 - b0).astype(np.float64) * w0
        pre[g[ok0]] = b0[ok0].astype(np.float64) + (a0 - b0)[ok0].astype(np.float64) * w0[ok0]
        for k, hz in enumerate(horizons_ns):
            b1, a1, w1, ok1 = pth.rows(pth.mark_index(t + int(hz), past_end), ref)
            rel1 = (b1 - p).astype(np.float64) + (a1 - b1).astype(np.float64) * w1
            good = ok1 & pv
            mo[g[good], k] = d[good] * rel1[good]
            gm = ok1 & ok0
            mm[g[gm], k] = d[gm] * (rel1[gm] - rel0[gm])
    return Markouts(mo, mm, pre)


# ---- units ----------------------------------------------------------------------------------------------------------


def ticks(x: Any, tick: Any) -> Any:
    """A markout in price units over the tick size."""
    return x / tick


def bps(x: Any, base: Any) -> Any:
    """A markout in basis points of `|base|` (the fill price for `mo`, `M(t-)` for `mm`)."""
    return x / abs(base) * 1e4


def ccy(x: float, price_exp: int, qty: int, size_exp: int, multiplier: float) -> float:
    """A markout in quote currency: `x 10^price_exp qty 10^size_exp multiplier`."""
    return x * 10.0**price_exp * qty * 10.0**size_exp * multiplier


__all__ = ["PAST_END", "REFS", "SIGNS", "Markouts", "PricePath", "bps", "ccy", "markouts", "ticks"]
