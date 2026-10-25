"""`queuetruth pm calibrate`: queue-position models scored against the queue truth the user's own orders reveal.

Two kinds of truth, both from the user's own data:

1. Kalshi's official queue position (`GET /portfolio/orders/{order_id}/queue_position`, "the number of preceding
   shares before the order in the queue ... determined using a price-time priority"), sampled by the user while the
   order rests. A model's estimate at the sample is its queue ahead `F` (other traders' size, from the own-free
   book) plus the user's own size resting ahead at the same price (own orders that joined earlier, from the live
   log). Samples are placed on the book's clock by their local receive time, or by the midpoint of the local send
   and receive times when the request was captured (`sample_time = mid`).
2. The queue ahead at an own maker fill (both venues): under FIFO, the prints of the batch that first filled the
   order consumed everything ahead of it first, so just before that batch the other traders' queue ahead was
   exactly the batch's at-price print volume minus the order's own fills in that batch and minus the user's own size
   resting ahead, when those fills did not complete the order (otherwise it is only an upper bound and the order is
   left out). Each own fill belongs to the batch of the nearest at-price print within the own-match window that
   still has print volume for it (the L2 rows carry no trade ids), so a second fill from a later taker never counts
   in the first batch. This needs no venue queue endpoint, so it is the fit target on Polymarket.
3. Polymarket with an on-chain export (`pm ingest --chain`): the operator's fill order gives the other traders'
   queue ahead at that batch, completed orders included (`qt.pm_chain_queue`), when Polymarket matches in
   price-time order (its docs do not say; the kit assumes it); it replaces rule 2 for the orders it covers
   (`fill_truth.n_chain`).

Rules 2 and 3 see only orders that filled, and an order fills when more of the queue ahead than usual left: that
truth leans to short queues, and a fit on it (`fit_on = fill_truth`) favours models that fill early. The kit warns
whenever it fits on it, and checks every model on the test orders that never filled live (`unfilled_filled`: those
the model filled while they rested, which a queue that is too short does).

With `Q0` the visible size at entry and `V` the other traders' at-price prints between entry and the first
filling batch, the size that left the queue ahead by cancels was `Q0 - V - F_true`, and the cancels at the level
in that time were the level's decreases minus `V`.

Fit: families with a parameter (`power`, `power2`, `power3`) are run on a grid of `ln n`; `log` and `log2` have
none; each also in its `atomic` variant (a decrease beyond the prints is one order leaving, `queuetruth.pm.queue`)
unless `atomic` is false; the queue bounds `cancels_ahead` and `risk_adverse` and the midpoint of the two are always
scored. Own orders are split by entry time: the first `train_fraction` train, the rest test (`test_from`). The loss
(`loss`) is order-weighted: each order's errors are reduced first (mean squared error for `rmse`, the default, whose
root is taken after averaging over orders; mean absolute error for `mae`), so long-lived orders do not dominate. RMSE
is the default because a cancel is either ahead of the order or behind it: the median that MAE rewards commits every
ambiguous cancel to one side, which biases the queue and the fills. The fitted model is the one with the smallest
train loss over every family; its test loss carries a percentile bootstrap interval over test orders. Errors are in
contracts (Kalshi) or shares (Polymarket). `bounds_coverage` is the share of official samples inside
`[cancels_ahead, risk_adverse]` (plus own size ahead) somewhere between the sample's send and receive times; on an
unconflated FIFO feed it is 1, and less points at missed deltas, misaligned clocks or netted changes.

Trust (`trust`, in the form of `calibrate-live`'s): the fit is `refused` for any of `PM_REFUSING`: nothing fitted
(`no_estimate`), fewer than `MIN_ORDERS` orders with truth in the train or the test half (`few_orders`), no test
order with truth (`no_held_out`), the best `n` at an end of the grid (`at_grid_edge`), a queue bound as the best fit
(`best_is_bound`) or a best fit no better than the bounds midpoint on the test orders (`not_beating_midpoint`); else
`usable`. `fill_truth_selection` flags a fit on fill truth. The reasons are also `trust:` warning lines, and `pm
score --calibration` repeats a refusal in its warnings and headline.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from typing import Any

import numpy as np

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.pm.dataset import L2Arrays
from queuetruth.pm.models import (
    FillTruth,
    ModelFit,
    PmCalibration,
    PmCalibrationSpec,
    PmCalibrationTrust,
    validate_spec,
)
from queuetruth.pm.queue import K_DEPTH, K_TRADE, Engine, Model, SimOrder, _batch_starts
from queuetruth.pm.terms import TERMS_NOTICE
from queuetruth.pm.workdir import Leg, PmDir, legs, load, resolve_ref
from queuetruth.schemas.runid import run_id, spec_echo
from queuetruth.stats.resampling import bootstrap_rng, percentile_ci

PARAM_FAMILIES = ("power", "power2", "power3")
FIXED_FAMILIES = ("log", "log2")
LO, HI = "cancels_ahead", "risk_adverse"
MIDPOINT = "bounds_midpoint"
DEFAULT_FILL_WINDOW_NS = {"kalshi": 1_000_000_000, "polymarket": 2_000_000_000}
#: Fewest orders with queue truth in each of the train and test halves of a usable fit (as `calibrate-live`'s 30).
MIN_ORDERS = 30
#: Tokens that make a calibration `refused`, in report order.
PM_REFUSING = ("no_estimate", "few_orders", "no_held_out", "at_grid_edge", "best_is_bound", "not_beating_midpoint")


@dataclass
class _Sample:
    leg: int
    official: int
    t_align: int
    t_send: int | None
    t_recv: int
    own_ahead: int


@dataclass
class _Probe:
    """Queue truth at one leg's first filling batch."""

    leg: int
    f_true: float
    f_model: np.ndarray
    q0: float
    prints_before: float
    cancels: float
    source: str  # l2 | chain


