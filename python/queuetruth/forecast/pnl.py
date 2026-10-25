"""Executable PnL of binary forecasts (edge truth): fractional-Kelly taker bets against a quote series.

Contracts pay 1 on YES. A YES bid at `b` is a NO offer at `1 - b`, so the bettor can buy YES at the ask or NO at
`1 - bid`, with the bid size as the NO depth. One candidate per scored (forecaster, binary question): the last
scored forecast `p`, decided at `t` (its ledger time). Updates of a position are never modelled.

1. The order arrives at `t + latency_ns` (at or after the question's information cutoff: no bet). With
   `price_at: decision` (the default) side, limit price and size come from the last quote at or before `t`, and the
   order is a marketable limit: it fills at the arrival quote's price when that is no worse than the limit
   (`price_moved` otherwise), at most `max_participation` of the arrival depth. With `price_at: arrival` they come
   from the last quote at or before the arrival, which models a bettor at the venue with no market-data delay and
   is more optimistic. No quote yet, or one older than `max_quote_age_ns` at the decision: no bet. Quotes after
   the arrival are never read.
2. Cost per contract `c = price + unit_fee(price)`, `unit_fee(P) = per_contract + notional_rate P +
   quadratic P (1 - P)`. The side with `p_side - c > min_edge` is bought (at most one side can have an edge).
3. Kelly fraction for a contract costing `c` that pays 1 with probability `p_side`: `f* = (p_side - c) / (1 - c)`.
   Stake `kelly_fraction f* equity`, in whole `lot`s (`lot = 0`: continuous), at most `max_participation` of the
   displayed depth at the touch and at most the cash on hand; the order's fee is then computed exactly (rounded up
   to `round_up_to`) and the bet is dropped if its expected value after that fee is not positive.
4. Settlement at `resolved_at_ns` pays `contracts` on a win.

The bankroll path processes arrivals and settlements in time order (settlements first on ties). Every position
that settles at one instant settles together, so the equity curve (cash plus open positions at cost, one point
per distinct settlement time) has no intermediate values that never existed, and drawdowns appear at settlement.
When many questions are open at once the path runs out of cash and only the first bets in ledger order are placed
(`path_cash_limited`: cash skipped or trimmed more than `CASH_LIMITED_SHARE` of the candidates); the path then
reflects that arbitrary subset. `isolated_growth` sizes every decision on a fresh bankroll and averages the log
returns over the decisions that met a quote (0 for no bet), so it measures the per-question edge regardless of
overlap. `frictionless_growth` is a full-Kelly bettor at the mid of the quote in force at `t`, with no spread,
fees, depth limit or latency: it multiplies wealth by `p / m` or `(1 - p) / (1 - m)`, so it equals the mean
log-score difference against that mid (the scorecard's log `mean_diff` against `baseline:market` on the same
questions).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from queuetruth.forecast.inference import mean_cluster_ci
from queuetruth.forecast.io import Quotes
from queuetruth.forecast.schemas import FeeSchedule, PnlResult, PnlSpec
from queuetruth.schemas.models import CiValue

SKIP_REASONS = (
    "no_quotes",
    "no_quote_yet",
    "stale_quote",
    "after_cutoff",
    "no_edge",
    "no_depth",
    "size_zero",
    "fees_exceed_edge",
    "insufficient_cash",
    "price_moved",
)
#: Share of candidates skipped or trimmed for cash above which `path_cash_limited` is set.
CASH_LIMITED_SHARE = 0.10


@dataclass(frozen=True)
class Candidate:
    """One scored binary forecast that may become a bet."""

    forecaster: str
    question_id: str
    p: float
    decided_at_ns: int
    cutoff_ns: int
    resolved_at_ns: int
    outcome: float
    cluster: int


def unit_fee(price: float, fees: FeeSchedule) -> float:
    """Fee per contract before rounding."""
    return fees.per_contract + fees.notional_rate * price + fees.quadratic * price * (1.0 - price)


def order_fee(contracts: float, price: float, fees: FeeSchedule) -> float:
    """Fee of one order of `contracts` at `price`, rounded up to a multiple of `round_up_to`."""
    raw = contracts * unit_fee(price, fees)
    r = fees.round_up_to
    if raw <= 0 or r <= 0:
        return max(raw, 0.0)
    return math.ceil(raw / r - 1e-9) * r


def _floor_lot(x: float, lot: float) -> float:
    if lot <= 0:
        return max(x, 0.0)
    return math.floor(x / lot + 1e-9) * lot


@dataclass
class _Decision:
    status: str
    quote_ts_ns: int | None = None
    decision_ts_ns: int | None = None
    exec_ns: int | None = None
    side: str | None = None
    price: float | None = None
    mid: float | None = None
    edge: float | None = None
    kelly: float | None = None
    contracts: float = 0.0
    fee: float = 0.0
    depth_limited: bool = False
    cash_limited: bool = False


def _quote(c: Candidate, quotes: dict[str, Quotes], spec: PnlSpec) -> tuple[str, int | None, int, Quotes | None]:
    """`(status, decision quote index, arrival time, quotes)`: the quote the decision is priced from."""
    q = quotes.get(c.question_id)
    t_exec = c.decided_at_ns + spec.latency_ns
    if q is None:
        return "no_quotes", None, t_exec, None
    if t_exec >= c.cutoff_ns:
        return "after_cutoff", None, t_exec, q
    t_dec = c.decided_at_ns if spec.price_at == "decision" else t_exec
    i = q.asof(t_dec)
    if i is None:
        return "no_quote_yet", None, t_exec, q
    if spec.max_quote_age_ns is not None and t_dec - int(q.ts[i]) > spec.max_quote_age_ns:
        return "stale_quote", i, t_exec, q
    return "ok", i, t_exec, q


def _side(q: Quotes, i: int, side: str) -> tuple[float, float]:
    """`(price, depth)` of buying `side` at quote `i` (NaN price when that side is empty)."""
    if side == "yes":
        return float(q.ask[i]), float(q.ask_depth[i])
    bid = float(q.bid[i])
    return (1.0 - bid if math.isfinite(bid) else math.nan), float(q.bid_depth[i])


def decide(c: Candidate, quotes: dict[str, Quotes], spec: PnlSpec, equity: float, cash: float) -> _Decision:
    """The order (or the reason for none) for one candidate given the current equity and cash."""
    status, i, t_exec, q = _quote(c, quotes, spec)
    if status != "ok" or q is None or i is None:
        return _Decision(status=status, exec_ns=t_exec, quote_ts_ns=None if i is None or q is None else int(q.ts[i]))
    d = _Decision(status="no_edge", exec_ns=t_exec, quote_ts_ns=int(q.ts[i]), decision_ts_ns=int(q.ts[i]))
    d.mid = q.mid(i)
    options = []
    for side, p_side in (("yes", c.p), ("no", 1.0 - c.p)):
        price, depth = _side(q, i, side)
        if math.isfinite(price):
            cost = price + unit_fee(price, spec.fees)
            options.append((side, price, cost, p_side - cost, depth))
    best = max(options, key=lambda o: o[3], default=None)
    if best is None or not best[3] > spec.min_edge or best[2] >= 1.0:
        if best is not None:
            d.edge = best[3]
        return d
    side, price, cost, edge, depth = best
    d.side, d.price, d.edge = side, price, edge
    d.kelly = edge / (1.0 - cost)
    if not math.isfinite(depth) or depth <= 0:
        d.status = "no_depth"
        return d
    n = _floor_lot(spec.kelly_fraction * d.kelly * equity / cost, spec.lot)
    cap = _floor_lot(spec.max_participation * depth, spec.lot)
    if n > cap:
        n, d.depth_limited = cap, True
    if spec.price_at == "decision":  # a marketable limit at `price`, filled against the arrival quote
        j = q.asof(t_exec)
        assert j is not None and j >= i
        fill, fill_depth = _side(q, j, side)
        d.quote_ts_ns = int(q.ts[j])
        if not math.isfinite(fill) or fill > price + 1e-12:
            d.status = "price_moved"
            return d
        if not math.isfinite(fill_depth) or fill_depth <= 0:
            d.status = "no_depth"
            return d
        cap = _floor_lot(spec.max_participation * fill_depth, spec.lot)
        if n > cap:
            n, d.depth_limited = cap, True
        price = fill
        cost = price + unit_fee(price, spec.fees)
        d.price = price
    p_side = c.p if side == "yes" else 1.0 - c.p
    fee = order_fee(n, price, spec.fees)
    if n * price + fee > cash + 1e-12:
        d.cash_limited = True
        if spec.lot > 0:
            n = _floor_lot(cash / cost, spec.lot)
            fee = order_fee(n, price, spec.fees)
            while n > 0 and n * price + fee > cash + 1e-12:
                n = _floor_lot(n - spec.lot, spec.lot)
                fee = order_fee(n, price, spec.fees)
        else:  # rounding adds at most one round_up_to to the fee
            n = max(0.0, (cash - spec.fees.round_up_to) / cost)
            fee = order_fee(n, price, spec.fees)
        if n <= 0:
            d.status = "insufficient_cash"
            return d
    if n <= 0:
        d.status = "size_zero"
        return d
    if n * (p_side - price) - fee <= 0:
        d.status = "fees_exceed_edge"
        return d
    d.status, d.contracts, d.fee = "bet", n, fee
    return d


@dataclass
class PnlRun:
    """`PnlResult` plus one row per candidate for `forecast_bets.parquet`."""

    result: PnlResult
    rows: list[dict[str, Any]] = field(default_factory=list)


def _decision_mid(c: Candidate, quotes: dict[str, Quotes], spec: PnlSpec) -> float | None:
    """Mid of the quote in force at the decision (None without one, or when it is older than `max_quote_age_ns`)."""
    q = quotes.get(c.question_id)
    if q is None or c.decided_at_ns >= c.cutoff_ns:
        return None
    i = q.asof(c.decided_at_ns)
    if i is None:
        return None
    if spec.max_quote_age_ns is not None and c.decided_at_ns - int(q.ts[i]) > spec.max_quote_age_ns:
        return None
    return q.mid(i)


def _log_ratio(p: float, m: float, y: float, eps: float) -> float:
    py = p if y == 1.0 else 1.0 - p
    my = m if y == 1.0 else 1.0 - m
    return math.log(max(py, eps)) - math.log(max(my, eps))


def run_pnl(
    candidates: list[Candidate],
    quotes: dict[str, Quotes],
    spec: PnlSpec,
    *,
    b: int = 1000,
    seed: int = 0,
    test_id: str = "pnl",
    alpha: float = 0.05,
    log_eps: float = 1e-6,
) -> PnlRun:
    """Runs the bankroll path and the isolated and frictionless views for one forecaster's candidates."""
    order = sorted(range(len(candidates)), key=lambda k: (candidates[k].decided_at_ns, candidates[k].question_id))
    cash = spec.bankroll
    open_pos: list[tuple[int, int, float, float, bool]] = []  # (resolved_at, idx, contracts, cost, wins)
    equity_curve = [spec.bankroll]
    skipped: dict[str, int] = dict.fromkeys(SKIP_REASONS, 0)
    rows: dict[int, dict[str, Any]] = {}
    fees_paid = spread_paid = contracts_total = 0.0
    depth_limited = cash_limited = wins = n_bets = 0

    def settle(until: int | None) -> None:
        """Settles every open position due by `until` (all of them for None), one equity point per instant."""
        nonlocal cash, wins
        open_pos.sort()
        while open_pos and (until is None or open_pos[0][0] <= until):
            t_settle = open_pos[0][0]
            while open_pos and open_pos[0][0] == t_settle:
                _, k, n, _cost, won = open_pos.pop(0)
                payout = n if won else 0.0
                cash += payout
                rows[k]["payout"] = payout
                wins += int(won)
            equity_curve.append(cash + sum(x[3] for x in open_pos))

    for k in order:
        c = candidates[k]
        t_exec = c.decided_at_ns + spec.latency_ns
        settle(t_exec)
        equity = cash + sum(x[3] for x in open_pos)
        d = decide(c, quotes, spec, equity, cash)
        row = {
            "forecaster": c.forecaster,
            "question_id": c.question_id,
            "p": c.p,
            "decided_at_ns": c.decided_at_ns,
            "exec_at_ns": d.exec_ns,
            "quote_ts_ns": d.quote_ts_ns,
            "decision_quote_ts_ns": d.decision_ts_ns,
            "status": d.status,
            "side": d.side,
            "price": d.price,
            "mid": d.mid,
            "edge": d.edge,
            "kelly_f": d.kelly,
            "contracts": d.contracts,
            "fee": d.fee,
            "cost": None,
            "payout": None,
            "outcome": c.outcome,
        }
        rows[k] = row
        if d.status != "bet":
            skipped[d.status] = skipped.get(d.status, 0) + 1
            continue
        assert d.price is not None and d.side is not None
        cost = d.contracts * d.price + d.fee
        row["cost"] = cost
        cash -= cost
        won = (c.outcome == 1.0) if d.side == "yes" else (c.outcome == 0.0)
        open_pos.append((c.resolved_at_ns, k, d.contracts, cost, won))
        n_bets += 1
        fees_paid += d.fee
        contracts_total += d.contracts
        if d.mid is not None:
            mid_side = d.mid if d.side == "yes" else 1.0 - d.mid
            spread_paid += d.contracts * (d.price - mid_side)
        depth_limited += int(d.depth_limited)
        cash_limited += int(d.cash_limited)
    settle(None)
    final = cash
    peak = -math.inf
    mdd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    log_growth = math.log(final / spec.bankroll) if final > 0 else -math.inf

    # isolated: each decision on a fresh bankroll; frictionless: full Kelly at the decision-time mid
    iso_vals: list[float] = []
    iso_cl: list[int] = []
    fric: list[float] = []
    for c in candidates:
        m = _decision_mid(c, quotes, spec)
        if m is not None:
            fric.append(_log_ratio(c.p, m, c.outcome, log_eps))
        status, i, _, q = _quote(c, quotes, spec)
        if status != "ok" or q is None or i is None:
            continue
        d = decide(c, quotes, spec, spec.bankroll, spec.bankroll)
        r = 0.0
        if d.status == "bet" and d.price is not None:
            cost = d.contracts * d.price + d.fee
            won = (c.outcome == 1.0) if d.side == "yes" else (c.outcome == 0.0)
            end = spec.bankroll - cost + (d.contracts if won else 0.0)
            r = math.log(end / spec.bankroll) if end > 0 else -math.inf
        iso_vals.append(r)
        iso_cl.append(c.cluster)
    if iso_vals:
        inv = np.unique(np.asarray(iso_cl), return_inverse=True)[1]
        est, lo, hi = mean_cluster_ci(np.asarray(iso_vals), inv, b, seed, test_id + "/isolated", alpha)
    else:
        est = lo = hi = float("nan")

    n_cash = skipped.get("insufficient_cash", 0) + cash_limited

    def fin(x: float) -> float | None:
        return float(x) if math.isfinite(x) else None

    result = PnlResult(
        n_candidates=len(candidates),
        n_bets=n_bets,
        n_skipped={k: v for k, v in skipped.items() if v},
        bankroll=spec.bankroll,
        final_wealth=final,
        total_return=final / spec.bankroll - 1.0,
        log_growth=fin(log_growth),
        growth_per_bet=fin(log_growth / n_bets) if n_bets else None,
        isolated_growth=CiValue(value=fin(est), ci_lo=fin(lo), ci_hi=fin(hi)),
        frictionless_growth=float(np.mean(fric)) if fric else None,
        max_drawdown=mdd,
        fees_paid=fees_paid,
        spread_paid=spread_paid,
        contracts=contracts_total,
        depth_limited=depth_limited,
        cash_limited=cash_limited,
        path_cash_limited=n_cash > CASH_LIMITED_SHARE * len(candidates),
        hit_rate=wins / n_bets if n_bets else None,
    )
    return PnlRun(result=result, rows=[rows[k] for k in range(len(candidates))])


__all__ = ["CASH_LIMITED_SHARE", "SKIP_REASONS", "Candidate", "PnlRun", "decide", "order_fee", "run_pnl", "unit_fee"]
