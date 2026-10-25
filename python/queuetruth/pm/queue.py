"""Queue-position models on a prediction-market L2 series, run in one pass over the batches.

The engine replays one instrument's `qt.l2` rows (normally `book_own_free/`) and moves every live order's queue
estimate under several models at once (vectorized over models). Its dispatch is the corrected mode of the Rust zoo
(SPEC 5.3.2 and 5.3.3, `crates/zoo/src/sim.rs` and `pair.rs`), so the same orders on the same dataset give the same
fills as `queuetruth zoo --mode corrected` (tested against the compiled core):

- Activation: a time-entered order goes live at the first batch with `ts_event > ts_live`, with `F` = the visible
  size at its price before that batch; an order with a known entry point (`seq`, `inside`) goes live at the first
  batch after it. On arrival into a book that crosses its price an order fills as a taker up to the visible opposite
  size at or through its price (`front_of_queue` and the queue families; never `trade_through_only`); a post-only
  order is rejected instead.
- Rows in batch order: a trade at the order's price hitting its side (`F -= v`, then a fill of whatever volume went
  beyond the queue, in whole lots), a trade through its price (fills the leaves), a depth row at its level (the
  family's update), a `clear` (stops the order, censored). At the end of every batch that touched the order, a book
  that crosses its price fills it as a maker for at most the visible opposite size not yet taken; the trade netting
  `T` is reset. A cancel or the end of the order's life stops it at the first batch after its time, or after its
  placed position (`end_seq`) when the user's own log placed it in the published series.
- Families (corrected mode): `front_of_queue` (no queue ahead; fills on every at-price print; the optimistic fill
  bound), `trade_through_only` (fills only on a trade through its price; the pessimistic fill bound),
  `risk_adverse` (= `hbt_risk_adverse`: only prints shrink the queue; the upper queue bound), `cancels_ahead` (every
  decrease beyond prints leaves the queue ahead first; the lower queue bound) and the hbt probability families
  `power[n]`, `power2[n]`, `power3[n]`, `log`, `log2` (`pi` = probability that a decrease is behind the order).
  `power3[n]` spans the bounds: `n -> 0` is `cancels_ahead`, `n -> infinity` is `risk_adverse`.
- `atomic` variants (`power3[n=1;atomic]`, `log[atomic]`, ...) read a decrease beyond the prints as one order
  leaving, the reading of a feed that publishes every exchange action on its own (Kalshi: deltas that share a
  `clock` are one action): the order could only have been ahead if its size fits in the queue ahead, and only
  behind if it fits in the size behind. When exactly one of the two is possible the decrease is placed there;
  otherwise the family splits it as usual. A change that nets several orders breaks the reading, so the variants
  are estimates to calibrate, not bounds.

The queue bounds hold on an unconflated FIFO feed: every visible change is one order's action, prints consume the
level from the front, adds join behind. A change that nets an add against a cancel ahead can push the true queue
below `cancels_ahead`; Kalshi's official queue positions measure how often (`pm calibrate` bound coverage).

Day-boundary restatements (a `resync` identical to the book) are skipped, so orders live across UTC midnight; a
resync that changes the book (after a feed gap) stops every live order, as do the feed gaps of `data_quality.json`.
"""

from __future__ import annotations

import heapq
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from queuetruth.errors import QtError
from queuetruth.pm.book import ASK, BID, Ladder
from queuetruth.pm.dataset import KINDS, L2Arrays

EPS = 1e-9
K_DEPTH, K_TRADE, K_SNAPSHOT, K_CLEAR = (KINDS.index(k) for k in ("depth", "trade", "snapshot", "clear"))
PROB_FAMILIES = ("power", "power2", "power3", "log", "log2", "cancels_ahead")
#: Families that take the one-order-per-change variant (`atomic`): the hbt probability families, not the bounds.
ATOMIC_FAMILIES = ("power", "power2", "power3", "log", "log2")
BOUND_FAMILIES = ("front_of_queue", "trade_through_only")
QUEUE_FAMILIES = ("risk_adverse", *PROB_FAMILIES)
ALL_FAMILIES = (*BOUND_FAMILIES, *QUEUE_FAMILIES)
_ID = re.compile(r"^([a-z_0-9]+?)(?:\[(.*)\])?$")


