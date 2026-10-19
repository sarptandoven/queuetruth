"""Own-order-free copy of an L2-only dataset (trader review E2).

On an L2-only venue (e.g. Binance through Tardis) the user's own resting orders are part of the visible depth. An L2
queue model that evaluates the twin shadow of a live order (`score` zoo arm, `calibrate-live`) then counts the live
order itself in the queue ahead of its twin, and reports a large, fake pessimistic bias. This module writes a copy of
the dataset whose `qt.l2` depth no longer contains the own resting quantity; it is the L2 counterpart of the MBO
self-exclusion of `queuetruth.score.selfexclude` (audit ruling A1). Only the tape that queue models read changes:
BBO-derived markouts must keep using the source dataset (the real market quotes).

Input, in DATASET units (`OwnOrder`, one record per own order and limit price): instrument, side, price, the
resting-quantity step function over exchange time (`resting`: `(ts, qty resting from ts on)`; the exchange ack
starts it, each own fill and amend lowers it, cancel / expire / full fill set it to 0) and the own maker fills
`(ts, qty, px)`. `own_orders_from_log` builds the records from a canonical live log with explicit, exact
live -> dataset unit multipliers (`unit_scale`).

Transform of `qt.l2`, per partition, rows kept in source order:
- Each change of an own order's resting quantity (`d = q_k - q_{k-1}`) is aligned to the feed row that reflects
  it ("latency-aware"): among the `depth`/`snapshot` rows at the order's (side, price) with `|ts_event - ts| <=
  match_window_ns` whose quantity moves in the same direction (an increase for an add, a decrease for a fill,
  amend or cancel; `OUT_OF_VIEW` rows excluded) and whose move is not already explained by earlier own changes,
  the one that explains the whole change, then the nearest in time, then the earliest. The change then happens
  inside that row: `prev_qty` loses the own quantity before it, `new_qty` the own quantity after it. A change with
  no such row is unaligned: it takes effect before the next row at that level with `ts_event >= ts` (both sides
  of that row lose the new own quantity) and is counted (`changes_unaligned`; `..._not_visible` when the level
  was not visible at that time, e.g. outside the top-N view). Changes of one order keep their order. The first
  step of a restated order (already resting when the capture started) is applied that way by design, uncounted.
- Each depth/snapshot row at an own level: `prev_qty -= R_before`, `new_qty -= R_after` (R = own resting qty at
  that level just before / after the row), `prev_ct`/`new_ct` likewise with the number of resting own orders;
  results below 0 are clamped and counted (`clamps`, only when the feed value was positive). Rows whose adjusted
  value differs are `rows_adjusted`; a `depth` row that becomes a no-op (same qty and count before and after) is
  dropped (`drop_noop_rows`), e.g. the row of the own ack or cancel.
- Trade prints: each own fill is matched to the prints at its price on the order's (hit) side within the window
  (exact-quantity print first, then nearest, then earliest). With `own_fill_prints="keep"` (default) the prints
  stay: the twin shadow takes the live order's place in the queue, and the aggressive volume that filled the live
  order must remain available to fill the twin (same reasoning as ruling A1, where the MBO copy keeps the twin's
  prints); removing them makes the twin unable to fill whenever the live order filled, i.e. the pessimistic bias
  comes back. Matching is still done and reported (`fills_matched`, `fills_unaligned`, `fill_qty_unmatched`; a
  fill with no print usually means a unit or clock mismatch). `own_fill_prints="remove"` subtracts the matched
  quantity from `trade_qty` and drops prints that reach 0 (for models of aggressors that only take displayed
  liquidity).
- A dropped row that carried `LAST_IN_BATCH` passes the flag to the previous kept row of its batch (same
  `event_id`); a batch left without rows disappears (event ids stay monotone; gaps are allowed).
The manifest (file sha256 and row counts, `dataset_id` by the SPEC 3.10 rule; `ts_min`/`ts_max` keep the source
values: end of data is a property of the capture), `instruments.parquet`, `data_quality.json` and `trades_source/`
(copied unchanged) form the new dataset; `own_orders_removed.json` records the parameters, stats and warnings.

Limitation: every own order is removed, so the twin of an order that joined a level where another own order was
already resting no longer sees that earlier own order ahead of it (the zoo treats each order independently, as the
MBO self-exclusion does); such orders are counted (`overlapping_orders`) and warned about.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.score._io import Row, read_live_log, read_manifest
from queuetruth.score.outcomes import log_outcomes

DEFAULT_MATCH_WINDOW_NS = 1_000_000
SIDECAR = "own_orders_removed.json"
BID, ASK = ord("B"), ord("A")
LAST_IN_BATCH, OUT_OF_VIEW, INTO_VIEW, HIDDEN_TRADE = 1, 2, 4, 32
_FAR = np.int64(2**62)
_SIDES = {
    "b": BID,
    "bid": BID,
    "buy": BID,
    "a": ASK,
    "ask": ASK,
    "sell": ASK,
    str(BID): BID,
    str(ASK): ASK,
}
_NON_RESTING_TIF = ("ioc", "fok")


def _config(msg: str, hint: str | None = None) -> QtError:
    return QtError("QT-CONFIG-001", msg, hint)


def side_byte(side: Any) -> int:
    """`B` (66) for bid / buy, `A` (65) for ask / sell (accepts the byte, the letter or the word)."""
    if isinstance(side, (int, np.integer)) and int(side) in (BID, ASK):
        return int(side)
    s = _SIDES.get(str(side).strip().lower())
    if s is None:
        raise _config(f"own order side {side!r}: expected bid/ask, buy/sell, B/A or the byte 66/65")
    return s


@dataclass(frozen=True)
class OwnFill:
    """One own maker fill in dataset units: exchange time, quantity, price."""

    ts: int
    qty: int
    px: int


@dataclass(frozen=True)
class OwnOrder:
    """One own order at one limit price, in dataset units.

    `resting`: the resting-quantity step function over exchange time, `(ts, qty resting from ts on)` with `ts`
    non-decreasing (the first step is the ack; a final 0 ends the order; an order without a final 0 rests until the
    end of the data). `fills`: own maker fills (their prints are matched; see `own_fill_prints`). `restated`: the
    order was already resting when the capture started (its first step is not a feed-visible add)."""

    instrument_id: int
    side: int
    price: int
    resting: tuple[tuple[int, int], ...]
    fills: tuple[OwnFill, ...] = ()
    key: str = ""
    restated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", side_byte(self.side))
        steps = tuple((int(t), int(q)) for t, q in self.resting)
        if any(q < 0 for _, q in steps):
            raise _config(f"own order {self.key or self.price}: negative resting quantity")
        if any(b[0] < a[0] for a, b in pairwise(steps)):
            raise _config(f"own order {self.key or self.price}: resting steps are not in time order")
        fills = tuple(f if isinstance(f, OwnFill) else OwnFill(*f) for f in self.fills)
        if any(int(f.qty) <= 0 for f in fills):
            raise _config(f"own order {self.key or self.price}: fill quantities must be positive")
        object.__setattr__(self, "resting", steps)
        object.__setattr__(self, "fills", fills)
        object.__setattr__(self, "instrument_id", int(self.instrument_id))
        object.__setattr__(self, "price", int(self.price))

    def changes(self) -> list[tuple[int, int, int]]:
        """`(ts, quantity change, resting-order count change)` per step that changes the resting quantity."""
        out, prev = [], 0
        for t, q in self.resting:
            if q != prev:
                out.append((t, q - prev, (1 if prev == 0 else 0) - (1 if q == 0 else 0)))
            prev = q
        return out


@dataclass
class OwnFreeStats:
    """Counters of one `own_free_l2_dataset` run (see the module docstring)."""

    orders: int = 0
    orders_matched: int = 0
    changes: int = 0
    changes_aligned: int = 0
    changes_unaligned: int = 0
    changes_unaligned_not_visible: int = 0
    restated: int = 0
    overlapping_orders: int = 0
    rows_adjusted: int = 0
    prev_adjusted: int = 0
    new_adjusted: int = 0
    depth_rows_dropped: int = 0
    clamps: int = 0
    count_clamps: int = 0
    empty_while_resting: int = 0
    fills: int = 0
    fills_matched: int = 0
    fills_unaligned: int = 0
    fill_qty_unmatched: int = 0
    trade_rows_reduced: int = 0
    trade_rows_dropped: int = 0
    flags_moved: int = 0


@dataclass
class OwnFreeResult:
    """Output of `own_free_l2_dataset`: the new dataset root and `dataset_id`, counters and warnings."""

    root: Path
    dataset_id: str
    stats: OwnFreeStats
    warnings: list[str] = field(default_factory=list)
    match_window_ns: int = DEFAULT_MATCH_WINDOW_NS
    own_fill_prints: str = "keep"

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view (the `own_orders_removed.json` sidecar content plus `root`)."""
        return {
            "root": str(self.root),
            "dataset_id": self.dataset_id,
            "match_window_ns": self.match_window_ns,
            "own_fill_prints": self.own_fill_prints,
            "stats": asdict(self.stats),
            "warnings": list(self.warnings),
        }