def models_for(sp: PmCalibrationSpec) -> list[Model]:
    """The bounds first, then every family's grid (`ln n` from `grid_lo` to `grid_hi`; `n` to 6 significant
    digits, so model ids stay short and parse back exactly), and with `sp.atomic` the family's `atomic` variants
    after its plain ones."""
    out = [Model(LO), Model(HI)]
    grid = np.linspace(sp.grid_lo, sp.grid_hi, sp.grid_n)
    seen = {LO, HI}
    variants = (False, True) if sp.atomic else (False,)
    for f in sp.families:
        if f in seen:
            continue
        seen.add(f)
        if f in PARAM_FAMILIES:
            out += [Model(f, float(f"{np.exp(x):.6g}"), a) for a in variants for x in grid]
        elif f in FIXED_FAMILIES:
            out += [Model(f, None, a) for a in variants]
        else:
            raise QtError(
                "QT-CONFIG-001", f"families: {f!r} is not one of {[*PARAM_FAMILIES, *FIXED_FAMILIES, LO, HI]}"
            )
    return out


def resting_at(g: Leg, t: int) -> int:
    """A leg's live resting size at exchange time `t` (0 before entry or after its end)."""
    if t < g.entry_ts or (g.end_ts is not None and g.end_ts <= t):
        return 0
    total = g.qty
    for at, tot in g.amends:
        if at <= t:
            total = tot
    filled = sum(q for ft, q, _p in g.fills if ft <= t)
    return max(total - filled, 0)


class LevelIndex:
    """The user's legs grouped by level (instrument, side, price): which legs rest at each moment, for `own_ahead`,
    and every maker fill with running totals, for `fills_between`. Exact equivalents of scanning every leg, at the
    cost of the legs resting at once, so a long capture with thousands of orders stays linear."""

    def __init__(self, all_legs: list[Leg]) -> None:
        self.legs = all_legs
        groups: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for i, g in enumerate(all_legs):
            groups[(g.instrument_id, g.side, g.price)].append(i)
        # level -> (change times, the legs resting from each change time to the next): a leg rests on [entry, end)
        self._resting: dict[tuple[int, int, int], tuple[list[int], list[tuple[int, ...]]]] = {}
        self._fills: dict[tuple[int, int, int], tuple[list[int], list[int]]] = {}
        for k, idx in groups.items():
            starts: dict[int, list[int]] = defaultdict(list)
            ends: dict[int, list[int]] = defaultdict(list)
            for i in idx:
                g = all_legs[i]
                starts[g.entry_ts].append(i)
                if g.end_ts is not None:
                    ends[g.end_ts].append(i)
            times = sorted(set(starts) | set(ends))
            alive: set[int] = set()
            sets: list[tuple[int, ...]] = []
            for t in times:
                alive.update(starts.get(t, ()))
                alive.difference_update(ends.get(t, ()))
                sets.append(tuple(sorted(alive)))
            self._resting[k] = (times, sets)
            fills = sorted((t, q) for i in idx for t, q, _p in all_legs[i].fills)
            self._fills[k] = ([t for t, _q in fills], list(accumulate((q for _t, q in fills), initial=0)))

    def own_ahead(self, i: int, t: int) -> int:
        """Own size resting ahead of leg `i` at its price at time `t`: legs at the same level that joined earlier
        and rest at `t`."""
        g = self.legs[i]
        times, sets = self._resting[(g.instrument_id, g.side, g.price)]
        pos = bisect_right(times, t) - 1
        if pos < 0:
            return 0
        key = (g.entry_seq, g.inside, g.entry_ts, g.key)
        out = 0
        for j in sets[pos]:
            h = self.legs[j]
            if j != i and (h.entry_seq, h.inside, h.entry_ts, h.key) < key:
                out += resting_at(h, t)
        return out

    def fills_between(self, i: int, t0: int, t1: int) -> int:
        """Maker fills of the user's other legs at leg `i`'s level with `t0 <= ts < t1`."""
        g = self.legs[i]
        times, cum = self._fills[(g.instrument_id, g.side, g.price)]
        total = cum[bisect_left(times, t1)] - cum[bisect_left(times, t0)]
        return total - sum(q for t, q, _p in g.fills if t0 <= t < t1)


