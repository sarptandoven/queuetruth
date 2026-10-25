"""`queuetruth pm score`: reward-aware market-making PnL with a fill band, and the sim-versus-live comparison.

The scored orders are hypothetical quotes (`quotes`, entered by time like zoo shadows) or the user's own live orders
(entered exactly where they joined the queue). Each live order (leg) rests in the replay as long as it rested live:
until its cancel, expiry or re-price; one still resting when the capture ended rests to the end of the data; one
that filled completely, with no cancel instruction to replay, rests until its last live fill or `window_ns` after
entry, whichever is later. The live arm and its rewards are scored over each order's real life. Each order is
replayed on the own-free book under three fill scenarios: `pessimistic` (`trade_through_only`: fills only when the
price trades through the order), `model` (the fitted queue model: `pm calibrate`'s best, or `model`) and
`optimistic` (`front_of_queue`: no queue ahead). Under FIFO the true fills of every order lie between the two
bounds (it fills no later than `trade_through_only` and no earlier than `front_of_queue`, SPEC 5.3.3 h, i). Each
order is a shadow of its own, as in the zoo: the user's other orders resting ahead at the same price are not in its
queue (the calibration counts them; a warning names the orders that had some).

Per scenario, in dollars, with `M` the mark (the settlement value of YES, else the last mid of the published book,
flagged), `m` the mid just before the fill's batch, `p` the fill price, `q` the quantity and `s = +1` for a buy:

- `spread_capture = sum s (m - p) q`; `adverse_selection = sum s (m - M) q` (positive = the price moved against
  the fills); `trading = spread_capture - adverse_selection = sum s (M - p) q`;
- `fees`: Kalshi per fill from the series fee type with the fee-rounding accumulator per order and the member's
  balance precision; Polymarket takers by the market's fee schedule, makers 0;
- `rewards`: Kalshi LIP (expected payout of the captured part of each time period, from the time-averaged snapshot
  score, `queuetruth.pm.rewards`) or Polymarket liquidity rewards (expected share x daily reward, with the
  others-competition bounds as `rewards_lower` / `rewards_upper`), computed on the book with the scenario's own
  resting size: the own-free book plus the simulated resting quotes (live arm: the published book with the live
  resting size);
- `rebates` (Polymarket maker rebates): the scenario's maker fee-equivalent over the market's fee-equivalent of the
  captured prints, times the rebate share of the prints' taker fees;
- `total = trading - fees + rewards + rebates`.

`band` gives each quantity under the pessimistic, model and optimistic scenarios as `[lower, model, upper]` with
lower / upper the minimum / maximum over the three. `band.trading_bound` is stricter: per order the trading PnL is
linear in its filled quantity, which lies between the two bounds' quantities, so the sum over orders of each order's
minimum and maximum is a bound on the trading PnL of the true fills; `band.net_trading_bound` is the same for the
trading PnL net of fees (fees grow with the filled quantity; their per-fill rounding aside). Rewards are evaluated
on the bounds' resting paths, which bracket the true resting size but not, in every book, the reward.

Markouts: for each scenario and horizon `h`, `adverse_selection(h) = sum s (m - mid(t + h)) q` over fills whose
horizon ends inside the data. Sim versus live (own orders): per model, live and simulated first fills inside the
same window (entry to the cancel, at most `window_ns`), the 2x2 table, the fill-rate bias with a bootstrap interval
over orders, the exact McNemar test, the median log ratio of times to first fill, and bracket violations (a live fill
before the optimistic bound's, or after the pessimistic bound's): on a consistent FIFO feed there are none. When the
model comes from a calibration of this same capture, its comparison is repeated for the calibration's train orders
(in-sample) and test orders (`orders`), so the agreement of the fitted model is read on orders it was not fitted on.

The model's source is reported (`model_source`), with the calibration's trust verdict (`calibration_trust`); a
`refused` calibration is scored with a warning that says so.
"""

from __future__ import annotations

import datetime
import json
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.live._common import rfc3339_ns
from queuetruth.pm.book import ASK, BID, Ladder
from queuetruth.pm.calibrate import LevelIndex
from queuetruth.pm.dataset import L2Arrays
from queuetruth.pm.fees import KalshiFeeAccumulator, KalshiFeeSchedule, PolymarketFeeSchedule, maker_rebate
from queuetruth.pm.models import (
    Band,
    PmCalibrationTrust,
    PmScore,
    PmScoreSpec,
    PnlParts,
    SimVsLive,
    validate_spec,
)
from queuetruth.pm.queue import K_CLEAR, K_DEPTH, K_SNAPSHOT, K_TRADE, Engine, Model, OrderResult, SimOrder
from queuetruth.pm.queue import _batch_starts as batch_starts
from queuetruth.pm.rewards import (
    LipProgram,
    PolyRewardParams,
    PriceGrid,
    kalshi_expected_payout,
    lip_snapshot,
    poly_expected_reward,
    poly_sample,
)
from queuetruth.pm.tables import table
from queuetruth.pm.terms import TERMS_NOTICE
from queuetruth.pm.units import ONE, dec
from queuetruth.pm.workdir import Leg, PmDir, legs, load, resolve_ref
from queuetruth.schemas.io import write_table
from queuetruth.schemas.runid import run_id, spec_echo
from queuetruth.stats.paired import mcnemar
from queuetruth.stats.resampling import bootstrap_rng, percentile_ci

SCENARIOS = ("pessimistic", "model", "optimistic")
DAY_NS = 86_400_000_000_000
NS = 1_000_000_000


@dataclass
class Fill:
    """One fill of a scenario (prices in price units, quantity in size units)."""

    scenario: str
    key: str
    iid: int
    ts: int
    side: int
    price: int
    qty: int
    liquidity: str
    reason: str
    mid_before: float | None = None
    fee: float = 0.0


@dataclass
class OrderRow:
    """One scored order under one scenario."""

    scenario: str
    key: str
    iid: int
    side: int
    price: int
    qty: int
    ts_live: int | None
    ts_end: int | None
    end: str
    fills: list[Fill] = field(default_factory=list)

    @property
    def filled(self) -> int:
        """Filled quantity (size units)."""
        return sum(f.qty for f in self.fills)


class Mids:
    """Mid of the published book after every batch (price units, NaN when a side is empty)."""

    def __init__(self, rows: L2Arrays) -> None:
        n = len(rows)
        starts = batch_starts(rows) if n else []
        bounds = list(zip(starts, [*starts[1:], n], strict=True))
        view = Ladder()
        ts, mid, bb, ba = [], [], [], []
        for a, b in bounds:
            for r in range(a, b):
                k = rows.kind[r]
                if k == K_CLEAR:
                    view.clear()
                elif k in (K_DEPTH, K_SNAPSHOT):
                    view.set(int(rows.side[r]), int(rows.price[r]), int(rows.new_qty[r]))
            bid, ask = view.best(BID), view.best(ASK)
            ts.append(int(rows.ts_event[a:b].max()))
            bb.append(bid)
            ba.append(ask)
            mid.append((bid + ask) / 2.0 if bid is not None and ask is not None and bid < ask else math.nan)
        self.ts = np.maximum.accumulate(np.asarray(ts, dtype=np.int64)) if ts else np.zeros(0, dtype=np.int64)
        self.mid = np.asarray(mid, dtype=float)
        self.end = int(self.ts[-1]) if ts else 0
        self.start = int(self.ts[0]) if ts else 0

    def before(self, t: int) -> float | None:
        """Mid after the last batch strictly before `t`."""
        i = int(np.searchsorted(self.ts, t, side="left")) - 1
        if i < 0 or not math.isfinite(self.mid[i]):
            return None
        return float(self.mid[i])

    def at(self, t: int) -> float | None:
        """Mid after the last batch at or before `t` (None past the end of the data)."""
        if t > self.end:
            return None
        i = int(np.searchsorted(self.ts, t, side="right")) - 1
        if i < 0 or not math.isfinite(self.mid[i]):
            return None
        return float(self.mid[i])

    def last(self) -> float | None:
        """Last finite mid."""
        fin = np.flatnonzero(np.isfinite(self.mid))
        return float(self.mid[fin[-1]]) if fin.size else None