# ---- units ---------------------------------------------------------------------------------------------------------


def unit_scale(live_exp: int, dataset_exp: int) -> Fraction:
    """Exact live -> dataset multiplier for fixed-point values: `10 ** (live_exp - dataset_exp)` (e.g. live
    `size_exp 0`, Tardis dataset `size_exp -8` -> 100,000,000)."""
    return Fraction(10) ** (int(live_exp) - int(dataset_exp))


def _check_scale(v: Any, name: str) -> Fraction:
    if isinstance(v, bool) or not isinstance(v, (int, Fraction)):
        raise _config(f"{name} must be an int or fractions.Fraction (exact), got {type(v).__name__}")
    f = Fraction(v)
    if f <= 0:
        raise _config(f"{name} must be positive, got {v}")
    return f


def _scaled(v: Any, f: Fraction, what: str) -> int:
    r = Fraction(int(v)) * f
    if r.denominator != 1:
        raise QtError("QT-INPUT-004", f"{what} {v} times {f} is not an integer in dataset units")
    return int(r)


# ---- live log -> own orders ----------------------------------------------------------------------------------------

_START = ("ack", "restate")
_END = ("cancel_ack", "expire", "done")
_PRIO = {"ack": 0, "restate": 0, "fill": 1, "amend_ack": 2, "cancel_ack": 3, "expire": 3, "done": 3}