@dataclass(frozen=True)
class Model:
    """One model: family, parameter `n` (power families) and the one-order-per-change variant (`atomic`)."""

    family: str
    n: float | None = None
    atomic: bool = False

    def __post_init__(self) -> None:
        if self.family not in ALL_FAMILIES:
            raise QtError("QT-CONFIG-001", f"model family {self.family!r}: expected one of {list(ALL_FAMILIES)}")
        needs_n = self.family in ("power", "power2", "power3")
        if needs_n and (self.n is None or not math.isfinite(self.n) or self.n <= 0):
            raise QtError("QT-CONFIG-001", f"model {self.family} needs a positive finite parameter n")
        if not needs_n and self.n is not None:
            raise QtError("QT-CONFIG-001", f"model {self.family} takes no parameter")
        if self.atomic and self.family not in ATOMIC_FAMILIES:
            raise QtError("QT-CONFIG-001", f"model {self.family}: only {list(ATOMIC_FAMILIES)} take `atomic`")

    @property
    def id(self) -> str:
        """Model id (`power3[n=1.5]`, `power3[n=1.5;atomic]`, `log[atomic]`, `risk_adverse`, ...)."""
        parts = [] if self.n is None else [f"n={_num(self.n)}"]
        if self.atomic:
            parts.append("atomic")
        return f"{self.family}[{';'.join(parts)}]" if parts else self.family

    @property
    def zoo_id(self) -> str | None:
        """The same model in the zoo's canonical spelling (`hbt_power3[exch=partial;mode=corrected;n=1.5]`), which
        `queuetruth zoo --models` and `calibrate-live` take; None when the zoo has no such model (`cancels_ahead`,
        the atomic variants)."""
        if self.atomic or self.family == "cancels_ahead":
            return None
        if self.family in BOUND_FAMILIES:
            return f"{self.family}[mode=corrected]"
        keys = ["exch=partial", "mode=corrected"] + ([] if self.n is None else [f"n={_num(self.n)}"])
        return f"hbt_{self.family}[{';'.join(keys)}]"

    @classmethod
    def parse(cls, text: str) -> Model:
        """`power3[n=2]`, `power3[n=2;atomic]`, `hbt_power3[exch=partial;mode=corrected;n=2]` (the zoo spelling),
        `risk_adverse`, ... The engine runs the zoo's corrected mode with partial fills: another `mode` or `exch` is
        `QT-CONFIG-001`."""
        m = _ID.match(text.strip())
        if not m:
            raise QtError("QT-CONFIG-001", f"model id {text!r} is not family[key=value;...]")
        fam = m.group(1)
        if fam.startswith("hbt_"):
            fam = fam[4:]
        params = {}
        for kv in (m.group(2) or "").split(";"):
            if not kv.strip():
                continue
            k, _, v = kv.partition("=")
            params[k.strip()] = v.strip()
        mode = params.pop("mode", "corrected")
        exch = params.pop("exch", None)
        if mode != "corrected":
            raise QtError("QT-CONFIG-001", f"model {text!r}: the prediction-market engine runs corrected mode only")
        if exch is not None and (fam in (*BOUND_FAMILIES, "cancels_ahead") or exch != "partial"):
            raise QtError(
                "QT-CONFIG-001",
                f"model {text!r}: exch={exch} is not supported (the engine fills partially, the zoo's exch=partial, "
                "which only the hbt families name)",
            )
        n = params.pop("n", None)
        atomic = params.pop("atomic", None)
        if atomic not in (None, "", "true", "1"):
            raise QtError("QT-CONFIG-001", f"model {text!r}: atomic takes no value (or true)")
        if params:
            raise QtError("QT-CONFIG-001", f"model {text!r}: unknown parameters {sorted(params)}")
        try:
            return cls(fam, float(n) if n is not None else None, atomic is not None)
        except ValueError:
            raise QtError("QT-CONFIG-001", f"model {text!r}: n must be a number") from None