# ---- quotes -----------------------------------------------------------------------------------------------------


def _ts(v: Any, what: str, where: str) -> int | None:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v == "":
        return None
    if isinstance(v, (int, np.integer)):
        return int(v)
    s = str(v).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return rfc3339_ns(s, what, where)[0]


def read_quotes(path: str | Path, d: PmDir) -> list[tuple[int, SimOrder]]:
    """Hypothetical quotes (CSV or Parquet): `quote_id`, `instrument` (symbol) or `instrument_id`, `side`
    (buy / sell, bid / ask, B / A), `price` (YES dollars), `qty` (contracts or shares), `ts_submit` (ns or RFC 3339),
    optional `latency_ns` (default 0), `ts_cancel` and `post_only` (default true)."""
    p = Path(path)
    if not p.is_file():
        raise QtError("QT-INPUT-001", f"{p}: quotes file not found")
    try:
        t = (
            pq.read_table(p)
            if p.suffix.lower() == ".parquet"
            else pacsv.read_csv(
                p, convert_options=pacsv.ConvertOptions(strings_can_be_null=True, column_types=_QUOTE_TYPES)
            )
        )
    except (OSError, pa.ArrowInvalid) as e:
        raise QtError("QT-INPUT-002", f"{p}: {e}") from None
    need = {"quote_id", "side", "price", "qty", "ts_submit"}
    missing = need - set(t.column_names)
    if missing or not ({"instrument", "instrument_id"} & set(t.column_names)):
        raise QtError("QT-INPUT-002", f"{p}: quotes need columns {sorted(need)} and instrument or instrument_id")
    by_symbol = {str(m["symbol"]): int(m["instrument_id"]) for m in d.markets}
    exps = {int(m["instrument_id"]): int(m["size_exp"]) for m in d.markets}
    out: list[tuple[int, SimOrder]] = []
    seen: set[str] = set()
    for n, r in enumerate(t.to_pylist(), start=2):
        where = f"{p}:{n}"
        qid = str(r["quote_id"])
        if qid in seen:
            raise QtError("QT-INPUT-003", f"{where}: duplicate quote_id {qid}")
        seen.add(qid)
        if r.get("instrument_id") is not None:
            iid = int(r["instrument_id"])
        else:
            sym = str(r.get("instrument"))
            if sym not in by_symbol:
                raise QtError("QT-INPUT-002", f"{where}: instrument {sym!r} is not in the ingest")
            iid = by_symbol[sym]
        if iid not in exps:
            raise QtError("QT-INPUT-002", f"{where}: instrument_id {iid} is not in the ingest")
        side_raw = str(r["side"]).strip().lower()
        side = BID if side_raw in ("buy", "bid", "b") else ASK if side_raw in ("sell", "ask", "a") else None
        if side is None:
            raise QtError("QT-INPUT-002", f"{where}: side {r['side']!r}")
        price_d = dec(str(r["price"]), "price", where)
        qty_d = dec(str(r["qty"]), "qty", where)
        if price_d is None or qty_d is None or qty_d <= 0:
            raise QtError("QT-INPUT-002", f"{where}: price and a positive qty are required")
        price = price_d.scaleb(4)
        qty = qty_d.scaleb(-exps[iid])
        if price != price.to_integral_value() or not 0 < price < ONE:
            raise QtError("QT-INPUT-004", f"{where}: price {price_d} is not a 0.0001 price inside (0, 1)")
        if qty != qty.to_integral_value():
            raise QtError("QT-INPUT-004", f"{where}: qty {qty_d} is not representable at size_exp {exps[iid]}")
        sub = _ts(r["ts_submit"], "ts_submit", where)
        if sub is None:
            raise QtError("QT-INPUT-002", f"{where}: ts_submit is required")
        lat = int(r.get("latency_ns") or 0)
        if lat < 0:
            raise QtError("QT-INPUT-002", f"{where}: latency_ns is negative")
        po = r.get("post_only")
        post_only = True if po is None else str(po).strip().lower() in ("true", "1", "yes")
        out.append(
            (
                iid,
                SimOrder(
                    qid,
                    side,
                    int(price),
                    float(int(qty)),
                    ts_live=sub + lat,
                    ts_end=_ts(r.get("ts_cancel"), "ts_cancel", where),
                    post_only=post_only,
                ),
            )
        )
    return out


_QUOTE_TYPES = {
    "quote_id": pa.utf8(),
    "instrument": pa.utf8(),
    "side": pa.utf8(),
    "price": pa.utf8(),
    "qty": pa.utf8(),
    "ts_submit": pa.utf8(),
    "ts_cancel": pa.utf8(),
    "post_only": pa.utf8(),
}


# ---- rewards over time ------------------------------------------------------------------------------------------


@dataclass
class OwnStep:
    """A change of own resting size: time, side, price, order key, signed size."""

    ts: int
    side: int
    price: int
    key: str
    delta: int


def steps_of_orders(rows: list[OrderRow]) -> list[OwnStep]:
    """Resting-size steps of simulated orders: + at activation, - per fill, - the rest at the end."""
    out: list[OwnStep] = []
    for o in rows:
        if o.ts_live is None:
            continue
        out.append(OwnStep(o.ts_live, o.side, o.price, o.key, o.qty))
        left = o.qty
        for f in o.fills:
            if f.liquidity == "taker":
                out.append(OwnStep(o.ts_live, o.side, o.price, o.key, -f.qty))
            else:
                out.append(OwnStep(f.ts, o.side, o.price, o.key, -f.qty))
            left -= f.qty
        if left > 0 and o.ts_end is not None:
            out.append(OwnStep(o.ts_end, o.side, o.price, o.key, -left))
    out.sort(key=lambda s: (s.ts, s.delta))
    return out


def steps_of_legs(gl: list[Leg]) -> list[OwnStep]:
    """Live resting-size steps of own legs (entry, fills, amends, end)."""
    out: list[OwnStep] = []
    for g in gl:
        times = sorted(
            {g.entry_ts, *(t for t, _q, _p in g.fills), *(t for t, _ in g.amends)}
            | ({g.end_ts} if g.end_ts is not None else set())
        )
        prev = 0
        for t in times:
            cur = _resting(g, t)
            if cur != prev:
                out.append(OwnStep(t, g.side, g.price, g.key, cur - prev))
                prev = cur
    out.sort(key=lambda s: (s.ts, s.delta))
    return out


def _resting(g: Leg, t: int) -> int:
    if t < g.entry_ts or (g.end_ts is not None and g.end_ts <= t):
        return 0
    total = g.qty
    for at, tot in g.amends:
        if at <= t:
            total = tot
    return max(total - sum(q for ft, q, _p in g.fills if ft <= t), 0)