def own_ahead(i: int, all_legs: list[Leg], t: int) -> int:
    """Own size resting ahead of leg `i` at its price at time `t`: legs at the same level that joined earlier."""
    return LevelIndex(all_legs).own_ahead(i, t)


def _trades_by_level(rows: L2Arrays) -> dict[tuple[int, int], np.ndarray]:
    """Row indices of the trade rows per hit level `(side, price)`, in row order."""
    idx = np.flatnonzero(rows.kind == K_TRADE)
    out: dict[tuple[int, int], list[int]] = defaultdict(list)
    for r, s, p in zip(idx.tolist(), rows.side[idx].tolist(), rows.price[idx].tolist(), strict=True):
        out[(s, p)].append(r)
    return {k: np.asarray(v, dtype=np.int64) for k, v in out.items()}


def _fill_batches(g: Leg, rows: L2Arrays, starts: np.ndarray, at_level: np.ndarray, window: int) -> list[int | None]:
    """The published batch of each of the leg's maker fills (None when no print at its level is within `window`):
    the batch of the nearest at-level print (ties: earliest) whose batch still has print volume for the fill, else
    of the nearest. A batch's at-price prints hold every fill they made, own fills included."""
    ts = rows.ts_event[at_level]
    batch = np.searchsorted(starts, at_level, side="right") - 1
    volume: dict[int, float] = {}
    for b, q in zip(batch.tolist(), rows.trade_qty[at_level].tolist(), strict=True):
        volume[b] = volume.get(b, 0.0) + float(q)
    out: list[int | None] = []
    for t, q, _p in g.fills:
        near = np.flatnonzero(np.abs(ts - t) <= window)
        if near.size == 0:
            out.append(None)
            continue
        near = near[np.lexsort((near, np.abs(ts[near] - t)))]
        cands = [int(batch[k]) for k in near.tolist()]
        pick = next((b for b in cands if volume[b] >= q - 1e-9), cands[0])
        volume[pick] -= q
        out.append(pick)
    return out


def _fill_batch(
    g: Leg,
    rows: L2Arrays,
    starts: np.ndarray,
    window: int,
    trades: dict[tuple[int, int], np.ndarray] | None = None,
) -> tuple[int, float, float] | None:
    """(batch index of the leg's first maker fill, at-price print volume in that batch, the leg's fills in that
    batch), or None when no print at the leg's level is within `window` of the first fill. Only fills whose own
    prints are in that batch count (`_fill_batches`), never a later taker's. `trades` is `_trades_by_level(rows)`
    (computed here when absent)."""
    if not g.fills:
        return None
    at_level = (trades if trades is not None else _trades_by_level(rows)).get((g.side, g.price))
    if at_level is None:
        return None
    where = _fill_batches(g, rows, starts, at_level, window)
    bi = where[0]
    if bi is None:
        return None
    a, b = int(starts[bi]), int(starts[bi + 1]) if bi + 1 < starts.size else len(rows)
    hit = (rows.kind[a:b] == K_TRADE) & (rows.side[a:b] == g.side) & (rows.price[a:b] == g.price)
    prints = float(rows.trade_qty[a:b][hit].sum())
    mine = float(sum(f[1] for f, w in zip(g.fills, where, strict=True) if w == bi))
    return bi, prints, mine