@dataclass
class SimOrder:
    """One order to simulate. Entry by time (`ts_live`, Rust-compatible) or by position (`entry_seq`, `inside`:
    joined during that published batch, else just before it). `amends` are priority-keeping reductions `(ts, new
    total qty)`; `ts_end` stops the order at the first batch after it, or, with `end_seq`, at the first batch after
    that position (`end_inside`: it left during batch `end_seq`, else just before it), `ts_end` then being the
    reported end time."""

    key: str
    side: int
    price: int
    qty: float
    ts_live: int | None = None
    entry_seq: int | None = None
    inside: bool = False
    ts_end: int | None = None
    end_seq: int | None = None
    end_inside: bool = False
    post_only: bool = False
    amends: list[tuple[int, float]] = field(default_factory=list)
    samples: list[int] = field(default_factory=list)  # local times at which to record the queue estimate
    probes: list[int] = field(default_factory=list)  # published batch seqs: record the estimate just before them


@dataclass
class OrderResult:
    """Per order: fills per model `(ts, qty, reason, liquidity, price)`, end state, activation, recorded estimates."""

    key: str
    activated_ts: int | None = None
    activated_seq: int | None = None
    visible_at_live: float | None = None
    end: str = "pending"
    end_ts: int | None = None
    fills: list[list[tuple[int, float, str, str, float]]] = field(default_factory=list)
    samples: list[tuple[int, np.ndarray]] = field(default_factory=list)  # (local time, F per model)
    probes: list[tuple[int, np.ndarray]] = field(default_factory=list)  # (seq, F per model before that batch)
    samples_before_live: int = 0
    samples_after_end: int = 0


class _Live:
    __slots__ = ("F", "T", "done", "filled", "i", "leaves", "o", "pi", "res", "si", "taker_taken", "touched")

    def __init__(self, i: int, o: SimOrder, res: OrderResult, k: int) -> None:
        self.i, self.o, self.res = i, o, res
        self.F = np.zeros(k)
        self.T = np.zeros(k)
        self.leaves = np.full(k, float(o.qty))
        self.filled = np.zeros(k)
        self.done = np.zeros(k, dtype=bool)
        self.taker_taken = np.zeros(k)
        self.touched = False
        self.si = 0
        self.pi = 0