def own_orders_from_log(
    orders: list[Row],
    events: list[Row],
    *,
    price_scale: int | Fraction = 1,
    size_scale: int | Fraction = 1,
) -> tuple[list[OwnOrder], list[str]]:
    """Own resting orders of a canonical live log (`orders`, `events` rows as `score._io.read_live_log` returns
    them), converted to dataset units with the exact multipliers `price_scale` and `size_scale` (`unit_scale`).

    Per order (limit orders that can rest; IOC/FOK, non-limit orders and orders without `instrument_id` or limit
    price are skipped and counted): the resting quantity starts at the `ack` (or `restate`) exchange time at the
    order total minus fills so far; net fills come from `outcomes.log_outcomes` (dedupe, busts, corrections); a
    maker / unknown-liquidity fill lowers it and is listed for print matching; a taker fill lowers it too (entry-time
    aggression before any other change of the leg is folded into the start step) but its print is never touched;
    `amend_ack` sets the new total (and a new price starts a new record); `cancel_ack` / `expire` / `done` end it.
    Lifecycle event times are `ts_exchange`, else `ts_local_recv` (counted: local times rarely align with the
    feed); fill times are those of `log_outcomes` (`ts_exchange`, else the local times).
    Returns (records, warnings)."""
    ps, ss = _check_scale(price_scale, "price_scale"), _check_scale(size_scale, "size_scale")
    outcomes, warnings = log_outcomes(orders, events)
    warnings = [f"own_orders: {w}" for w in warnings]
    by: dict[str, list[Row]] = {}
    for e in events:
        by.setdefault(str(e["order_key"]), []).append(e)
    skipped: dict[str, int] = {}
    local_time = 0
    folded = 0
    out: list[OwnOrder] = []

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    for o in sorted(orders, key=lambda r: str(r["order_key"])):
        key = str(o["order_key"])
        if o.get("order_type") not in (None, "limit"):
            skip("not a limit order")
            continue
        if o.get("tif") in _NON_RESTING_TIF:
            skip("IOC/FOK (never rests)")
            continue
        if o.get("instrument_id") is None:
            skip("without instrument_id (pass an instrument map to `live import`)")
            continue
        try:
            side = side_byte(o.get("side"))
        except QtError:
            skip("with an unknown side")
            continue
        timeline: list[tuple[int, int, int, str, Any]] = []
        for i, ev in enumerate(by.get(key, [])):
            et = str(ev["event_type"])
            if et not in _PRIO or et == "fill":
                continue
            t = ev.get("ts_exchange")
            if t is None and ev.get("ts_local_recv") is not None:
                t = ev["ts_local_recv"]
                local_time += 1
            if t is None:
                continue
            timeline.append((int(t), _PRIO[et], i, et, ev))
        oc = outcomes.get(key)
        for j, f in enumerate(oc.fills if oc is not None else []):
            timeline.append((int(f.ts), _PRIO["fill"], j, "fill", f))
        timeline.sort(key=lambda x: (x[0], x[1], x[2]))
        if not any(x[3] in _START for x in timeline):
            skip("without an ack or restate (never seen resting)")
            continue
        legs, n_folded, why = _walk(o, timeline, ps, ss)
        folded += n_folded
        if why is not None:
            skip(why)
            continue
        for k, lg in enumerate(legs):
            out.append(
                OwnOrder(
                    instrument_id=int(o["instrument_id"]),
                    side=side,
                    price=int(lg["px"]),
                    resting=tuple((int(t), int(q)) for t, q in lg["steps"]),
                    fills=tuple(lg["fills"]),
                    key=key if len(legs) == 1 else f"{key}#leg{k}",
                    restated=bool(lg["restated"]),
                )
            )
    for why, n in sorted(skipped.items()):
        warnings.append(f"own_orders: skipped {n} order(s) {why}")
    if local_time:
        warnings.append(
            f"own_orders: {local_time} lifecycle event(s) without ts_exchange use ts_local_recv; they align with the "
            "feed only when the receive delay is inside the match window"
        )
    if folded:
        warnings.append(f"own_orders: {folded} taker fill(s) at entry reduce the order's initial resting quantity")
    return out, warnings