def _flows(
    g: Leg, rows: L2Arrays, starts: np.ndarray, from_seq: int, to_bi: int, monotone: bool | None = None
) -> tuple[float, float]:
    """(at-price prints, level decreases) at the leg's level over batches with seq >= `from_seq` before batch
    `to_bi`. Rows carry their batch's `sequence`, which never decreases within an instrument (`monotone`, checked
    here when not given), so the scan starts at the first row of `from_seq`."""
    end = int(starts[to_bi])
    seq = rows.sequence
    mono = _monotone(seq) if monotone is None else monotone
    lo = int(np.searchsorted(seq[:end], from_seq, side="left")) if mono else 0
    sel = np.arange(lo, end)
    sel = sel[seq[lo:end] >= from_seq]
    lvl = (rows.side[sel] == g.side) & (rows.price[sel] == g.price)
    prints = float(rows.trade_qty[sel][lvl & (rows.kind[sel] == K_TRADE)].sum())
    dep = lvl & (rows.kind[sel] == K_DEPTH)
    dec = np.maximum(rows.prev_qty[sel][dep] - rows.new_qty[sel][dep], 0)
    return prints, float(dec.sum())


def _monotone(a: np.ndarray) -> bool:
    return bool(a.size < 2 or np.all(a[1:] >= a[:-1]))


def own_fills_between(i: int, all_legs: list[Leg], t0: int, t1: int) -> int:
    """Maker fills of the user's other legs at leg `i`'s level with `t0 <= ts < t1` (they are in the prints)."""
    return LevelIndex(all_legs).fills_between(i, t0, t1)


class _ErrorTable:
    """Signed errors of every column (the models, then the bounds midpoint) for a set of observations, each tied to
    one leg. The losses are order-weighted: each leg's errors are reduced first (mean absolute or mean squared), then
    averaged over legs, so long-lived orders with many samples do not dominate."""

    def __init__(self, legs: list[int], rows: list[np.ndarray], n_cols: int) -> None:
        self.legs = np.asarray(legs, dtype=np.int64)
        self.err = np.asarray(rows, dtype=float).reshape(len(rows), n_cols)
        self.leg_ids, inv = np.unique(self.legs, return_inverse=True)
        self.counts = np.bincount(inv, minlength=self.leg_ids.size)
        abs_sum = np.zeros((self.leg_ids.size, n_cols))
        sq_sum = np.zeros((self.leg_ids.size, n_cols))
        np.add.at(abs_sum, inv, np.abs(self.err))
        np.add.at(sq_sum, inv, np.square(self.err))
        n = np.maximum(self.counts, 1)[:, None]
        self.per_leg = {"mae": abs_sum / n, "rmse": sq_sum / n}  # per-leg MAE and per-leg mean squared error

    def __len__(self) -> int:
        return int(self.legs.size)

    def leg_rows(self, keys: list[int]) -> np.ndarray:
        """Positions in `leg_ids` of the legs of `keys` that have observations (ascending leg index)."""
        return np.flatnonzero(np.isin(self.leg_ids, np.asarray(keys, dtype=np.int64)))

    def loss(self, keys: list[int], kind: str) -> np.ndarray | None:
        """Order-weighted `mae` (mean of per-leg MAE) or `rmse` (root of the mean of per-leg mean squared errors)
        per column over the legs `keys` (None when none has observations)."""
        rows = self.leg_rows(keys)
        if not rows.size:
            return None
        m = self.per_leg[kind][rows].mean(axis=0)
        return np.sqrt(m) if kind == "rmse" else m

    def resampled(self, keys: list[int], kind: str, col: int, draws: np.ndarray) -> np.ndarray:
        """The loss of column `col` over each row of `draws` (indices into the legs of `keys` with observations)."""
        vals = self.per_leg[kind][self.leg_rows(keys), col][draws].mean(axis=1)
        return np.sqrt(vals) if kind == "rmse" else vals