class Engine:
    """Vectorized corrected-mode queue models over one instrument's L2 rows."""

    def __init__(self, models: Sequence[Model], lot: float = 1.0) -> None:
        if not models:
            raise QtError("QT-CONFIG-001", "no models to run")
        self.models = list(models)
        self.k = len(self.models)
        self.lot = float(lot)
        fam = [m.family for m in self.models]
        self.is_foq = np.array([f == "front_of_queue" for f in fam])
        self.is_tt = np.array([f == "trade_through_only" for f in fam])
        self.is_ra = np.array([f == "risk_adverse" for f in fam])
        self.is_prob = np.array([f in PROB_FAMILIES for f in fam])
        self.is_queue = self.is_ra | self.is_prob
        self.is_atomic = np.array([m.atomic for m in self.models])
        self.groups: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for f in PROB_FAMILIES:
            idx = np.array([i for i, m in enumerate(self.models) if m.family == f], dtype=np.int64)
            if idx.size:
                n = np.array([self.models[i].n if self.models[i].n is not None else np.nan for i in idx])
                self.groups[f] = (idx, n)

    # ---- model updates (corrected mode) -------------------------------------------------------------------------

    def _prob(self, front: np.ndarray, back: np.ndarray) -> np.ndarray:
        pi = np.zeros(self.k)
        with np.errstate(all="ignore"):
            for f, (idx, n) in self.groups.items():
                fr, bk = front[idx], back[idx]
                if f == "power":
                    v = bk**n / (bk**n + fr**n)
                elif f == "power2":
                    v = bk**n / (bk + fr) ** n
                elif f == "power3":
                    v = 1.0 - (fr / (fr + bk)) ** n
                elif f == "log":
                    v = np.log1p(bk) / (np.log1p(bk) + np.log1p(fr))
                elif f == "log2":
                    v = np.log1p(bk) / np.log1p(bk + fr)
                else:  # cancels_ahead
                    v = np.zeros(idx.size)
                pi[idx] = v
        return pi

    def _depth(self, s: _Live, prev: float, new: float) -> None:
        act = ~s.done
        ra = act & self.is_ra
        if ra.any():
            s.F[ra] = np.minimum(np.maximum(s.F[ra], 0.0), new)
        pm = act & self.is_prob
        if pm.any():
            chg = prev - new - s.T
            s.T[pm] = 0.0
            inc = pm & (chg < 0.0)
            s.F[inc] = np.minimum(np.maximum(s.F[inc], 0.0), new)
            dec = pm & ~(chg < 0.0)
            if dec.any():
                front = np.maximum(s.F, 0.0)
                zero = dec & (front == 0.0)
                s.F[zero] = np.minimum(front[zero], new)
                rest = dec & ~(front == 0.0)
                if rest.any():
                    back = np.maximum(prev - front, 0.0)
                    pi = self._prob(front, back)
                    with np.errstate(all="ignore"):
                        alt = back / (front + back)
                    pi = np.where(np.isfinite(pi), pi, alt)
                    pi = np.where(back == 0.0, 0.0, np.clip(pi, 0.0, 1.0))
                    if self.is_atomic.any():
                        # one order left: it was ahead only if it fits in front, behind only if it fits behind
                        fits_ahead, fits_behind = chg <= front, chg <= back
                        pi = np.where(self.is_atomic & fits_ahead & ~fits_behind, 0.0, pi)
                        pi = np.where(self.is_atomic & fits_behind & ~fits_ahead, 1.0, pi)
                    est = front - (1.0 - pi) * chg + np.minimum(back - pi * chg, 0.0)
                    s.F[rest] = np.minimum(np.maximum(est[rest], 0.0), new)
        tt = act & self.is_tt
        if tt.any():
            s.F[tt] = np.minimum(s.F[tt], new)

    def _fill(
        self,
        s: _Live,
        ts: int,
        q: np.ndarray,
        reason: str,
        liquidity: str,
        price: Callable[[int, float], float] | None = None,
    ) -> None:
        """Fills `q` (per model, capped at the leaves) at the order's price, or at `price(model, qty)` (taker)."""
        q = np.minimum(np.where(s.done, 0.0, q), s.leaves)
        for m in np.flatnonzero(q > 0.0):
            px = float(s.o.price) if price is None else price(int(m), float(q[m]))
            s.leaves[m] -= q[m]
            s.filled[m] += q[m]
            s.res.fills[m].append((ts, float(q[m]), reason, liquidity, px))
            if s.leaves[m] <= EPS * self.lot:
                s.leaves[m] = max(s.leaves[m], 0.0)
                s.done[m] = True

    def _trade(self, s: _Live, ts: int, v: float) -> None:
        act = ~s.done
        q = np.zeros(self.k)
        foq = act & self.is_foq
        if foq.any():
            q[foq] = math.floor(v / self.lot) * self.lot
        qm = act & self.is_queue
        if qm.any():
            s.F[qm] -= v
            pm = qm & self.is_prob
            s.T[pm] += v
            lots = np.floor(np.maximum(-s.F, 0.0) / self.lot + 1e-9)
            hit = qm & (lots > 0)
            s.F[hit] = 0.0
            q[hit] = lots[hit] * self.lot
        self._fill(s, ts, q, "queue", "maker")

    def _through(self, s: _Live, ts: int) -> None:
        self._fill(s, ts, np.where(~s.done, s.leaves, 0.0), "trade_through", "maker")

    def _cross(self, s: _Live, ts: int, levels: list[tuple[int, int]], reason: str, liquidity: str) -> None:
        """Taker cap of the corrected mode: a cross fills at most the visible opposite size at or through the price
        (`levels`, best first) not yet taken. A taker fill (`crossed_at_live`) is priced at the volume-weighted price
        of the levels it walks past what the order already took; a maker fill (`crossed`) at the order's price."""
        able = ~s.done & ~self.is_tt
        if not able.any():
            return
        opp = float(sum(q for _p, q in levels))
        s.taker_taken = np.minimum(s.taker_taken, max(opp, 0.0))
        avail = np.maximum(opp - s.taker_taken, 0.0)
        q = np.where(able & (avail > 0), np.minimum(s.leaves, avail), 0.0)
        before = s.filled.copy()
        taken = s.taker_taken.copy()

        def vwap(m: int, qty: float) -> float:
            return _walk(levels, float(taken[m]), qty)

        self._fill(s, ts, q, reason, liquidity, vwap if liquidity == "taker" else None)
        s.taker_taken += np.minimum(np.maximum(s.filled - before, 0.0), avail)

    # ---- the pass ---------------------------------------------------------------------------------------------------

    def run(
        self,
        rows: L2Arrays,
        orders: Sequence[SimOrder],
        *,
        gaps: Sequence[tuple[int, int]] = (),
        end_ts: int | None = None,
    ) -> list[OrderResult]:
        """Simulates `orders` over `rows` (one instrument) and returns one result per order (same order).

        Queue estimates are recorded at each order's `samples` (local times, sorted): a sample sees the state after
        every batch whose running maximum `ts_recv` is at or before it. Samples before the order went live in the
        replay are counted in `samples_before_live`, those after it ended in `samples_after_end`."""
        n = len(rows)
        results = [OrderResult(o.key, fills=[[] for _ in range(self.k)]) for o in orders]
        if n == 0:
            for r in results:
                r.end = "no_data"
            return results
        starts = _batch_starts(rows)
        bounds = list(zip(starts, [*starts[1:], n], strict=True))
        b_ts = np.array([int(rows.ts_event[a:b].max()) for a, b in bounds], dtype=np.int64)
        b_recv = np.array([int(rows.ts_recv[a:b].max()) for a, b in bounds], dtype=np.int64)
        b_seq = np.array([int(rows.sequence[b - 1]) for a, b in bounds], dtype=np.int64)
        run_ts = np.maximum.accumulate(b_ts)
        run_recv = np.maximum.accumulate(b_recv)
        act_at: dict[int, list[int]] = {}
        stop_at: dict[int, list[int]] = {}  # batch index -> orders whose placed end is before it
        for i, o in enumerate(orders):
            if any(o.samples[j] > o.samples[j + 1] for j in range(len(o.samples) - 1)):
                raise QtError("QT-CONFIG-001", f"order {o.key}: sample times must be sorted")
            if o.qty <= 0:
                results[i].end = "empty"
                continue
            if o.entry_seq is not None:
                k = int(np.searchsorted(b_seq, o.entry_seq, side="right" if o.inside else "left"))
            elif o.ts_live is not None:
                k = int(np.searchsorted(run_ts, o.ts_live, side="right"))
            else:
                raise QtError("QT-CONFIG-001", f"order {o.key}: needs ts_live or entry_seq")
            if k >= len(bounds):
                results[i].end = "after_data"
                results[i].samples_after_end = len(o.samples)
                continue
            if o.end_seq is not None:
                k_end = int(np.searchsorted(b_seq, o.end_seq, side="right" if o.end_inside else "left"))
                if k_end <= k:
                    results[i].end, results[i].end_ts = "cancelled_before_live", o.ts_end
                    continue
                stop_at.setdefault(k_end, []).append(i)
            act_at.setdefault(k, []).append(i)
        gap_starts = sorted(g[0] for g in gaps)
        deadlines: list[tuple[int, int, int, int]] = []  # (ts, 0 amend / 1 end, order, amend index)
        live: dict[int, _Live] = {}
        by_level: dict[tuple[int, int], set[int]] = {}
        view = Ladder()
        gap_i = 0

        def stop(i: int, why: str, ts: int) -> None:
            s = live.pop(i, None)
            if s is None:
                return
            by_level[(s.o.side, s.o.price)].discard(i)
            s.res.end, s.res.end_ts = why, ts
            s.res.samples_after_end += len(s.o.samples) - s.si
            s.si = len(s.o.samples)

        for bi, (a, b) in enumerate(bounds):
            ts = int(b_ts[bi])
            for s in live.values():
                _flush_samples(s, int(run_recv[bi]))
                _flush_probes(s, int(b_seq[bi]))
            while gap_i < len(gap_starts) and gap_starts[gap_i] < ts:
                for i in list(live):
                    stop(i, "censored_gap", ts)
                gap_i += 1
            prev_recv = int(run_recv[bi - 1]) if bi > 0 else None
            for i in act_at.get(bi, []):
                o = orders[i]
                res = results[i]
                if o.ts_end is not None and o.ts_live is not None and o.ts_end <= o.ts_live:
                    res.end = "cancelled_before_live"
                    continue
                s = _Live(i, o, res, self.k)
                while s.si < len(o.samples) and prev_recv is not None and o.samples[s.si] < prev_recv:
                    s.si += 1
                res.samples_before_live = s.si
                q_view = float(view.qty(o.side, o.price))
                opp_best = view.best(ASK if o.side == BID else BID)
                crosses = opp_best is not None and (
                    (o.side == BID and opp_best <= o.price) or (o.side == ASK and opp_best >= o.price)
                )
                res.activated_ts, res.activated_seq, res.visible_at_live = ts, int(b_seq[bi]), q_view
                if crosses and o.post_only:
                    res.end, res.end_ts = "rejected_post_only", ts
                    continue
                s.F[:] = np.where(self.is_foq, 0.0, q_view)
                live[i] = s
                by_level.setdefault((o.side, o.price), set()).add(i)
                if crosses:
                    at = o.ts_live if o.ts_live is not None else ts
                    self._cross(s, at, _opp_through(view, o), "crossed_at_live", "taker")
                _flush_samples(s, int(run_recv[bi]))
                while s.pi < len(o.probes) and o.probes[s.pi] < int(b_seq[bi]):
                    s.pi += 1
                _flush_probes(s, int(b_seq[bi]))
                for k, (t, _q) in enumerate(o.amends):
                    heapq.heappush(deadlines, (t, 0, i, k))
                if o.ts_end is not None and o.end_seq is None:
                    heapq.heappush(deadlines, (o.ts_end, 1, i, 0))
                if s.done.all():
                    stop(i, "filled", ts)
            for i in stop_at.pop(bi, []):
                stop(i, "cancelled", orders[i].ts_end if orders[i].ts_end is not None else ts)
            while deadlines and deadlines[0][0] < ts:
                t, kind, i, k = heapq.heappop(deadlines)
                s = live.get(i)
                if s is None:
                    continue
                if kind == 1:
                    stop(i, "cancelled", t)
                    continue
                q = float(orders[i].amends[k][1])
                s.leaves = np.where(s.done, s.leaves, np.maximum(q - s.filled, 0.0))
                s.done |= ~s.done & (s.leaves <= EPS * self.lot)
                if s.done.all():
                    stop(i, "amended_to_filled", t)
            restatement = False
            if rows.kind[a] == K_CLEAR and len(view):
                lv = {(int(rows.side[r]), int(rows.price[r])): int(rows.new_qty[r]) for r in range(a + 1, b)}
                cur = view.snapshot()
                restatement = lv == {(sd, p): q for sd in (BID, ASK) for p, q in cur[sd].items()}
            touched: set[int] = set()
            for r in range(a, b):
                kind = rows.kind[r]
                if kind == K_CLEAR:
                    view.clear()
                    if not restatement:
                        for i in list(live):
                            stop(i, "censored_clear", int(rows.ts_event[r]))
                    continue
                if kind in (K_DEPTH, K_SNAPSHOT):
                    side, price = int(rows.side[r]), int(rows.price[r])
                    prev = view.set(side, price, int(rows.new_qty[r]))
                    if restatement:
                        continue
                    for i in list(by_level.get((side, price), ())):
                        self._depth(live[i], float(prev), float(rows.new_qty[r]))
                        touched.add(i)
                    continue
                if kind == K_TRADE:
                    hit, price = int(rows.side[r]), int(rows.price[r])
                    if hit not in (BID, ASK):
                        continue
                    v, rts = float(rows.trade_qty[r]), int(rows.ts_event[r])
                    for i in list(by_level.get((hit, price), ())):
                        self._trade(live[i], rts, v)
                        touched.add(i)
                    for (sd, p), idx in list(by_level.items()):
                        if sd == hit and idx and ((hit == BID and p > price) or (hit == ASK and p < price)):
                            for i in list(idx):
                                self._through(live[i], rts)
                                touched.add(i)
            ends = set(touched)
            best_a, best_b = view.best(ASK), view.best(BID)
            for (sd, p), idx in by_level.items():
                if (sd == BID and best_a is not None and p >= best_a) or (
                    sd == ASK and best_b is not None and p <= best_b
                ):
                    ends |= idx
            for i in sorted(ends):
                s = live.get(i)
                if s is None:
                    continue
                o = s.o
                opp = best_a if o.side == BID else best_b
                if opp is not None and ((o.side == BID and opp <= o.price) or (o.side == ASK and opp >= o.price)):
                    self._cross(s, ts, _opp_through(view, o), "crossed", "maker")
                s.T[:] = 0.0
                if s.done.all():
                    stop(i, "filled", ts)
        last = end_ts if end_ts is not None else int(b_ts[-1])
        for s in list(live.values()):
            _flush_samples(s, None)
        pending_end = {i: t for t, kind, i, _k in deadlines if kind == 1}
        pending_end.update({i: orders[i].ts_end for k in stop_at.values() for i in k if orders[i].ts_end is not None})
        for i in list(live):
            t = pending_end.get(i)
            if t is not None and t <= last:
                stop(i, "cancelled", t)
            else:
                stop(i, "open_at_end", last)
        return results