def _walk(
    o: Row, timeline: list[tuple[int, int, int, str, Any]], ps: Fraction, ss: Fraction
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Resting legs (`px`, `steps` [[ts, qty]], `fills`, `restated`) of one order from its time-ordered lifecycle
    events and net fills; returns (legs, taker fills folded into the start, skip reason or None)."""
    legs: list[dict[str, Any]] = []
    leg: dict[str, Any] | None = None
    total = _scaled(o.get("orig_qty") or 0, ss, "orig_qty")
    filled = 0
    folded = 0
    for t, _, _, et, ev in timeline:
        if et in _START:
            if leg is not None:
                continue
            px_raw = ev.get("px") if ev.get("px") is not None else o.get("limit_px")
            if px_raw is None:
                return [], folded, "without a limit price"
            if ev.get("qty") is not None:
                total = _scaled(ev["qty"], ss, "ack qty")
            q = max(total - filled, 0)
            if et == "restate" and ev.get("leaves_qty") is not None:
                q = _scaled(ev["leaves_qty"], ss, "restate leaves_qty")
            px = _scaled(px_raw, ps, "limit price")
            leg = {"px": px, "steps": [[t, q]], "fills": [], "touched": False, "restated": et == "restate"}
        elif et == "fill":
            q = _scaled(ev.qty, ss, "fill qty")
            filled += q
            if leg is None:
                continue
            rest = max(total - filled, 0)
            if ev.liquidity == "taker" and not leg["touched"]:
                leg["steps"][0][1] = rest
                folded += 1
                continue
            leg["steps"].append([t, rest])
            if ev.liquidity != "taker":
                leg["fills"].append(OwnFill(t, q, _scaled(ev.px, ps, "fill price")))
            leg["touched"] = True
            if rest == 0:
                legs.append(leg)
                break
        elif et == "amend_ack":
            if leg is None:
                continue
            if ev.get("qty") is not None:
                total = _scaled(ev["qty"], ss, "amend qty")
            rest = max(total - filled, 0)
            new_px = _scaled(ev["px"], ps, "amend price") if ev.get("px") is not None else leg["px"]
            if new_px != leg["px"]:
                leg["steps"].append([t, 0])
                legs.append(leg)
                leg = {"px": new_px, "steps": [[t, rest]], "fills": [], "touched": False, "restated": False}
            else:
                leg["steps"].append([t, rest])
                leg["touched"] = True
        elif et in _END:
            if leg is not None:
                leg["steps"].append([t, 0])
                legs.append(leg)
            leg = None
            break
    else:
        if leg is not None:
            legs.append(leg)
    return legs, folded, None


# ---- the transform -------------------------------------------------------------------------------------------------


def _ints(t: pa.Table, name: str, fill: int = 0) -> tuple[np.ndarray, np.ndarray]:
    col = t.column(name)
    if pa.types.is_timestamp(col.type):
        col = col.cast(pa.int64())
    valid = np.asarray(pc.is_valid(col).to_numpy(zero_copy_only=False), dtype=bool)
    vals = np.asarray(pc.fill_null(col, fill).to_numpy(zero_copy_only=False)).astype(np.int64)
    return vals, valid


def _group(idx: np.ndarray, side: np.ndarray, price: np.ndarray) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """Per side byte: (row indices sorted by (price, row), their prices)."""
    out = {}
    for s in (BID, ASK):
        rows = idx[side[idx] == s]
        order = np.lexsort((rows, price[rows]))
        rows = rows[order]
        out[s] = (rows, price[rows])
    return out


def _rows_at(groups: dict[int, tuple[np.ndarray, np.ndarray]], side: int, px: int) -> np.ndarray:
    rows, prices = groups[side]
    a, b = np.searchsorted(prices, px, "left"), np.searchsorted(prices, px, "right")
    return rows[a:b]


@dataclass
class _Change:
    t: int
    d: int
    dn: int
    order: int
    seq: int
    restated_start: bool


def _sub(vals: np.ndarray, own: np.ndarray, applies: np.ndarray) -> tuple[np.ndarray, int]:
    """`vals - own` clamped at 0 where `applies`; returns (result, clamps where 0 < vals < own)."""
    res = vals.copy()
    m = applies & (own > 0)
    res[m] = np.maximum(vals[m] - own[m], 0)
    return res, int((m & (vals > 0) & (vals < own)).sum())


def _process(
    t: pa.Table,
    orders: list[OwnOrder],
    window: int,
    fill_prints: str,
    drop_noop: bool,
    st: OwnFreeStats,
    matched: set[int],
) -> pa.Table:
    n = t.num_rows
    if n == 0 or not orders:
        return t
    ts, _ = _ints(t, "ts_event")
    kind = np.asarray(pc.cast(t.column("kind"), pa.string()).to_numpy(zero_copy_only=False), dtype=object)
    side, _ = _ints(t, "side")
    price, pvalid = _ints(t, "price")
    prev, _ = _ints(t, "prev_qty")
    new, _ = _ints(t, "new_qty")
    prev_ct, prev_ct_ok = _ints(t, "prev_ct")
    new_ct, new_ct_ok = _ints(t, "new_ct")
    tq, _ = _ints(t, "trade_qty")
    aggr, _ = _ints(t, "aggressor")
    flags, _ = _ints(t, "flags")
    eid, _ = _ints(t, "event_id")
    lo, hi = int(ts.min()), int(ts.max())
    is_depth = kind == "depth"
    is_snap = kind == "snapshot"
    is_trade = kind == "trade"
    oov = (flags & OUT_OF_VIEW) != 0
    lvl = (is_depth | is_snap) & pvalid & np.isin(side, (BID, ASK))
    levels = _group(np.flatnonzero(lvl), side, price)
    hit = np.where(np.isin(side, (BID, ASK)), side, np.where(aggr == BID, ASK, np.where(aggr == ASK, BID, 0)))
    trd = is_trade & pvalid & ((flags & HIDDEN_TRADE) == 0) & (hit != 0)
    trades = _group(np.flatnonzero(trd), hit, price)

    # own changes per level, in this partition's time range
    per_level: dict[tuple[int, int], list[_Change]] = {}
    for oi, o in enumerate(orders):
        ch = o.changes()
        if not ch or ch[0][0] > hi + window:
            continue
        prior = sum(d for tt, d, _ in ch if tt < lo - window)
        if prior > 0 or any(lo - window <= tt <= hi + window for tt, _, _ in ch):
            matched.add(id(o))
        for k, (tt, d, dn) in enumerate(ch):
            if tt > hi + window:
                break
            per_level.setdefault((o.side, o.price), []).append(_Change(tt, d, dn, oi, k, o.restated and k == 0))

    r_before = np.zeros(n, np.int64)
    r_after = np.zeros(n, np.int64)
    c_before = np.zeros(n, np.int64)
    c_after = np.zeros(n, np.int64)
    for (s, px), changes in per_level.items():
        rows = _rows_at(levels, s, px)
        ts_g = ts[rows]
        delta = new[rows] - prev[rows]
        usable = ~oov[rows]
        cap_up = np.where(usable & (delta > 0), delta, 0)
        cap_down = np.where(usable & (delta < 0), -delta, 0)
        min_pos: dict[int, int] = {}
        pos_list: list[tuple[int, int, int]] = []
        changes.sort(key=lambda c: (c.t, c.order, c.seq))
        # overlapping own orders: an order starting while another own order rests at the level
        resting_now: dict[int, int] = {}
        for c in changes:
            if c.dn > 0 and c.t >= lo - window and any(q > 0 for oi, q in resting_now.items() if oi != c.order):
                st.overlapping_orders += 1
            resting_now[c.order] = resting_now.get(c.order, 0) + c.d
        for c in changes:
            floor = min_pos.get(c.order, -1)
            if c.t < lo - window:
                pos = -1
            elif c.restated_start:
                st.restated += 1
                pos = _next_row_pos(rows, ts_g, c.t, floor)
            else:
                st.changes += 1
                cap = cap_up if c.d > 0 else cap_down
                cand = np.flatnonzero((np.abs(ts_g - c.t) <= window) & (cap > 0) & (2 * rows + 1 >= floor))
                if cand.size:
                    need = abs(c.d)
                    k = cand[np.lexsort((rows[cand], np.abs(ts_g[cand] - c.t), cap[cand] < need))[0]]
                    cap[k] -= min(cap[k], need)
                    pos = int(2 * rows[k] + 1)
                    st.changes_aligned += 1
                else:
                    st.changes_unaligned += 1
                    before = np.flatnonzero(ts_g <= c.t)
                    if before.size == 0 or new[rows[before[-1]]] == 0 or oov[rows[before[-1]]]:
                        st.changes_unaligned_not_visible += 1
                    pos = _next_row_pos(rows, ts_g, c.t, floor)
            if pos < int(_FAR):
                min_pos[c.order] = max(pos, floor)
                pos_list.append((max(pos, floor), c.d, c.dn))
        if not pos_list or rows.size == 0:
            continue
        pos_list.sort()
        p = np.array([x[0] for x in pos_list], np.int64)
        cd = np.cumsum([x[1] for x in pos_list]).astype(np.int64)
        cn = np.cumsum([x[2] for x in pos_list]).astype(np.int64)
        kb = np.searchsorted(p, 2 * rows, "right")
        ka = np.searchsorted(p, 2 * rows + 1, "right")
        r_before[rows] = np.where(kb > 0, cd[np.maximum(kb - 1, 0)], 0)
        r_after[rows] = np.where(ka > 0, cd[np.maximum(ka - 1, 0)], 0)
        c_before[rows] = np.where(kb > 0, cn[np.maximum(kb - 1, 0)], 0)
        c_after[rows] = np.where(ka > 0, cn[np.maximum(ka - 1, 0)], 0)

    r_before = np.maximum(r_before, 0)
    r_after = np.maximum(r_after, 0)
    c_before = np.maximum(c_before, 0)
    c_after = np.maximum(c_after, 0)
    new_prev, cl1 = _sub(prev, r_before, lvl)
    new_new, cl2 = _sub(new, r_after, lvl)
    new_pct, cc1 = _sub(prev_ct, c_before, lvl & prev_ct_ok)
    new_nct, cc2 = _sub(new_ct, c_after, lvl & new_ct_ok)
    st.clamps += cl1 + cl2
    st.count_clamps += cc1 + cc2
    into = (flags & INTO_VIEW) != 0
    st.empty_while_resting += int(
        (lvl & is_depth & (new == 0) & (r_after > 0) & ~oov).sum()
        + (lvl & is_depth & (prev == 0) & (r_before > 0) & ~into).sum()
    )
    p_chg = new_prev != prev
    n_chg = new_new != new
    adjusted = p_chg | n_chg | (new_pct != prev_ct) | (new_nct != new_ct)
    st.prev_adjusted += int(p_chg.sum())
    st.new_adjusted += int(n_chg.sum())
    st.rows_adjusted += int((p_chg | n_chg).sum())
    keep = np.ones(n, dtype=bool)
    if drop_noop:
        same_ct = (~prev_ct_ok | ~new_ct_ok) | (new_pct == new_nct)
        noop = is_depth & adjusted & (new_prev == new_new) & same_ct
        keep &= ~noop
        st.depth_rows_dropped += int(noop.sum())

    # own fills -> trade prints
    rem = tq.copy()
    for o in orders:
        for f in o.fills:
            if f.ts < lo - window or f.ts > hi + window:
                continue
            st.fills += 1
            rows = _rows_at(trades, o.side, int(f.px))
            left = int(f.qty)
            cand = rows[(np.abs(ts[rows] - f.ts) <= window) & (rem[rows] > 0)] if rows.size else rows
            if cand.size:
                order = np.lexsort((cand, np.abs(ts[cand] - f.ts), rem[cand] != left))
                for r in cand[order]:
                    take = min(int(rem[r]), left)
                    rem[r] -= take
                    left -= take
                    if left == 0:
                        break
            if left == 0:
                st.fills_matched += 1
            else:
                st.fills_unaligned += 1
                st.fill_qty_unmatched += left
    if fill_prints == "remove":
        reduced = is_trade & (rem < tq)
        gone = reduced & (rem == 0)
        st.trade_rows_reduced += int((reduced & ~gone).sum())
        st.trade_rows_dropped += int(gone.sum())
        keep &= ~gone
        new_tq = rem
    else:
        new_tq = tq

    # LAST_IN_BATCH of dropped rows -> previous kept row of the same batch
    new_flags = flags.copy()
    if not keep.all():
        start = np.ones(n, dtype=bool)
        start[1:] = ((flags[:-1] & LAST_IN_BATCH) != 0) | (eid[1:] != eid[:-1])
        batch = np.cumsum(start) - 1
        last_kept = np.maximum.accumulate(np.where(keep, np.arange(n), -1))
        for i in np.flatnonzero(~keep & ((flags & LAST_IN_BATCH) != 0)):
            j = int(last_kept[i])
            if j >= 0 and batch[j] == batch[i]:
                new_flags[j] |= LAST_IN_BATCH
                st.flags_moved += 1

    def col(name: str, vals: np.ndarray, valid: np.ndarray | None = None) -> None:
        nonlocal t
        i = t.schema.get_field_index(name)
        f = t.schema.field(i)
        arr = pa.array(vals, mask=None if valid is None else ~valid).cast(f.type)
        t = t.set_column(i, f, arr)

    col("prev_qty", new_prev)
    col("new_qty", new_new)
    col("prev_ct", new_pct, prev_ct_ok)
    col("new_ct", new_nct, new_ct_ok)
    col("trade_qty", new_tq)
    col("flags", new_flags)
    return t.filter(pa.array(keep))


def _next_row_pos(rows: np.ndarray, ts_g: np.ndarray, t: int, floor: int) -> int:
    """Half-unit position just before the first level row with `ts_event >= t` at or after `floor`."""
    cand = np.flatnonzero((ts_g >= t) & (2 * rows >= floor))
    return int(2 * rows[cand[0]]) if cand.size else int(_FAR)


#: `qt.l2` columns the transform reads.
L2_COLUMNS = ("ts_event", "event_id", "kind", "side", "price", "prev_qty", "new_qty", "trade_qty", "flags")


def _read_l2_file(path: Path) -> pa.Table:
    """A `qt.l2` partition file; unreadable or not an L2 table is `QT-INPUT-001` / `QT-INPUT-002`."""
    if not path.exists():
        raise QtError("QT-INPUT-001", f"{path}: L2 partition file not found")
    try:
        t = pq.read_table(path)
    except (OSError, pa.ArrowException) as e:
        raise QtError("QT-INPUT-002", f"{path}: unreadable Parquet: {e}") from None
    missing = [c for c in L2_COLUMNS if c not in t.column_names]
    if missing:
        raise QtError("QT-INPUT-002", f"{path}: not a qt.l2 table (missing columns {missing})")
    return t


def own_free_l2_dataset(
    dataset_root: str | Path,
    out: str | Path,
    own_orders: Iterable[OwnOrder],
    *,
    match_window_ns: int = DEFAULT_MATCH_WINDOW_NS,
    own_fill_prints: Literal["keep", "remove"] = "keep",
    drop_noop_rows: bool = True,
) -> OwnFreeResult:
    """Writes to `out` a copy of the L2-only dataset at `dataset_root` without the own resting orders `own_orders`
    (dataset units) in its `qt.l2` depth; see the module docstring for the semantics. Raises `QT-CONFIG-001` for an
    MBO dataset (use `selfexclude`), a negative window, an unknown `own_fill_prints` or `out` equal to the source."""
    src, dst = Path(dataset_root), Path(out)
    if isinstance(match_window_ns, bool) or not isinstance(match_window_ns, int) or match_window_ns < 0:
        raise _config(f"match_window_ns must be a non-negative integer (ns), got {match_window_ns!r}")
    if own_fill_prints not in ("keep", "remove"):
        raise _config(f"own_fill_prints must be 'keep' or 'remove', got {own_fill_prints!r}")
    if not (src / "manifest.json").exists():
        raise QtError("QT-INPUT-001", f"{src / 'manifest.json'}: dataset manifest not found")
    manifest = read_manifest(src)
    if str(manifest.get("kind", "mbo")) != "l2":
        raise _config(
            f"{src}: own-order removal from the L2 depth needs an L2-only dataset (kind {manifest.get('kind')!r})",
            "MBO datasets exclude the live orders by order id (queuetruth.score.selfexclude)",
        )
    if dst.resolve() == src.resolve():
        raise _config("out must differ from the source dataset directory")
    orders = list(own_orders)
    st = OwnFreeStats(orders=len(orders))
    by_inst: dict[int, list[OwnOrder]] = {}
    for o in orders:
        by_inst.setdefault(o.instrument_id, []).append(o)
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("instruments.parquet", "data_quality.json"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    if (src / "trades_source").exists():
        shutil.copytree(src / "trades_source", dst / "trades_source", dirs_exist_ok=True)
    matched: set[int] = set()
    shas: list[str] = []
    part_iids = set()
    for p in manifest.get("partitions", []) or []:
        iid = int(p["instrument_id"])
        part_iids.add(iid)
        files = list(p.get("files", []))
        mine = by_inst.get(iid, [])
        if mine and files:
            tables = [_read_l2_file(src / f["path"]) for f in files]
            which = np.concatenate([np.full(tb.num_rows, i, np.int64) for i, tb in enumerate(tables)])
            whole = pa.concat_tables(tables).append_column("__file", pa.array(which))
            done = _process(whole, mine, match_window_ns, own_fill_prints, drop_noop_rows, st, matched)
            fidx = np.asarray(done.column("__file").to_numpy())
            done = done.drop_columns(["__file"])
            for i, f in enumerate(files):
                part = done.filter(pa.array(fidx == i))
                write_table(part, dst / f["path"], SCHEMAS["qt.l2"])
                f["rows"] = part.num_rows
        else:
            for f in files:
                (dst / f["path"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / f["path"], dst / f["path"])
        for f in files:
            f["sha256"] = sha256_file(dst / f["path"])
            shas.append(f["sha256"])
        p["rows"] = sum(int(f.get("rows") or 0) for f in files)
    st.orders_matched = len(matched)
    ident = "\n".join(sorted(shas)) + "\n" + str(manifest.get("source_format", ""))
    source_id = str(manifest.get("dataset_id", ""))
    manifest["dataset_id"] = hashlib.sha256(ident.encode()).hexdigest()
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    warnings = _warnings(st, match_window_ns, own_fill_prints, sorted(set(by_inst) - part_iids))
    res = OwnFreeResult(dst, manifest["dataset_id"], st, warnings, match_window_ns, own_fill_prints)
    side = {**res.to_dict(), "source_dataset_id": source_id, "drop_noop_rows": drop_noop_rows}
    side.pop("root")
    (dst / SIDECAR).write_text(json.dumps(side, indent=2) + "\n", encoding="utf-8")
    return res


def _warnings(st: OwnFreeStats, window: int, prints: str, no_partition: Sequence[int]) -> list[str]:
    w = []
    if no_partition:
        w.append(f"own_orders: instrument(s) {', '.join(map(str, no_partition))} have no partition in the dataset")
    if st.orders and st.orders_matched < st.orders:
        w.append(f"own_orders: {st.orders - st.orders_matched} of {st.orders} own order(s) fall outside the data")
    if st.changes_unaligned:
        w.append(
            f"own_orders: {st.changes_unaligned} of {st.changes} own resting-quantity change(s) matched no feed row "
            f"at their level within {window} ns ({st.changes_unaligned_not_visible} where the level was not "
            "visible); applied at their exchange time"
        )
    if st.fills_unaligned:
        w.append(
            f"own_orders: {st.fills_unaligned} of {st.fills} own fill(s) ({st.fill_qty_unmatched} qty) matched no "
            f"trade print within {window} ns (check units and clocks)"
            + ("; those prints were left in the tape" if prints == "remove" else "")
        )
    if st.clamps:
        w.append(
            f"own_orders: {st.clamps} depth value(s) smaller than the own resting quantity were clamped at 0 (feed "
            "and live log disagree)"
        )
    if st.empty_while_resting:
        w.append(
            f"own_orders: {st.empty_while_resting} depth row(s) show an empty level while an own order rests there"
        )
    if st.overlapping_orders:
        w.append(
            f"own_orders: {st.overlapping_orders} own order(s) joined a level where another own order was resting; "
            "their shadows no longer see that earlier own order ahead of them"
        )
    return w


def own_free_l2_from_live(
    dataset_root: str | Path,
    live: str | Path,
    out: str | Path,
    *,
    price_scale: int | Fraction = 1,
    size_scale: int | Fraction = 1,
    match_window_ns: int = DEFAULT_MATCH_WINDOW_NS,
    own_fill_prints: Literal["keep", "remove"] = "keep",
    drop_noop_rows: bool = True,
) -> OwnFreeResult:
    """`own_orders_from_log` on the canonical live-log directory `live`, then `own_free_l2_dataset`; the builder's
    warnings lead the result's warnings."""
    orders, events = read_live_log(live)
    own, warn = own_orders_from_log(orders, events, price_scale=price_scale, size_scale=size_scale)
    res = own_free_l2_dataset(
        dataset_root,
        out,
        own,
        match_window_ns=match_window_ns,
        own_fill_prints=own_fill_prints,
        drop_noop_rows=drop_noop_rows,
    )
    res.warnings = [*warn, *res.warnings]
    return res