def calibrate(spec: PmCalibrationSpec | dict[str, Any]) -> PmCalibration:
    """Runs `pm calibrate` (see the module docstring)."""
    sp = validate_spec(PmCalibrationSpec, spec)
    if not sp.out:
        raise QtError("QT-CONFIG-001", "out: an output directory is required")
    d = load(sp.pm.path)
    all_legs = legs(d)
    index = LevelIndex(all_legs)
    if not all_legs:
        raise QtError(
            "QT-INPUT-002",
            f"{d.root}: no own orders with a queue entry point (calibration needs your own orders and fills)",
            "ingest your own order and fill messages (Kalshi user_orders + fill channels, Polymarket user channel)",
        )
    models = models_for(sp)
    eng = Engine(models)
    warnings: list[str] = []
    by_key: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(all_legs):
        by_key[g.order_key].append(i)
    samples: list[_Sample] = []
    times: dict[int, list[int]] = defaultdict(list)
    unplaced = 0
    for q in d.samples:
        ok = q.get("order_key")
        if not ok or ok not in by_key:
            unplaced += 1
            continue
        recv = int(q["ts_local_recv"])
        send = int(q["ts_local_send"]) if q.get("ts_local_send") is not None else None
        t_align = (send + recv) // 2 if (sp.sample_time == "mid" and send is not None) else recv
        cand = [i for i in by_key[ok] if all_legs[i].entry_ts <= t_align] or by_key[ok][:1]
        li = max(cand, key=lambda i: (all_legs[i].entry_ts, i))
        samples.append(_Sample(li, int(q["queue_position"]), t_align, send, recv, index.own_ahead(li, t_align)))
        times[li] += [t_align, recv] + ([send] if send is not None else [])
    if unplaced:
        warnings.append(f"queue samples: {unplaced} sample(s) of orders without a queue entry point were skipped")
    window = int(d.summary.get("spec", {}).get("own_match_window_ns") or DEFAULT_FILL_WINDOW_NS[d.venue])
    exps = {int(m["size_exp"]) for m in d.markets}
    if len(exps) != 1:
        raise QtError("QT-INPUT-002", f"{d.root}: instruments with different size exponents {sorted(exps)}")
    unit = 10.0 ** exps.pop()
    estimates: dict[int, dict[int, np.ndarray]] = {}
    filled_by: dict[int, np.ndarray] = {}  # leg -> per model: did the replay fill it
    probes: list[_Probe] = []
    full_first = 0
    chain_truth = _chain_truth(d, all_legs)
    chain_lost = 0
    for iid in d.instrument_ids():
        mine = [i for i, g in enumerate(all_legs) if g.instrument_id == iid]
        if not mine:
            continue
        rows = d.book(iid)
        starts = np.asarray(_batch_starts(rows) if len(rows) else [], dtype=np.int64)
        seqs = np.array([int(rows.sequence[b - 1]) for b in [*starts[1:].tolist(), len(rows)]], dtype=np.int64)
        index_of = {int(q): b for b, q in enumerate(seqs.tolist())}
        trades = _trades_by_level(rows)
        mono = _monotone(rows.sequence)
        # k -> (batch index, at-price prints, the leg's fills in it, chain truth or None)
        fb: dict[int, tuple[int, float, float, float | None]] = {}
        sim = []
        for k, i in enumerate(mine):
            g = all_legs[i]
            pr = []
            if i in chain_truth:
                seq, ahead = chain_truth[i]
                if seq in index_of:
                    fb[k] = (index_of[seq], 0.0, 0.0, ahead)
                    pr = [seq]
                else:
                    chain_lost += 1
            elif len(rows):
                hit = _fill_batch(g, rows, starts, window, trades)
                if hit is not None:
                    fb[k] = (*hit, None)
                    pr = [int(seqs[hit[0]])]
            sim.append(
                SimOrder(
                    g.key,
                    g.side,
                    g.price,
                    float(g.qty),
                    entry_seq=g.entry_seq,
                    inside=g.inside,
                    **g.sim_end(),
                    post_only=g.post_only,
                    amends=[(t, float(q)) for t, q in g.amends],
                    samples=sorted(set(times.get(i, []))),
                    probes=pr,
                )
            )
        res = eng.run(rows, sim, gaps=d.gaps(iid))
        for k, i in enumerate(mine):
            r = res[k]
            estimates[i] = dict(r.samples)
            filled_by[i] = np.array([bool(f) for f in r.fills], dtype=bool)
            if k not in fb or not r.probes or r.activated_seq is None:
                continue
            g = all_legs[i]
            bi, prints, fill, ahead = fb[k]
            t_first = g.fills[0][0] if g.fills else int(rows.ts_event[starts[bi]])
            if ahead is not None:
                f_true, source = ahead, "chain"
            else:
                if fill >= resting_at(g, t_first - 1):
                    full_first += 1
                    continue
                f_true, source = prints - fill - index.own_ahead(i, t_first - 1), "l2"
            v, dec = _flows(g, rows, starts, r.activated_seq, bi, mono)
            v -= index.fills_between(i, g.entry_ts, int(rows.ts_event[starts[bi]]))
            q0 = float(r.visible_at_live or 0.0)
            probes.append(_Probe(i, f_true, r.probes[0][1], q0, v, dec - v, source))
    if full_first:
        warnings.append(f"fill truth: {full_first} order(s) were completed by their first filling batch (bound only)")
    if chain_lost:
        warnings.append(f"fill truth: {chain_lost} on-chain queue truth row(s) name a batch not in the book (skipped)")
    # ---- errors --------------------------------------------------------------------------------------------------
    ids = [m.id for m in models]
    cols = [*ids, MIDPOINT]

    def with_midpoint(f: np.ndarray) -> np.ndarray:
        return np.append(f, (f[0] + f[1]) / 2.0)

    s_legs: list[int] = []
    s_rows: list[np.ndarray] = []
    covered = n_cov = 0
    for s in samples:
        est = estimates.get(s.leg, {})
        if s.t_align not in est:
            continue
        f = est[s.t_align] + s.own_ahead
        s_legs.append(s.leg)
        s_rows.append((with_midpoint(f) - s.official) * unit)
        pts = [est[t] for t in (s.t_send, s.t_recv, s.t_align) if t is not None and t in est]
        lo = min(float(p[0]) for p in pts) + s.own_ahead
        hi = max(float(p[1]) for p in pts) + s.own_ahead
        n_cov += 1
        covered += int(lo - 1e-9 <= s.official <= hi + 1e-9)
    errs = _ErrorTable(s_legs, s_rows, len(cols))
    f_legs: list[int] = []
    f_rows: list[np.ndarray] = []
    viol = 0
    for p in probes:
        if p.f_true < -1e-9:
            warnings.append(f"fill truth: order {all_legs[p.leg].key}: its fill exceeds its batch's prints (skipped)")
            continue
        f_legs.append(p.leg)
        f_rows.append((with_midpoint(p.f_model) - p.f_true) * unit)
        viol += int(not (p.f_model[0] - 1e-9 <= p.f_true <= p.f_model[1] + 1e-9))
    ferr = _ErrorTable(f_legs, f_rows, len(cols))
    good = [p for p in probes if p.f_true >= -1e-9]
    # ---- split, fit and report -----------------------------------------------------------------------------------
    first_entry = {k: min(all_legs[i].entry_ts for i in v) for k, v in by_key.items()}
    order_keys = sorted(by_key, key=lambda k: (first_entry[k], k))
    n_train = max(1, math.floor(len(order_keys) * sp.train_fraction)) if len(order_keys) > 1 else 1
    train_keys, test_keys = set(order_keys[:n_train]), set(order_keys[n_train:])
    test_from = (first_entry[order_keys[n_train]], order_keys[n_train]) if test_keys else None
    train = [i for i, g in enumerate(all_legs) if g.order_key in train_keys]
    test = [i for i, g in enumerate(all_legs) if g.order_key in test_keys]
    use_samples = len(errs) > 0
    if not use_samples and not good:
        raise QtError(
            "QT-INPUT-002",
            "nothing to calibrate on: no official queue position could be placed on the book and no own maker fill "
            "shows the queue ahead in its batch",
            "capture queue positions (Kalshi) with their receive times, or include your own fills and the market "
            "channel's prints",
        )
    target = errs if use_samples else ferr
    with_truth = {all_legs[int(i)].order_key for i in target.leg_ids.tolist()}
    n_train_truth, n_test_truth = len(with_truth & train_keys), len(with_truth & test_keys)
    if not test:
        warnings.append("calibration: a single own order; train and test are the same order")
        test = train
    unfilled = [i for i in test if not all_legs[i].fills and i in filled_by] if test_keys else []
    fits = _fits(cols, models, target, ferr, train, test, sp, filled_by, unfilled)
    candidates = [f for f in fits if f.train_loss is not None and f.model_id != MIDPOINT]
    best = min(candidates, key=lambda f: (f.train_loss, f.model_id)) if candidates else None
    mid_fit = next(f for f in fits if f.model_id == MIDPOINT)
    beats = None
    if best is not None and best.test_loss is not None and mid_fit.test_loss is not None and test_keys:
        beats = best.test_loss < mid_fit.test_loss
    fit_on = "queue_samples" if use_samples else "fill_truth"
    if best is None:
        warnings.append(
            "calibration: nothing fitted: no training order has queue truth (every order with truth is a test "
            "order); add captures, capture queue positions (Kalshi) or a chain export (Polymarket)"
        )
    if fit_on == "fill_truth":
        warnings.append(
            "fill truth: the fit uses only orders that filled, and an order fills when more of the queue ahead than "
            "usual left, so this truth favours short queues (fills that come early); unfilled_filled checks each "
            "model on the unfilled test orders. A fit on every order, filled or not: queuetruth calibrate-live "
            "--data <pm dir>/book --live <pm dir>/live --family power3 --mode corrected"
        )
    trust, trust_lines = _trust(sp, best, beats, mid_fit.test_loss, n_train_truth, n_test_truth, fit_on)
    warnings += trust_lines
    shown = _summarize(fits, best)
    cancels = sum(p.cancels for p in good)
    fill_truth = FillTruth(
        n_orders=len(good),
        n_chain=sum(1 for p in good if p.source == "chain"),
        cancel_ahead_true=float(np.mean([p.q0 - p.prints_before - p.f_true for p in good])) * unit if good else None,
        by_model={
            f.model_id: float(np.mean([p.q0 - p.prints_before - p.f_model[ids.index(f.model_id)] for p in good])) * unit
            for f in shown
            if good and f.model_id in ids
        },
        fraction_ahead_true=(sum(p.q0 - p.prints_before - p.f_true for p in good) / cancels) if cancels > 0 else None,
        bound_violations=viol,
    )
    out = Path(sp.out)
    out.mkdir(parents=True, exist_ok=True)
    spec_doc = sp.model_dump(mode="json")
    doc = PmCalibration(
        run_id=run_id(spec_doc, __version__, resolve_ref),
        spec=spec_echo(spec_doc),
        venue=d.venue,
        terms=TERMS_NOTICE,
        pm_run_id=str(d.summary["run_id"]),
        fit_on=fit_on,
        loss=sp.loss,
        units="contracts" if d.venue == "kalshi" else "shares",
        n_orders=len(order_keys),
        n_samples=len(errs),
        n_train_orders=len(train_keys),
        n_test_orders=len(test_keys) if test_keys else len(train_keys),
        n_train_orders_with_truth=n_train_truth,
        n_test_orders_with_truth=n_test_truth,
        test_from=test_from,
        n_unfilled_test_orders=len(unfilled),
        bounds_coverage=(covered / n_cov) if n_cov else None,
        best=best.model_id if best else None,
        best_beats_bounds_midpoint=beats,
        trust=trust,
        models=shown,
        fill_truth=fill_truth,
        warnings=warnings,
        files=[],
    )
    path = out / "pm_calibration.json"
    path.write_text(json.dumps(doc.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return doc


def _trust(
    sp: PmCalibrationSpec,
    best: ModelFit | None,
    beats: bool | None,
    mid_loss: float | None,
    n_train: int,
    n_test: int,
    fit_on: str,
) -> tuple[PmCalibrationTrust, list[str]]:
    """The verdict (see the module docstring) and its `trust:` warning lines."""
    texts: dict[str, str] = {}
    if best is None:
        texts["no_estimate"] = "nothing was fitted (no training order has queue truth)"
    if min(n_train, n_test) < MIN_ORDERS:
        texts["few_orders"] = (
            f"{n_train} train and {n_test} test order(s) have queue truth; a usable fit needs at least {MIN_ORDERS} "
            "of each"
        )
    if best is not None and n_test == 0:
        texts["no_held_out"] = "no test order has queue truth: the fit is not checked out of sample"
    if best is not None and best.family in PARAM_FAMILIES and best.n is not None:
        ln_n = math.log(best.n)
        for end, edge in (("lower", sp.grid_lo), ("upper", sp.grid_hi)):
            if abs(ln_n - edge) < 1e-5:
                texts["at_grid_edge"] = (
                    f"{best.model_id} is at the {end} end of the grid (ln n = {ln_n:g} in [{sp.grid_lo:g}, "
                    f"{sp.grid_hi:g}]): the data may prefer a value outside it; widen --grid"
                )
    if best is not None and best.family in (LO, HI):
        texts["best_is_bound"] = f"the best fit is the queue bound {best.model_id}, not a model between the bounds"
    if beats is False and best is not None and best.test_loss is not None and mid_loss is not None:
        texts["not_beating_midpoint"] = (
            f"it does not beat the midpoint of the two queue bounds on the test orders ({sp.loss} {best.test_loss:.4g} "
            f"against {mid_loss:.4g})"
        )
    reasons = [t for t in PM_REFUSING if t in texts]
    flags = sorted({*texts, *(["fill_truth_selection"] if fit_on == "fill_truth" else [])})
    verdict = "refused" if reasons else "usable"
    lines = [f"trust: {t}: {texts[t]}" for t in reasons]
    lines.append(f"trust: verdict: {verdict}" + (f" ({', '.join(reasons)})" if reasons else ""))
    return PmCalibrationTrust(verdict=verdict, reasons=reasons, flags=flags), lines


def _chain_truth(d: PmDir, all_legs: list[Leg]) -> dict[int, tuple[int, float]]:
    """Leg index -> (published batch seq, other traders' queue ahead) from `qt.pm_chain_queue`: the order's first
    leg with a maker fill."""
    out: dict[int, tuple[int, float]] = {}
    rows = {str(r["order_key"]): r for r in d.chain_queue}
    done: set[str] = set()
    for i, g in enumerate(all_legs):
        r = rows.get(g.order_key)
        if r is None or not g.fills or g.order_key in done:
            continue
        done.add(g.order_key)
        out[i] = (int(r["seq"]), float(r["ahead_others"]))
    return out


def _fits(
    cols: list[str],
    models: list[Model],
    e: _ErrorTable,
    fe: _ErrorTable,
    train: list[int],
    test: list[int],
    sp: PmCalibrationSpec,
    filled_by: dict[int, np.ndarray],
    unfilled: list[int],
) -> list[ModelFit]:
    """One `ModelFit` per column: order-weighted train and test loss (`sp.loss`) with a percentile bootstrap
    interval of the test loss over test orders (one stream per model id), the order-weighted test MAE and RMSE, the
    bias and median absolute error of the test samples, the errors at the first filling batch, and how many of the
    `unfilled` test legs (never filled live) the model's replay filled (`filled_by`: leg -> per model)."""
    train_loss, test_loss = e.loss(train, sp.loss), e.loss(test, sp.loss)
    test_mae, test_rmse = e.loss(test, "mae"), e.loss(test, "rmse")
    ft_mae = fe.loss(list(fe.leg_ids), "mae")
    test_rows = e.leg_rows(test)
    test_obs = np.isin(e.legs, e.leg_ids[test_rows])
    flat = e.err[test_obs]
    n_train_samples = int(e.counts[e.leg_rows(train)].sum())
    by_id = {m.id: m for m in models}
    false_fills = np.sum([filled_by[i] for i in unfilled], axis=0) if unfilled else np.zeros(len(models), dtype=int)

    def opt(v: np.ndarray | None, c: int) -> float | None:
        return None if v is None else float(v[c])

    out = []
    for c, mid in enumerate(cols):
        ci: tuple[float | None, float | None] = (None, None)
        if test_rows.size >= 2 and sp.bootstrap_b > 0:
            rng = bootstrap_rng(sp.seed, f"pm_calibrate:{mid}")
            draws = rng.integers(0, test_rows.size, size=(sp.bootstrap_b, test_rows.size))
            ci = percentile_ci(e.resampled(test, sp.loss, c, draws))
        model = by_id.get(mid)
        col = flat[:, c]
        f_col = fe.err[:, c]
        out.append(
            ModelFit(
                model_id=mid,
                family=model.family if model else MIDPOINT,
                n=model.n if model else None,
                atomic=model.atomic if model else False,
                zoo_id=model.zoo_id if model else None,
                train_loss=opt(train_loss, c),
                test_loss=opt(test_loss, c),
                test_loss_ci=ci,
                test_mae=opt(test_mae, c),
                test_rmse=opt(test_rmse, c),
                test_bias=float(np.mean(col)) if col.size else None,
                test_median_abs=float(np.median(np.abs(col))) if col.size else None,
                n_train_samples=n_train_samples,
                n_test_samples=int(col.size),
                fill_truth_mae=opt(ft_mae, c),
                fill_truth_bias=float(np.mean(f_col)) if f_col.size else None,
                n_fill_truth=int(fe.leg_ids.size),
                unfilled_filled=int(false_fills[c]) if model else None,
            )
        )
    return out


def _summarize(fits: list[ModelFit], best: ModelFit | None) -> list[ModelFit]:
    """Per family (and variant) its grid point with the smallest train loss (ties: model id), sorted by train loss;
    the overall best first."""
    inf = math.inf
    by_family: dict[tuple[str, bool], ModelFit] = {}
    for f in fits:
        cur = by_family.get((f.family, f.atomic))
        key = (f.train_loss if f.train_loss is not None else inf, f.model_id)
        if cur is None or key < (cur.train_loss if cur.train_loss is not None else inf, cur.model_id):
            by_family[(f.family, f.atomic)] = f
    out = sorted(by_family.values(), key=lambda f: (f.train_loss if f.train_loss is not None else inf, f.model_id))
    if best is not None:
        out = [best] + [f for f in out if f.model_id != best.model_id]
    return out


__all__ = ["LevelIndex", "calibrate", "models_for", "own_ahead", "own_fills_between", "resting_at"]