def _num(x: float) -> str:
    """Shortest round-trip decimal without an exponent (the zoo's Rust `{}` of an f64)."""
    return str(np.format_float_positional(x, unique=True, trim="-"))


def _flush_probes(s: _Live, seq: int) -> None:
    """Records the estimate for every pending probe at or before batch `seq` (the state before that batch)."""
    probes = s.o.probes
    while s.pi < len(probes) and probes[s.pi] <= seq:
        s.res.probes.append((probes[s.pi], s.F.copy()))
        s.pi += 1


def _flush_samples(s: _Live, until_recv: int | None) -> None:
    """Records the estimate at every pending sample time before `until_recv` (all when None)."""
    times = s.o.samples
    while s.si < len(times) and (until_recv is None or times[s.si] < until_recv):
        s.res.samples.append((times[s.si], s.F.copy()))
        s.si += 1


def _opp_through(view: Ladder, o: SimOrder) -> list[tuple[int, int]]:
    """Visible opposite levels at prices at or through the order's price, best first."""
    out = []
    if o.side == BID:
        for p, q in view.levels(ASK):
            if p > o.price:
                break
            out.append((p, q))
    else:
        for p, q in view.levels(BID):
            if p < o.price:
                break
            out.append((p, q))
    return out


def _walk(levels: list[tuple[int, int]], skip: float, qty: float) -> float:
    """Volume-weighted price of `qty` units taken from `levels` (best first) after the first `skip` units."""
    cost = 0.0
    left = qty
    for p, q in levels:
        avail = float(q)
        if skip >= avail:
            skip -= avail
            continue
        avail -= skip
        skip = 0.0
        take = min(avail, left)
        cost += take * p
        left -= take
        if left <= 0:
            break
    return cost / qty if qty > 0 else float("nan")


def _batch_starts(rows: L2Arrays) -> list[int]:
    """Row index where each batch starts (a new `event_id` or partition, or after a `LAST_IN_BATCH` row)."""
    n = len(rows)
    starts = [0]
    for r in range(1, n):
        if rows.part[r] != rows.part[r - 1] or rows.event_id[r] != rows.event_id[r - 1] or (rows.flags[r - 1] & 1) != 0:
            starts.append(r)
    return starts


__all__ = [
    "ALL_FAMILIES",
    "ATOMIC_FAMILIES",
    "BOUND_FAMILIES",
    "QUEUE_FAMILIES",
    "Engine",
    "Model",
    "OrderResult",
    "SimOrder",
]