def integrate(
    rows: L2Arrays,
    steps: list[OwnStep],
    add_own: bool,
    evaluate: Callable[[list[tuple[int, int]], list[tuple[int, int]], dict[int, dict[int, list[int]]]], tuple],
    w0: int,
    w1: int,
) -> np.ndarray:
    """Integral over `[w0, w1]` (in seconds x value) of `evaluate(bids, asks, own)` on the book replayed from `rows`
    with the own resting sizes of `steps` (added to the book's levels when `add_own`); `own` is
    `{side: {price: [order sizes]}}`, the levels are best first."""
    n = len(rows)
    starts = batch_starts(rows) if n else []
    bounds = list(zip(starts, [*starts[1:], n], strict=True))
    view = Ladder()
    # side -> price -> order key -> resting size; keys leave when their size returns to 0, so the state holds only
    # orders resting now (not every order of the run): each evaluation costs the live orders, not the history
    own: dict[int, dict[int, dict[str, int]]] = {BID: {}, ASK: {}}
    events: list[tuple[int, int, int]] = []  # (ts, 0 = batch / 1 = step, index)
    for i, (a, b) in enumerate(bounds):
        events.append((int(rows.ts_event[a:b].max()), 0, i))
    for j, s in enumerate(steps):
        events.append((s.ts, 1, j))
    events.sort()
    total: np.ndarray | None = None
    cur_val: tuple | None = None
    cur_t: int | None = None
    started = False

    def value() -> tuple:
        ob = {p: q for p, d in own[BID].items() if (q := [v for v in d.values() if v > 0])}
        oa = {p: q for p, d in own[ASK].items() if (q := [v for v in d.values() if v > 0])}
        if add_own:
            bids = _merge(view.levels(BID), {p: sum(v) for p, v in ob.items()}, True)
            asks = _merge(view.levels(ASK), {p: sum(v) for p, v in oa.items()}, False)
        else:
            bids, asks = list(view.levels(BID)), list(view.levels(ASK))
            bids = [(p, max(q, sum(ob.get(p, [])))) for p, q in bids]
            asks = [(p, max(q, sum(oa.get(p, [])))) for p, q in asks]
        return evaluate(bids, asks, {BID: ob, ASK: oa})

    def accrue(lo: int, hi: int) -> None:
        """Adds the value of the current state over `[lo, hi]` (evaluated lazily: states that hold for no time inside
        the window, e.g. between events with one timestamp or outside it, are never evaluated)."""
        nonlocal total, cur_val
        if hi <= lo:
            return
        if cur_val is None:
            cur_val = value()
        v = np.asarray(cur_val, dtype=float) * ((hi - lo) / NS)
        total = v if total is None else total + v

    for t, kind, idx in events:
        if cur_t is not None and started:
            accrue(max(cur_t, w0), min(t, w1))
        if t >= w1 and started and cur_t is not None and cur_t >= w1:
            break  # nothing after the window counts
        if kind == 0:
            a, b = bounds[idx]
            for r in range(a, b):
                k = rows.kind[r]
                if k == K_CLEAR:
                    view.clear()
                elif k in (K_DEPTH, K_SNAPSHOT):
                    view.set(int(rows.side[r]), int(rows.price[r]), int(rows.new_qty[r]))
            started = True
        else:
            s = steps[idx]
            d = own[s.side].setdefault(s.price, {})
            q = d.get(s.key, 0) + s.delta
            if q:
                d[s.key] = q
            else:
                d.pop(s.key, None)
                if not d:
                    del own[s.side][s.price]
        cur_t = t
        cur_val = None
    if cur_t is not None and started:
        accrue(max(cur_t, w0), w1)
    return total if total is not None else np.zeros(0)


def _merge(levels: Any, own: dict[int, int], bids: bool) -> list[tuple[int, int]]:
    book = dict(levels)
    for p, q in own.items():
        book[p] = book.get(p, 0) + q
    return sorted(((p, q) for p, q in book.items() if q > 0), key=lambda x: -x[0] if bids else x[0])


# ---- the scorer --------------------------------------------------------------------------------------------------


@dataclass
class _ModelChoice:
    model: Model
    source: str  # calibration | given | default
    calibration: dict[str, Any] | None
    warnings: list[str]


def _model_of(sp: PmScoreSpec) -> _ModelChoice:
    if sp.model is not None and sp.calibration is not None:
        raise QtError("QT-USAGE-001", "give either calibration or model, not both")
    if sp.model is not None:
        return _ModelChoice(Model.parse(sp.model), "given", None, [])
    if sp.calibration is not None:
        p = Path(sp.calibration.path)
        f = p / "pm_calibration.json" if p.is_dir() else p
        if not f.is_file():
            raise QtError("QT-INPUT-001", f"{f}: calibration not found")
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise QtError("QT-INPUT-002", f"{f}: invalid JSON: {e}") from None
        if doc.get("schema") != "qt.pm_calibration":
            raise QtError("QT-INPUT-002", f"{f}: not a qt.pm_calibration document")
        if not doc.get("best"):
            raise QtError(
                "QT-INPUT-002",
                f"{f}: the calibration fitted no model (no training order had queue truth)",
                "calibrate on more captures, with Kalshi queue positions or a Polymarket chain export, or pass a "
                "model id (--model)",
            )
        warnings = []
        trust = doc.get("trust") or {}
        if trust.get("verdict") == "refused":
            warnings.append(
                f"model: the calibration's verdict is refused ({', '.join(trust.get('reasons') or [])}): its best fit "
                f"{doc['best']} is scored, but the model scenario is not a calibrated estimate (see its trust lines)"
            )
        return _ModelChoice(Model.parse(str(doc["best"])), "calibration", doc, warnings)
    return _ModelChoice(
        Model("power3", 1.0),
        "default",
        None,
        [
            "model: no calibration or model given; using power3[n=1] (cancels split in proportion to the size "
            "ahead), an uncalibrated default"
        ],
    )


def _leg_life(g: Leg, window_ns: int) -> tuple[int | None, str]:
    """(end of the leg's life in the replay, how it ended live): its cancel, expiry or re-price (`ended`); a leg that
    filled completely, with no cancel to replay, rests until its last fill or `window_ns` after entry, whichever is
    later (`filled`); one still resting when the capture ended rests to the end of the data (`open_at_end`)."""
    if g.end_ts is not None:
        return g.end_ts, "ended"
    total = g.amends[-1][1] if g.amends else g.qty
    if g.fills and sum(q for _t, q, _p in g.fills) >= total:
        return max(g.fills[-1][0], g.entry_ts + window_ns), "filled"
    return None, "open_at_end"


def _dollars(price_units: float, size_units: int, size_exp: int) -> float:
    return price_units / ONE * size_units * 10.0**size_exp


