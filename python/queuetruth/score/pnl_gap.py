"""The P&L gap of a scorecard in one plain sentence (`qt.scorecard_summary.pnl_gap`, schema 1.2, additive).

The fill-rate gap leads `score`'s output (the `RESULT:` line, `verdict.headline`); this is the same answer in money.
It reads the total rows of `qt.score_shortfall` (`queuetruth.score.shortfall`), so every number is one the
decomposition table already holds: at the horizon `h`, the per-order mean of `total_ccy = R_sim - R_live` (each
arm's markout P&L of its fills in the evaluation window, net of fees) over the valued orders, with the 95% percentile
CI of the scorer's clustered block bootstrap. Positive means the simulator's P&L is too high.

`h` is the scorecard's markout equivalence horizon (`equivalence.markout_horizon_ns`, 1 s by default) when it is a
markout horizon, else the closest markout horizon (the shorter one on a tie).

A difference is stated as a finding only when its 95% CI excludes 0 (`direction` `sim_too_high` or `sim_too_low`);
a CI that contains 0, or no CI, is `no_significant_difference`, and an `h` with no valued order is
`not_computable`. A finding names the part it mostly comes from. The parts are the shortfall buckets per order,
regrouped by what the simulator did: the `missed_fill` bucket split by which arm filled (`granted`: orders the
simulator filled and live did not, `+R_sim`; `denied`: orders live filled and the simulator did not, `-R_live`),
`partial` (`partial_fill`), `timing` (`delay + spread + adverse_selection`, the matched units of orders both arms
filled) and `fees`. They are per-order means over the same valued orders, so they add up to `mean_ccy` (to
floating-point rounding); they carry no CI of their own. The named part is the one with the largest contribution
in the direction of the gap ("mostly from" when it is at least half of the gap, else "the largest part from").
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Parts in output order, with what each says about the simulator.
PARTS: tuple[tuple[str, str], ...] = (
    ("granted", "fills it grants that live did not get"),
    ("denied", "fills live got that it misses"),
    ("partial", "filling more or less of an order than live"),
    ("timing", "when and at what price it fills the orders live also filled"),
    ("fees", "fees and rebates"),
)
PART_WORDS: dict[str, str] = dict(PARTS)
DIRECTIONS = ("sim_too_high", "sim_too_low", "no_significant_difference", "not_computable")


def pick_horizon(horizons: Iterable[int], target: int) -> int | None:
    """`target` when it is one of `horizons`, else the closest (the shorter one on a tie); None without horizons."""
    hs = sorted(set(int(h) for h in horizons))
    if not hs:
        return None
    return min(hs, key=lambda h: (abs(h - target), h))


def duration_text(ns: int) -> str:
    """`1 s`, `50 ms`, `5 min` (the scorer's wording of a horizon)."""
    for unit, scale in (("min", 60 * 10**9), ("s", 10**9), ("ms", 10**6), ("us", 10**3)):
        if ns >= scale and (unit != "min" or ns % scale == 0):
            return f"{ns / scale:g} {unit}"
    return f"{ns} ns"


def decimals(x: float | None) -> int:
    """Decimals that show `x` per order: 2 from 1 up (and for 0 or None), else 3 significant digits."""
    if x is None or not math.isfinite(x) or x == 0 or abs(x) >= 1:
        return 2
    return max(2, 2 - math.floor(math.log10(abs(x))))


def money(x: float | None, signed: bool = True, dec: int | None = None) -> str:
    """An amount per order with `dec` decimals (default `decimals(x)`): `+1,234.50`, `+0.00457`."""
    if x is None or not math.isfinite(x):
        return "n/a"
    v = x if signed else abs(x)
    sign = "+" if signed and v > 0 else ""
    return f"{sign}{v:,.{decimals(x) if dec is None else dec}f}"


def _f(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None


def _parts(orders: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    """Per-order means of the parts over valued order rows (None without any)."""
    n = len(orders)
    if not n:
        return dict.fromkeys(PART_WORDS)
    acc: dict[str, list[float]] = {k: [] for k in PART_WORDS}
    for r in orders:
        missed = _f(r.get("missed_fill_ccy")) or 0.0
        live, sim = int(r.get("live_qty") or 0), int(r.get("sim_qty") or 0)
        acc["granted"].append(missed if live == 0 and sim > 0 else 0.0)
        acc["denied"].append(missed if sim == 0 and live > 0 else 0.0)
        acc["partial"].append(_f(r.get("partial_fill_ccy")) or 0.0)
        acc["timing"].append(math.fsum(_f(r.get(f"{c}_ccy")) or 0.0 for c in ("delay", "spread", "adverse_selection")))
        acc["fees"].append(_f(r.get("fees_ccy")) or 0.0)
    return {k: math.fsum(v) / n for k, v in acc.items()}


def main_part(mean: float, parts: Mapping[str, float | None]) -> tuple[str, bool] | None:
    """(the part with the largest contribution in the direction of `mean`, whether it is at least half of it)."""
    same = [(k, v) for k, v in parts.items() if v is not None and v * mean > 0]
    if not same or mean == 0:
        return None
    k, v = max(same, key=lambda kv: abs(kv[1]))
    return k, abs(v) >= 0.5 * abs(mean)


def headline(g: Mapping[str, Any]) -> str:
    """The plain sentence of one `pnl_gap` entry (also its `headline` field)."""
    at = f"at the {duration_text(int(g['horizon_ns']))} markout"
    cur = f" {g['currency']}" if g.get("currency") else ""
    n, nu = int(g.get("n_valued") or 0), int(g.get("n_unvalued") or 0)
    left = f"; {nu:,} unvalued order(s) left out" if nu else ""
    if g["direction"] == "not_computable":
        return f"P&L not computable {at}: no scored order could be valued (no quotes to mark its fills){left}"
    mean, lo, hi = g.get("mean_ccy"), g.get("ci_lo"), g.get("ci_hi")
    d = decimals(mean)  # the CI bounds with the estimate's decimals
    ci = f"95% CI {money(lo, dec=d)} to {money(hi, dec=d)}" if lo is not None and hi is not None else "no CI"
    if g["direction"] == "no_significant_difference":
        why = "includes 0" if lo is not None and hi is not None else "so no difference can be shown"
        return (
            f"no P&L difference found {at}: the simulator's P&L is {money(mean)}{cur} per order from live's "
            f"({ci} {why}; {n:,} orders{left})"
        )
    word = "too high" if g["direction"] == "sim_too_high" else "too low"
    s = f"the simulator's P&L is {money(mean, signed=False)}{cur} per order {word} ({ci}) {at}"
    part = g.get("main_part")
    if part:
        lead = "mostly from" if g.get("main_part_majority") else "the largest part from"
        s += f", {lead} {PART_WORDS[part]} ({money((g.get('parts_ccy') or {}).get(part))}{cur} per order)"
    return s + f"; {n:,} orders{left}"


def pnl_gap(shortfall_rows: Iterable[Mapping[str, Any]], horizon_ns: int) -> list[dict[str, Any]]:
    """The `pnl_gap` entries (one per currency, sorted, no currency last) of `qt.score_shortfall` rows at the
    horizon `pick_horizon(<the table's horizons>, horizon_ns)`; [] for a table without rows."""
    rs = list(shortfall_rows)
    h = pick_horizon((int(r["horizon_ns"]) for r in rs), int(horizon_ns))
    if h is None:
        return []
    at_h = [r for r in rs if int(r["horizon_ns"]) == h]
    totals: dict[str | None, dict[str, Mapping[str, Any]]] = {}
    for r in at_h:
        if r.get("scope") == "total":
            totals.setdefault(r.get("currency"), {})[str(r.get("statistic"))] = r
    out: list[dict[str, Any]] = []
    for cur in sorted(totals, key=lambda c: (c is None, c or "")):
        t = totals[cur]
        orders = [r for r in at_h if r.get("scope") == "order" and r.get("currency") == cur]
        valued = [r for r in orders if r.get("valued")]
        mean = _f((t.get("mean") or {}).get("total_ccy"))
        lo = _f((t.get("ci_lo") or {}).get("total_ccy"))
        hi = _f((t.get("ci_hi") or {}).get("total_ccy"))
        parts = _parts(valued)
        if not valued or mean is None:
            direction = "not_computable"
        elif lo is not None and hi is not None and lo > 0:
            direction = "sim_too_high"
        elif lo is not None and hi is not None and hi < 0:
            direction = "sim_too_low"
        else:
            direction = "no_significant_difference"
        mp = main_part(mean, parts) if direction in ("sim_too_high", "sim_too_low") and mean is not None else None
        g: dict[str, Any] = {
            "horizon_ns": h,
            "currency": cur,
            "n_valued": len(valued),
            "n_unvalued": len(orders) - len(valued),
            "mean_ccy": mean,
            "ci_lo": lo,
            "ci_hi": hi,
            "direction": direction,
            "parts_ccy": parts,
            "main_part": mp[0] if mp else None,
            "main_part_majority": mp[1] if mp else None,
        }
        g["headline"] = headline(g)
        out.append(g)
    return out


__all__ = ["DIRECTIONS", "PARTS", "PART_WORDS", "decimals", "duration_text", "headline", "main_part", "money",
           "pick_horizon", "pnl_gap"]  # fmt: skip