def score(spec: PmScoreSpec | dict[str, Any]) -> PmScore:
    """Runs `pm score` (see the module docstring)."""
    sp = validate_spec(PmScoreSpec, spec)
    if not sp.out:
        raise QtError("QT-CONFIG-001", "out: an output directory is required")
    d = load(sp.pm.path)
    choice = _model_of(sp)
    model, warnings = choice.model, list(choice.warnings)
    if model.family in ("front_of_queue", "trade_through_only"):
        raise QtError("QT-CONFIG-001", f"model {model.id} is a fill bound; give a queue model")
    eng = Engine([Model("trade_through_only"), model, Model("front_of_queue")])
    source = "quotes" if sp.quotes is not None else "live"
    all_legs = legs(d)
    by_inst_orders: dict[int, list[SimOrder]] = defaultdict(list)
    leg_of: dict[str, Leg] = {}
    ended: dict[str, str] = {}
    if sp.quotes is not None:
        for iid, o in read_quotes(sp.quotes.path, d):
            by_inst_orders[iid].append(o)
    else:
        if not all_legs:
            raise QtError("QT-INPUT-002", f"{d.root}: no own orders to score; pass quotes= for hypothetical quotes")
        index = LevelIndex(all_legs)
        stacked = sum(1 for i, g in enumerate(all_legs) if index.own_ahead(i, g.entry_ts) > 0)
        if stacked:
            warnings.append(
                f"own orders: {stacked} of {len(all_legs)} order(s) joined behind your own size resting at the same "
                "price; the replay scores each order as an independent shadow (your size ahead is not in its queue), "
                "so their model fills can come early"
            )
        for g in all_legs:
            end, ended[g.key] = _leg_life(g, sp.window_ns)
            leg_of[g.key] = g
            by_inst_orders[g.instrument_id].append(
                SimOrder(
                    g.key,
                    g.side,
                    g.price,
                    float(g.qty),
                    entry_seq=g.entry_seq,
                    inside=g.inside,
                    **(g.sim_end() if g.end_ts is not None else {"ts_end": end}),
                    post_only=g.post_only,
                    amends=[(t, float(q)) for t, q in g.amends],
                )
            )
    orders: dict[str, list[OrderRow]] = {s: [] for s in SCENARIOS}
    live_orders: list[OrderRow] = []
    live_fees = _live_fee_index(d)
    marks: dict[str, Any] = {}
    reward_parts: dict[str, dict[str, list[float]]] = {s: defaultdict(list) for s in (*SCENARIOS, "live")}
    programs: list[dict[str, Any]] = []
    rebate_parts: dict[str, float] = defaultdict(float)
    markout_rows: list[dict[str, Any]] = []
    fee_unknown = 0
    for iid in d.instrument_ids():
        sims = by_inst_orders.get(iid, [])
        if not sims:
            continue
        mk = d.market(iid)
        size_exp = int(mk["size_exp"])
        free, pub = d.book(iid, True), d.book(iid, False)
        mids = Mids(pub)
        res = eng.run(free, sims, gaps=d.gaps(iid))
        for k, scen in enumerate(SCENARIOS):
            for o, r in zip(sims, res, strict=True):
                orders[scen].append(_order_row(scen, iid, o, r, k, mids))
        if source == "live":
            for o in sims:
                live_orders.append(_live_row(leg_of[o.key], iid, live_fees, mids, ended[o.key]))
        mark, mark_src = _mark(sp, mk, mids)
        marks[str(mk["symbol"])] = {"value": None if mark is None else mark / ONE, "source": mark_src}
        if mark_src == "last_mid":
            warnings.append(f"mark: {mk['symbol']} has no settlement; fills are marked to the last mid")
        fee_unknown += _fees(sp, d.venue, mk, orders, live_orders, iid, warnings)
        # rewards
        grid = _grid(mk)
        scen_steps = {s: steps_of_orders([o for o in orders[s] if o.iid == iid]) for s in SCENARIOS}
        live_steps = steps_of_legs([leg_of[o.key] for o in live_orders if o.iid == iid]) if source == "live" else []
        if d.venue == "kalshi":
            for prog in _lip_programs(mk):
                w0, w1 = max(prog.start_ns, mids.start), min(prog.end_ns, mids.end)
                if w1 <= w0:
                    continue
                ev = _lip_eval(prog, grid)
                programs.append(
                    {
                        "instrument_id": iid,
                        "program_id": prog.program_id,
                        "start_ns": prog.start_ns,
                        "end_ns": prog.end_ns,
                        "period_reward": str(prog.period_reward),
                        "target_size": prog.target_size,
                        "discount_factor": str(prog.discount_factor),
                        "total_snapshots": prog.total_snapshots,
                        "scored_from_ns": w0,
                        "scored_to_ns": w1,
                    }
                )
                for s in SCENARIOS:
                    v = integrate(free, scen_steps[s], True, ev, w0, w1)
                    pay = float(kalshi_expected_payout(float(v[0]) if v.size else 0.0, prog))
                    for part in ("point", "lower", "upper"):
                        reward_parts[s][part].append(pay)
                if source == "live":
                    v = integrate(pub, live_steps, False, ev, w0, w1)
                    pay = float(kalshi_expected_payout(float(v[0]) if v.size else 0.0, prog))
                    for part in ("point", "lower", "upper"):
                        reward_parts["live"][part].append(pay)
        else:
            params_by_day = _poly_params(sp, mk, mids, warnings)
            if params_by_day:
                small = sum(1 for o in sims if o.qty < params_by_day[0][1].min_size)
                if small:
                    warnings.append(
                        f"rewards: {small} of {len(sims)} scored order(s) on {mk['symbol']} are smaller than the "
                        f"rewards min size ({_units_text(params_by_day[0][1].min_size, size_exp)}) and earn nothing"
                    )
            for day, params in params_by_day:
                w0, w1 = max(day * DAY_NS, mids.start), min((day + 1) * DAY_NS, mids.end)
                if w1 <= w0:
                    continue
                ev = _poly_eval(params)
                programs.append(
                    {
                        "instrument_id": iid,
                        "day": (datetime.date(1970, 1, 1) + datetime.timedelta(days=day)).isoformat(),
                        "daily_reward": str(params.daily_reward),
                        "max_spread_cents": params.max_spread_cents,
                        "min_size": params.min_size,
                        "multiplier": params.multiplier,
                        "c": params.c,
                        "scored_from_ns": w0,
                        "scored_to_ns": w1,
                    }
                )
                for s in SCENARIOS:
                    v = integrate(free, scen_steps[s], True, ev, w0, w1)
                    for part, j in (("lower", 0), ("point", 1), ("upper", 2)):
                        reward_parts[s][part].append(
                            float(poly_expected_reward(float(v[j]) if v.size else 0.0, params))
                        )
                if source == "live":
                    v = integrate(pub, live_steps, False, ev, w0, w1)
                    for part, j in (("lower", 0), ("point", 1), ("upper", 2)):
                        reward_parts["live"][part].append(
                            float(poly_expected_reward(float(v[j]) if v.size else 0.0, params))
                        )
            pool = _poly_rebate_pool(sp, mk, pub, warnings)
            if pool is not None:
                tot_eq, pool_usd, sched = pool
                for s in (*SCENARIOS, "live"):
                    rows_s = live_orders if s == "live" else orders[s]
                    mine = sum(
                        float(sched.fee_equivalent(Decimal(f.qty).scaleb(size_exp), Decimal(f.price).scaleb(-4)))
                        for o in rows_s
                        if o.iid == iid
                        for f in o.fills
                        if f.liquidity == "maker"
                    )
                    if tot_eq > 0:
                        rebate_parts[s] += float(
                            maker_rebate(
                                Decimal(repr(min(mine, tot_eq))), Decimal(repr(tot_eq)), Decimal(repr(pool_usd))
                            )
                        )
        markout_rows += _markouts(sp, orders, iid, size_exp, mids)
    if fee_unknown:
        warnings.append(f"fees: {fee_unknown} taker fill(s) without a fee schedule were charged 0 (pass poly_category)")
    parts: dict[str, PnlParts] = {}
    for s in SCENARIOS:
        parts[s] = _parts(orders[s], d, marks, reward_parts[s], rebate_parts[s])
    live_parts = _parts(live_orders, d, marks, reward_parts["live"], rebate_parts["live"]) if source == "live" else None
    band = _band(parts, orders, d, marks)
    tb = band["trading_bound"]
    if (
        live_parts is not None
        and live_parts.trading is not None
        and tb.lower is not None
        and tb.upper is not None
        and not tb.lower - 1e-6 <= live_parts.trading <= tb.upper + 1e-6
    ):
        warnings.append(
            f"live trading PnL {live_parts.trading:.2f} is outside the fill bounds' trading bound [{tb.lower:.2f}, "
            f"{tb.upper:.2f}]: the book and the live log disagree (missed deltas or misplaced queue entries)"
        )
    subsets = _calibration_subsets(choice, d, all_legs) if source == "live" else {}
    svl = _sim_vs_live(orders, live_orders, model, sp, d, subsets) if source == "live" else []
    ci = _trading_ci(orders["model"], d, marks, sp)
    rewards_doc = _rewards_doc(d, sp, reward_parts, programs)
    out = Path(sp.out)
    out.mkdir(parents=True, exist_ok=True)
    files = _write_tables(out, orders, live_orders, d, marks)
    spec_doc = sp.model_dump(mode="json")
    cal_trust = None
    if choice.calibration is not None and choice.calibration.get("trust"):
        cal_trust = PmCalibrationTrust.model_validate(choice.calibration["trust"])
    doc = PmScore(
        run_id=run_id(spec_doc, __version__, resolve_ref),
        spec=spec_echo(spec_doc),
        venue=d.venue,
        terms=TERMS_NOTICE,
        source=source,
        model_id=model.id,
        model_source=choice.source,
        calibration_trust=cal_trust,
        n_orders=len(orders["model"]),
        mark=marks,
        scenarios=parts,
        band=band,
        markouts=markout_rows,
        rewards=rewards_doc,
        live=live_parts,
        sim_vs_live=svl,
        trading_pnl_ci=ci,
        warnings=warnings,
        files=files,
    )
    (out / "pm_score.json").write_text(json.dumps(doc.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return doc


def _order_row(scen: str, iid: int, o: SimOrder, r: OrderResult, k: int, mids: Mids) -> OrderRow:
    """One order under model `k`. The engine keeps an order live until every model is done with it, so an order
    this model filled completely ends `filled` at its last fill here."""
    row = OrderRow(scen, o.key, iid, o.side, o.price, int(o.qty), r.activated_ts, r.end_ts, r.end)
    for ts, q, reason, liq, px in r.fills[k]:
        row.fills.append(Fill(scen, o.key, iid, ts, o.side, round(px), round(q), liq, reason, mids.before(ts)))
    if row.fills and row.filled >= row.qty:
        row.end, row.ts_end = "filled", max(f.ts for f in row.fills)
    return row


def _live_fee_index(d: PmDir) -> dict[tuple[str, int, int], float]:
    """(order key, exchange time, qty) -> fee of every live fill event that carries one."""
    out: dict[tuple[str, int, int], float] = {}
    for e in d.events:
        if e.get("event_type") == "fill" and e.get("fee") is not None and e.get("ts_exchange") is not None:
            out[(str(e["order_key"]), int(e["ts_exchange"]), int(e["qty"] or 0))] = float(e["fee"])
    return out


def _live_row(g: Leg, iid: int, fees: dict[tuple[str, int, int], float], mids: Mids, ended: str) -> OrderRow:
    """The live arm of a leg over its real life: end `filled`, `ended` (cancelled, expired, re-priced or amended up
    live) or `open_at_end` (still resting when the capture ended)."""
    row = OrderRow("live", g.key, iid, g.side, g.price, g.qty, g.entry_ts, g.end_ts, ended)
    for ts, q, px in g.fills:
        f = Fill("live", g.key, iid, ts, g.side, px, q, "maker", "live", mids.before(ts))
        f.fee = fees.get((g.order_key, ts, q), math.nan)
        row.fills.append(f)
    return row


def _mark(sp: PmScoreSpec, mk: dict[str, Any], mids: Mids) -> tuple[float | None, str]:
    sym = str(mk["symbol"])
    if sym in sp.settle:
        v = dec(sp.settle[sym], "settle", sym)
        if v is None or not Decimal(0) <= v <= Decimal(1):
            raise QtError("QT-CONFIG-001", f"settle: {sym}: a YES value in [0, 1] dollars is required")
        return float(v) * ONE, "override"
    if mk.get("settlement") is not None:
        return float(Decimal(str(mk["settlement"]))) * ONE, "settlement"
    last = mids.last()
    return last, "last_mid"


def _fees(
    sp: PmScoreSpec,
    venue: str,
    mk: dict[str, Any],
    orders: dict[str, list[OrderRow]],
    live_orders: list[OrderRow],
    iid: int,
    warnings: list[str],
) -> int:
    """Sets `Fill.fee` (dollars) on every scenario fill of the instrument; returns taker fills left uncharged."""
    size_exp = int(mk["size_exp"])
    unknown = 0
    if venue == "kalshi":
        sched = _kalshi_schedule(sp, mk, warnings)
        for s in SCENARIOS:
            pos = Decimal(0)
            accs: dict[str, KalshiFeeAccumulator] = {}
            fills = sorted(
                ((f.ts, o.key, j, f) for o in orders[s] if o.iid == iid for j, f in enumerate(o.fills)),
                key=lambda x: (x[0], x[1], x[2]),
            )
            for _t, key, _j, f in fills:
                acc = accs.setdefault(key, KalshiFeeAccumulator(sp.member))
                q = Decimal(f.qty).scaleb(size_exp)
                p = Decimal(f.price).scaleb(-4)
                rev, pos = _kalshi_revenue(f.side, q, p, pos)
                fee = acc.fill(rev, sched.model_fee(q, p, maker=f.liquidity == "maker", ts_ns=f.ts))
                f.fee = float(fee.net_fee)
        for o in live_orders:
            if o.iid != iid:
                continue
            for f in o.fills:
                if math.isnan(f.fee):
                    q, p = Decimal(f.qty).scaleb(size_exp), Decimal(f.price).scaleb(-4)
                    f.fee = float(
                        KalshiFeeAccumulator(sp.member).fill(-q * p, sched.model_fee(q, p, maker=True)).net_fee
                    )
        return 0
    pf = mk.get("poly_fee")
    sched_p: PolymarketFeeSchedule | None = None
    if pf:
        sched_p = PolymarketFeeSchedule(
            fees_enabled=bool(pf["fees_enabled"]),
            rate=Decimal(pf["rate"]),
            exponent=Decimal(pf["exponent"]),
            taker_only=bool(pf["taker_only"]),
            rebate_rate=Decimal(pf["rebate_rate"]),
        )
    elif sp.poly_category:
        sched_p = PolymarketFeeSchedule.for_category(sp.poly_category)
    for s in SCENARIOS:
        for o in orders[s]:
            if o.iid != iid:
                continue
            for f in o.fills:
                if f.liquidity == "maker":
                    f.fee = (
                        0.0
                        if sched_p is None
                        else float(sched_p.maker_fee(Decimal(f.qty).scaleb(size_exp), Decimal(f.price).scaleb(-4)))
                    )
                elif sched_p is None:
                    unknown += 1
                else:
                    f.fee = float(sched_p.taker_fee(Decimal(f.qty).scaleb(size_exp), Decimal(f.price).scaleb(-4)))
    for o in live_orders:
        if o.iid != iid:
            continue
        for f in o.fills:
            if math.isnan(f.fee):
                f.fee = 0.0  # Polymarket makers are never charged fees
    return unknown


def _kalshi_schedule(sp: PmScoreSpec, mk: dict[str, Any], warnings: list[str]) -> KalshiFeeSchedule:
    """The market's fee schedule: its series' `fee_type` and `fee_multiplier` from the capture; `flat_taker` /
    `flat_maker` make it a flat schedule (required for a flat series, `QT-CONFIG-001` without them); a market whose
    series was not captured gets the general quadratic schedule, with a warning."""
    kf = mk.get("kalshi_fee")
    meta = mk.get("meta") or {}
    flat = sp.flat_taker is not None or sp.flat_maker is not None
    if kf is None:
        ftype = meta.get("kalshi_fee_type") or ("flat" if flat else None)
        if ftype is None:
            warnings.append(
                f"fees: no series fee metadata for {mk['symbol']} (capture GET /series/{{series_ticker}}): the general "
                "quadratic schedule (multiplier 1, makers free) is assumed"
            )
        kf = {"fee_type": ftype or "quadratic", "fee_multiplier": "1", "fee_waiver_until_ns": None}
        waiver = meta.get("fee_waiver_until_ns")
    else:
        waiver = kf.get("fee_waiver_until_ns")
        if flat:
            warnings.append(
                f"fees: {mk['symbol']}'s series has fee_type {kf['fee_type']}; flat_taker / flat_maker override it"
            )
    return KalshiFeeSchedule(
        fee_type="flat" if flat else kf["fee_type"],
        fee_multiplier=Decimal(str(kf["fee_multiplier"])),
        flat_taker=Decimal(sp.flat_taker) if sp.flat_taker is not None else None,
        flat_maker=Decimal(sp.flat_maker) if sp.flat_maker is not None else None,
        fee_waiver_until_ns=waiver,
    )


def _kalshi_revenue(side: int, q: Decimal, p: Decimal, pos: Decimal) -> tuple[Decimal, Decimal]:
    """Signed cash flow of a Kalshi fill before fees and the new YES position: a buy closes a NO position at
    `1 - p` first, a sell closes a YES position at `p` first (Kalshi nets YES and NO)."""
    rev = Decimal(0)
    if side == BID:
        close = min(q, max(-pos, Decimal(0)))
        rev += close * (Decimal(1) - p)
        rev -= (q - close) * p
        return rev, pos + q
    close = min(q, max(pos, Decimal(0)))
    rev += close * p
    rev -= (q - close) * (Decimal(1) - p)
    return rev, pos - q


def _grid(mk: dict[str, Any]) -> PriceGrid:
    g = mk.get("grid")
    if g:
        return PriceGrid(tuple(tuple(int(x) for x in b) for b in g))
    return PriceGrid.uniform(int(mk["tick_size"]))


def _lip_programs(mk: dict[str, Any]) -> list[LipProgram]:
    out = []
    for g in mk.get("lip") or []:
        cap = g.get("max_reward_per_account")
        out.append(
            LipProgram(
                market_ticker=str(mk["symbol"]),
                start_ns=int(g["start_ns"]),
                end_ns=int(g["end_ns"]),
                period_reward=Decimal(g["period_reward"]),
                target_size=int(g["target_size"]),
                discount_factor=Decimal(g["discount_factor"]),
                max_reward_per_account=None if cap is None else Decimal(cap),
                program_id=str(g.get("program_id") or ""),
            )
        )
    return out


def _lip_eval(prog: LipProgram, grid: PriceGrid) -> Callable[..., tuple]:
    def ev(bids: list[tuple[int, int]], asks: list[tuple[int, int]], own: dict[int, dict[int, list[int]]]) -> tuple:
        ob = {p: sum(v) for p, v in own[BID].items()}
        oa = {p: sum(v) for p, v in own[ASK].items()}
        s, _inc = lip_snapshot(bids, asks, ob, oa, prog, grid)
        return (s,)

    return ev


def _poly_params(
    sp: PmScoreSpec, mk: dict[str, Any], mids: Mids, warnings: list[str]
) -> list[tuple[int, PolyRewardParams]]:
    meta = mk.get("meta") or {}
    spread, min_size = meta.get("rewards_max_spread"), meta.get("rewards_min_size")
    if spread is None or min_size is None:
        warnings.append(f"rewards: {mk['symbol']} has no rewards_max_spread / rewards_min_size (no liquidity rewards)")
        return []
    size_exp = int(mk["size_exp"])
    min_units = int(Decimal(str(min_size)).scaleb(-size_exp))
    days = range(mids.start // DAY_NS, mids.end // DAY_NS + 1) if mids.end else range(0)
    out = []
    for day in days:
        rate = None
        if sp.poly_daily_reward is not None:
            rate = Decimal(sp.poly_daily_reward)
        else:
            for r in mk.get("poly_rewards") or []:
                start = _date_days(r.get("start_date"))
                end = _date_days(r.get("end_date"))
                if r.get("daily_rate") is not None and (start is None or start <= day) and (end is None or day <= end):
                    rate = Decimal(str(r["daily_rate"]))
        if rate is None:
            continue
        out.append((day, PolyRewardParams(float(spread), min_units, rate, multiplier=sp.poly_multiplier)))
    if not out:
        warnings.append(f"rewards: {mk['symbol']} has no daily reward for the captured days (pass poly_daily_reward)")
    return out


def _date_days(v: Any) -> int | None:
    """Days since 1970-01-01 of a `YYYY-MM-DD` date (None when absent; `QT-INPUT-002` when malformed)."""
    if not v:
        return None
    try:
        return (datetime.date.fromisoformat(str(v)[:10]) - datetime.date(1970, 1, 1)).days
    except ValueError:
        raise QtError("QT-INPUT-002", f"reward date {v!r} is not YYYY-MM-DD") from None


def _poly_eval(params: PolyRewardParams) -> Callable[..., tuple]:
    def ev(bids: list[tuple[int, int]], asks: list[tuple[int, int]], own: dict[int, dict[int, list[int]]]) -> tuple:
        smp = poly_sample(bids, asks, own[BID], own[ASK], params)
        return smp.share()

    return ev


def _poly_rebate_pool(
    sp: PmScoreSpec, mk: dict[str, Any], pub: L2Arrays, warnings: list[str]
) -> tuple[float, float, PolymarketFeeSchedule] | None:
    """(market fee-equivalent of the captured prints, rebate pool in pUSD, schedule) or None without fees."""
    pf = mk.get("poly_fee")
    if pf:
        sched = PolymarketFeeSchedule(
            fees_enabled=bool(pf["fees_enabled"]),
            rate=Decimal(pf["rate"]),
            exponent=Decimal(pf["exponent"]),
            taker_only=bool(pf["taker_only"]),
            rebate_rate=Decimal(pf["rebate_rate"]),
        )
    elif sp.poly_category:
        sched = PolymarketFeeSchedule.for_category(sp.poly_category)
    else:
        return None
    if not sched.fees_enabled or sched.rate == 0 or sched.rebate_rate == 0:
        return None
    size_exp = int(mk["size_exp"])
    eq = taker = Decimal(0)
    for r in np.flatnonzero(pub.kind == K_TRADE):
        q = Decimal(int(pub.trade_qty[r])).scaleb(size_exp)
        p = Decimal(int(pub.price[r])).scaleb(-4)
        eq += sched.fee_equivalent(q, p)
        taker += sched.taker_fee(q, p)
    if not sched.documented:
        warnings.append(f"fees: {mk['symbol']} fee exponent {sched.exponent} is applied to p(1 - p) (undocumented)")
    return float(eq), float(taker * sched.rebate_rate), sched


def _markouts(
    sp: PmScoreSpec, orders: dict[str, list[OrderRow]], iid: int, size_exp: int, mids: Mids
) -> list[dict[str, Any]]:
    out = []
    for s in SCENARIOS:
        fills = [f for o in orders[s] if o.iid == iid for f in o.fills if f.mid_before is not None]
        for h in sp.markout_horizons_ns:
            tot, qty, n = 0.0, 0, 0
            for f in fills:
                m1 = mids.at(f.ts + h)
                if m1 is None or f.mid_before is None:
                    continue
                sign = 1 if f.side == BID else -1
                tot += sign * (f.mid_before - m1) * f.qty
                qty += f.qty
                n += 1
            out.append(
                {
                    "scenario": s,
                    "instrument_id": iid,
                    "horizon_ns": int(h),
                    "n": n,
                    "adverse_selection": _dollars(tot, 1, size_exp) if n else None,
                    "per_unit": (tot / qty / ONE) if qty else None,
                }
            )
    return out


def _parts(
    rows: list[OrderRow], d: PmDir, marks: dict[str, Any], rewards: dict[str, list[float]], rebates: float
) -> PnlParts:
    exps = {int(m["instrument_id"]): int(m["size_exp"]) for m in d.markets}
    syms = {int(m["instrument_id"]): str(m["symbol"]) for m in d.markets}
    sc = adv = 0.0
    fees = 0.0
    qty = 0.0
    pos = 0.0
    n = 0
    sc_ok = adv_ok = True
    for o in rows:
        mark = marks.get(syms[o.iid], {}).get("value")
        for f in o.fills:
            n += 1
            sign = 1.0 if f.side == BID else -1.0
            q = f.qty * 10.0 ** exps[o.iid]
            qty += q
            pos += sign * q
            fees += f.fee
            if f.mid_before is None:
                sc_ok = False
            else:
                sc += sign * (f.mid_before / ONE - f.price / ONE) * q
            if mark is None or f.mid_before is None:
                adv_ok = False
            else:
                adv += sign * (f.mid_before / ONE - mark) * q
    trading = (sc - adv) if (sc_ok and adv_ok) else _trading(rows, d, marks)
    rw = sum(rewards.get("point", []))
    total = None if trading is None else trading - fees + rw + rebates
    return PnlParts(
        fills=n,
        filled_qty=qty,
        spread_capture=sc if sc_ok else None,
        adverse_selection=adv if adv_ok else None,
        trading=trading,
        fees=fees,
        rewards=rw,
        rewards_lower=sum(rewards.get("lower", [])),
        rewards_upper=sum(rewards.get("upper", [])),
        rebates=rebates,
        total=total,
        position=pos,
    )


def _trading(rows: list[OrderRow], d: PmDir, marks: dict[str, Any]) -> float | None:
    exps = {int(m["instrument_id"]): int(m["size_exp"]) for m in d.markets}
    syms = {int(m["instrument_id"]): str(m["symbol"]) for m in d.markets}
    tot = 0.0
    for o in rows:
        mark = marks.get(syms[o.iid], {}).get("value")
        if mark is None and o.fills:
            return None
        for f in o.fills:
            sign = 1.0 if f.side == BID else -1.0
            tot += sign * (mark - f.price / ONE) * f.qty * 10.0 ** exps[o.iid]
    return tot


def _order_trading(o: OrderRow, mark: float | None, size_exp: int, net: bool = True) -> float | None:
    """An order's trading PnL `sum s (M - p) q` in dollars, net of its fees when `net` (None without a mark)."""
    if mark is None:
        return None if o.fills else 0.0
    return sum(
        (1.0 if f.side == BID else -1.0) * (mark - f.price / ONE) * f.qty * 10.0**size_exp - (f.fee if net else 0.0)
        for f in o.fills
    )


def _band(
    parts: dict[str, PnlParts], orders: dict[str, list[OrderRow]], d: PmDir, marks: dict[str, Any]
) -> dict[str, Band]:
    out: dict[str, Band] = {}
    for name in ("trading", "fees", "rewards", "rebates", "total", "spread_capture", "adverse_selection"):
        vals = [getattr(parts[s], name) for s in SCENARIOS]
        fin = [v for v in vals if v is not None]
        out[name] = Band(
            lower=min(fin) if fin else None, model=getattr(parts["model"], name), upper=max(fin) if fin else None
        )
    exps = {int(m["instrument_id"]): int(m["size_exp"]) for m in d.markets}
    syms = {int(m["instrument_id"]): str(m["symbol"]) for m in d.markets}
    for name, net in (("trading_bound", False), ("net_trading_bound", True)):
        lo = hi = 0.0
        ok = True
        for pe, op in zip(orders["pessimistic"], orders["optimistic"], strict=True):
            mark = marks.get(syms[pe.iid], {}).get("value")
            a, b = _order_trading(pe, mark, exps[pe.iid], net), _order_trading(op, mark, exps[op.iid], net)
            if a is None or b is None:
                ok = False
                break
            lo += min(a, b)
            hi += max(a, b)
        m_vals = [_order_trading(o, marks.get(syms[o.iid], {}).get("value"), exps[o.iid], net) for o in orders["model"]]
        out[name] = Band(
            lower=lo if ok else None,
            model=sum(v for v in m_vals if v is not None) if ok and all(v is not None for v in m_vals) else None,
            upper=hi if ok else None,
        )
    return out


def _first(o: OrderRow, until: int | None) -> int | None:
    ts = [f.ts for f in o.fills if until is None or f.ts <= until]
    return min(ts) if ts else None


def _calibration_subsets(choice: _ModelChoice, d: PmDir, all_legs: list[Leg]) -> dict[str, set[str]]:
    """The calibration's train and test legs (keys) when the model was fitted on this same capture, else {}."""
    cal = choice.calibration
    if cal is None or cal.get("pm_run_id") != d.summary.get("run_id"):
        return {}
    first: dict[str, int] = {}
    for g in all_legs:
        first[g.order_key] = min(first.get(g.order_key, g.entry_ts), g.entry_ts)
    split = cal.get("test_from")
    out: dict[str, set[str]] = {"train": set(), "test": set()}
    for g in all_legs:
        is_test = split is not None and (first[g.order_key], g.order_key) >= (int(split[0]), str(split[1]))
        out["test" if is_test else "train"].add(g.key)
    return out


def _sim_vs_live(
    orders: dict[str, list[OrderRow]],
    live: list[OrderRow],
    model: Model,
    sp: PmScoreSpec,
    d: PmDir,
    subsets: dict[str, set[str]] | None = None,
) -> list[SimVsLive]:
    """The model, `trade_through_only` and `front_of_queue` over every order, then the model over the calibration's
    train and test orders (`subsets`) when it was fitted on this capture."""
    plan: list[tuple[str, str, str, set[str] | None]] = [
        ("model", model.id, "all", None),
        ("pessimistic", "trade_through_only", "all", None),
        ("optimistic", "front_of_queue", "all", None),
    ]
    for name in ("train", "test"):
        if subsets and name in subsets:
            plan.append(("model", model.id, name, subsets[name]))
    return [_svl(orders[s], live, mid, which, keys, s, sp, d) for s, mid, which, keys in plan]


def _svl(
    rows: list[OrderRow],
    live: list[OrderRow],
    mid: str,
    which: str,
    keys: set[str] | None,
    s: str,
    sp: PmScoreSpec,
    d: PmDir,
) -> SimVsLive:
    by_key = {o.key: o for o in live}
    exps = {int(m["instrument_id"]): int(m["size_exp"]) for m in d.markets}
    tab = {"both": 0, "neither": 0, "live_only": 0, "sim_only": 0}
    lf, sf, ratios = [], [], []
    qty_l = qty_s = 0.0
    viol = 0
    for o in rows:
        lv = by_key.get(o.key)
        if lv is None or lv.ts_live is None or (keys is not None and o.key not in keys):
            continue
        until = lv.ts_live + sp.window_ns
        if lv.ts_end is not None:
            until = min(until, lv.ts_end)
        tl = _first(lv, until)
        ts_ = _first(o, until)
        live_f, sim_f = tl is not None, ts_ is not None
        key = (
            "both"
            if live_f and sim_f
            else "neither"
            if not (live_f or sim_f)
            else "live_only"
            if live_f
            else "sim_only"
        )
        tab[key] += 1
        lf.append(float(live_f))
        sf.append(float(sim_f))
        unit = 10.0 ** exps[o.iid]
        qty_l += sum(f.qty for f in lv.fills if f.ts <= until) * unit
        qty_s += sum(f.qty for f in o.fills if f.ts <= until) * unit
        if live_f and sim_f and tl > lv.ts_live and ts_ > lv.ts_live:
            ratios.append(math.log((ts_ - lv.ts_live) / (tl - lv.ts_live)))
        if s == "optimistic" and live_f and (not sim_f or ts_ > tl):
            viol += 1
        if s == "pessimistic" and sim_f and (not live_f or ts_ < tl):
            viol += 1
    n = len(lf)
    bias = (float(np.mean(sf)) - float(np.mean(lf))) if n else None
    ci: tuple[float | None, float | None] = (None, None)
    if n >= 2 and sp.bootstrap_b > 0:
        rng = bootstrap_rng(sp.seed, f"pm_score:svl:{s}" + ("" if which == "all" else f":{which}"))
        idx = rng.integers(0, n, size=(sp.bootstrap_b, n))
        arr_l, arr_s = np.asarray(lf), np.asarray(sf)
        ci = percentile_ci(arr_s[idx].mean(axis=1) - arr_l[idx].mean(axis=1))
    mc = mcnemar(tab["live_only"], tab["sim_only"])
    return SimVsLive(
        model_id=mid,
        orders=which,
        n_orders=n,
        both=tab["both"],
        neither=tab["neither"],
        live_only=tab["live_only"],
        sim_only=tab["sim_only"],
        fill_rate_live=float(np.mean(lf)) if n else None,
        fill_rate_sim=float(np.mean(sf)) if n else None,
        fill_rate_bias=bias,
        fill_rate_bias_ci=ci,
        mcnemar_p=mc.p_exact,
        qty_live=qty_l,
        qty_sim=qty_s,
        ttf_log_ratio_median=float(np.median(ratios)) if ratios else None,
        bracket_violations=viol,
    )


def _trading_ci(
    rows: list[OrderRow], d: PmDir, marks: dict[str, Any], sp: PmScoreSpec
) -> tuple[float | None, float | None]:
    exps = {int(m["instrument_id"]): int(m["size_exp"]) for m in d.markets}
    syms = {int(m["instrument_id"]): str(m["symbol"]) for m in d.markets}
    vals = [_order_trading(o, marks.get(syms[o.iid], {}).get("value"), exps[o.iid]) for o in rows]
    if len(vals) < 2 or any(v is None for v in vals) or sp.bootstrap_b <= 0:
        return (None, None)
    arr = np.asarray(vals, dtype=float)
    rng = bootstrap_rng(sp.seed, "pm_score:trading")
    reps = arr[rng.integers(0, arr.size, size=(sp.bootstrap_b, arr.size))].sum(axis=1)
    return percentile_ci(reps)


def _units_text(units: int, size_exp: int) -> str:
    return f"{Decimal(units).scaleb(size_exp).normalize():f}"


def _rewards_doc(
    d: PmDir, sp: PmScoreSpec, parts: dict[str, dict[str, list[float]]], programs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Expected reward per scenario (point, and the competition bounds on Polymarket) and the programs scored."""
    doc: dict[str, Any] = {
        "program": "kalshi_lip" if d.venue == "kalshi" else "polymarket_liquidity_rewards",
        "programs": programs,
        "by_scenario": {s: {k: sum(v) for k, v in sorted(parts[s].items())} for s in parts if parts[s]},
    }
    if sp.actual_rewards is not None:
        actual = float(Decimal(sp.actual_rewards))
        doc["actual"] = actual
        live = parts.get("live", {}).get("point")
        doc["live_minus_actual"] = (sum(live) - actual) if live else None
    return doc


def _write_tables(
    out: Path, orders: dict[str, list[OrderRow]], live: list[OrderRow], d: PmDir, marks: dict[str, Any]
) -> list[dict[str, Any]]:
    exps = {int(m["instrument_id"]): int(m["size_exp"]) for m in d.markets}
    syms = {int(m["instrument_id"]): str(m["symbol"]) for m in d.markets}
    fill_rows, order_rows = [], []
    for scen, rows in [*orders.items(), ("live", live)]:
        for o in rows:
            mark = marks.get(syms[o.iid], {}).get("value")
            order_rows.append(
                {
                    "scenario": scen,
                    "order_key": o.key,
                    "instrument_id": o.iid,
                    "side": o.side,
                    "price": o.price,
                    "qty": o.qty,
                    "ts_live": o.ts_live,
                    "ts_end": o.ts_end,
                    "end": o.end,
                    "filled_qty": o.filled,
                    "ts_first_fill": min((f.ts for f in o.fills), default=None),
                    "trading_pnl": _order_trading(o, mark, exps[o.iid], net=False),
                    "fees": float(sum(f.fee for f in o.fills)),
                }
            )
            for f in o.fills:
                fill_rows.append(
                    {
                        "scenario": scen,
                        "order_key": f.key,
                        "instrument_id": f.iid,
                        "ts": f.ts,
                        "side": f.side,
                        "price": f.price,
                        "qty": f.qty,
                        "liquidity": f.liquidity,
                        "reason": f.reason,
                        "mid_before": f.mid_before,
                        "fee": f.fee,
                    }
                )
    files = []
    for name, rows, fname in (
        ("qt.pm_score_fills", fill_rows, "pm_fills.parquet"),
        ("qt.pm_score_orders", order_rows, "pm_orders.parquet"),
    ):
        ent = write_table(table(name, rows), out / fname)
        files.append(ent)
    return files


__all__ = ["SCENARIOS", "Mids", "integrate", "read_quotes", "score", "steps_of_legs", "steps_of_orders"]
