"""The P&L gap of a scorecard in one plain sentence (`qt.scorecard_summary.pnl_gap`, schema 1.2, additive).

The fill-rate gap leads `score`'s output (the `RESULT:` line, `verdict.headline`); this is the same answer in money.
It reads `qt.score_shortfall` (`queuetruth.score.shortfall`), so the gap is a number the decomposition table already
holds: at the horizon `h`, the per-order mean of `total_ccy = R_sim - R_live` (each arm's markout P&L of its fills in
the evaluation window, net of fees) over the valued orders, with the 95% percentile CI of the scorer's clustered
block bootstrap. Positive means the simulator's P&L is too high.

`h` is the scorecard's markout equivalence horizon (`equivalence.markout_horizon_ns`, 1 s by default) when it is a
markout horizon, else the closest markout horizon (the shorter one on a tie).

`direction`:

- `sim_too_high` / `sim_too_low`: the 95% CI excludes 0 (the only case stated as a finding);
- `no_significant_difference`: the CI contains 0, or there is no CI;
- `not_computable`: no order could be valued at `h`; `reason` says why (the unvalued orders' reasons with counts);
- `not_answerable`: a fills-only live log. The orders live never filled are not in it, so the fills the simulator
  grants on them are missing and the mean is biased towards "too low"; the numbers are kept, no finding is stated.

Parts: the shortfall buckets per order regrouped by what the simulator did, so that they add up to `mean_ccy` (to
floating-point rounding):

- `granted`: fills the simulator grants that live did not get: `missed_fill` of orders only the simulator filled,
  plus `partial_fill` of orders it filled more of than live;
- `denied`: fills live got that the simulator misses: `missed_fill` of orders only live filled, plus `partial_fill`
  of orders it filled less of than live;
- `timing`: `delay + spread + adverse_selection`, the matched units of orders both arms filled;
- `fees`.

With the scorer's order rows each part also gets a 95% CI on the same bootstrap draws as the total (stream
`score_shortfall:<currency>:<h>`, `shortfall.block_mean_cis`). A finding names a part ("mostly from ...") only when
it has the sign of the gap, its own CI excludes 0 and it is more than half of the sum of the absolute parts; parts
that offset one another name none. Without order rows (the report's fallback for older summaries) no part has a CI
and none is named.

Orders whose instrument is not in the dataset have no currency; that group is left out when another currency was
valued (the shortfall table's total rows count it).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from queuetruth.score._io import rows as _rows
from queuetruth.score.shortfall import block_mean_cis, bootstrap_args, stream_id
from queuetruth.score.statistics import block_labels

#: Parts in output order, with what each says about the simulator.
PARTS: tuple[tuple[str, str], ...] = (
    ("granted", "fills it grants that live did not get"),
    ("denied", "fills live got that it misses"),
    ("timing", "when and at what price it fills the orders live also filled"),
    ("fees", "fees and rebates"),
)
PART_WORDS: dict[str, str] = dict(PARTS)
DIRECTIONS = ("sim_too_high", "sim_too_low", "no_significant_difference", "not_computable", "not_answerable")
FILLS_ONLY_REASON = (
    "the live log holds fills but not the orders that never filled, so the fills the simulator grants on those "
    "orders are missing"
)
_FILL_AT = re.compile(r" \(fill at -?\d+\)$")


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


def _orders_word(n: int) -> str:
    return f"{n:,} order" + ("" if n == 1 else "s")


def part_values(r: Mapping[str, Any]) -> dict[str, float]:
    """The parts of one valued `qt.score_shortfall` order row (they add up to its `total_ccy`)."""
    missed = _f(r.get("missed_fill_ccy")) or 0.0
    partial = _f(r.get("partial_fill_ccy")) or 0.0
    live, sim = int(r.get("live_qty") or 0), int(r.get("sim_qty") or 0)
    return {
        "granted": (missed if live == 0 and sim > 0 else 0.0) + (partial if sim > live else 0.0),
        "denied": (missed if sim == 0 and live > 0 else 0.0) + (partial if sim < live else 0.0),
        "timing": math.fsum(_f(r.get(f"{c}_ccy")) or 0.0 for c in ("delay", "spread", "adverse_selection")),
        "fees": _f(r.get("fees_ccy")) or 0.0,
    }


def main_part(
    mean: float, parts: Mapping[str, float | None], cis: Mapping[str, tuple[float | None, float | None]]
) -> str | None:
    """The part a finding comes from: the sign of `mean`, its own 95% CI excludes 0, and more than half of the sum
    of the absolute parts; None when no part qualifies."""
    vals = {k: v for k, v in parts.items() if v is not None}
    total = math.fsum(abs(v) for v in vals.values())
    if not vals or mean == 0 or total == 0:
        return None
    k, v = max(vals.items(), key=lambda kv: abs(kv[1]))
    lo, hi = cis.get(k, (None, None))
    clear = lo is not None and hi is not None and (lo > 0 if mean > 0 else hi < 0)
    return k if v * mean > 0 and clear and abs(v) > 0.5 * total else None


def _reasons(unvalued: Sequence[Mapping[str, Any]]) -> str:
    """`no dataset quotes: 3; instrument not in the dataset: 1` from unvalued order rows."""
    n: dict[str, int] = {}
    for r in unvalued:
        why = _FILL_AT.sub("", str(r.get("note") or "unvalued").split("; ")[0])
        n[why] = n.get(why, 0) + 1
    return "; ".join(f"{k}: {v:,}" for k, v in sorted(n.items()))


def headline(g: Mapping[str, Any]) -> str:
    """The plain sentence of one `pnl_gap` entry (also its `headline` field, without a `BLOCKED:` prefix)."""
    at = f"at the {duration_text(int(g['horizon_ns']))} markout"
    cur = f" {g['currency']}" if g.get("currency") else ""
    n, nu = int(g.get("n_valued") or 0), int(g.get("n_unvalued") or 0)
    left = f"; {nu:,} unvalued order(s) left out" if nu else ""
    if g["direction"] == "not_computable":
        return f"P&L not computable {at}: no scored order could be valued ({g.get('reason') or 'unvalued'})"
    mean, lo, hi = g.get("mean_ccy"), g.get("ci_lo"), g.get("ci_hi")
    # one precision for the estimate and its CI, enough that a nonzero bound never prints as 0
    d = max(decimals(mean), decimals(lo), decimals(hi))
    ci = f"95% CI {money(lo, dec=d)} to {money(hi, dec=d)}" if lo is not None and hi is not None else "no CI"
    if g["direction"] == "not_answerable":
        return (
            f"P&L gap not answerable {at}: {g.get('reason') or FILLS_ONLY_REASON} (the simulator's P&L is "
            f"{money(mean, dec=d)}{cur} per order from live's, {ci}; {_orders_word(n)}{left})"
        )
    if g["direction"] == "no_significant_difference":
        why = "includes 0" if lo is not None and hi is not None else "so no difference can be shown"
        return (
            f"no P&L difference found {at}: the simulator's P&L is {money(mean, dec=d)}{cur} per order from live's "
            f"({ci} {why}; {_orders_word(n)}{left})"
        )
    word = "too high" if g["direction"] == "sim_too_high" else "too low"
    s = f"the simulator's P&L is {money(mean, signed=False, dec=d)}{cur} per order {word} ({ci}) {at}"
    part = g.get("main_part")
    if part:
        s += f", mostly from {PART_WORDS[part]} ({money((g.get('parts_ccy') or {}).get(part))}{cur} per order)"
    return s + f"; {_orders_word(n)}{left}"


def pnl_gap(
    shortfall_rows: Iterable[Mapping[str, Any]],
    horizon_ns: int,
    *,
    order_rows: Sequence[Mapping[str, Any]] | None = None,
    bootstrap: Any = None,
    fills_only: bool = False,
) -> list[dict[str, Any]]:
    """The `pnl_gap` entries (one per currency, sorted, no currency last) of `qt.score_shortfall` rows at the
    horizon `pick_horizon(<the table's horizons>, horizon_ns)`; [] for a table without rows. `order_rows` (the
    scorer's `qt.score_orders` rows, with `session_date` and `local_ns`) and `bootstrap` (the scorecard's
    `BootstrapSpec`) give the parts their CIs; `fills_only` marks a fills-only live log (`not_answerable`)."""
    rs = list(shortfall_rows)
    h = pick_horizon((int(r["horizon_ns"]) for r in rs), int(horizon_ns))
    if h is None:
        return []
    at_h = [r for r in rs if int(r["horizon_ns"]) == h]
    totals: dict[str | None, dict[str, Mapping[str, Any]]] = {}
    for r in at_h:
        if r.get("scope") == "total":
            totals.setdefault(r.get("currency"), {})[str(r.get("statistic"))] = r
    by_key = {str(o["order_key"]): o for o in order_rows or ()}
    b, block, seed = bootstrap_args(bootstrap)
    kind = block_labels(list(order_rows), block)[1] if order_rows else "day"  # chosen once, as the shortfall does
    groups: list[dict[str, Any]] = []
    for cur in sorted(totals, key=lambda c: (c is None, c or "")):
        t = totals[cur]
        orders = [r for r in at_h if r.get("scope") == "order" and r.get("currency") == cur]
        valued = [r for r in orders if r.get("valued")]
        mean = _f((t.get("mean") or {}).get("total_ccy"))
        lo = _f((t.get("ci_lo") or {}).get("total_ccy"))
        hi = _f((t.get("ci_hi") or {}).get("total_ccy"))
        per = [part_values(r) for r in valued]
        parts = {k: (math.fsum(p[k] for p in per) / len(per) if per else None) for k in PART_WORDS}
        cis: dict[str, tuple[float | None, float | None]] = dict.fromkeys(PART_WORDS, (None, None))
        scorer = [by_key.get(str(r.get("order_key"))) for r in valued]
        if valued and all(o is not None for o in scorer):
            got, _, _ = block_mean_cis(
                scorer,  # type: ignore[arg-type]
                {k: [p[k] for p in per] for k in PART_WORDS},
                b,
                kind,
                seed,
                stream_id(cur, h),
            )
            cis.update(got)
        reason = None
        if not valued or mean is None:
            direction = "not_computable"
            reason = _reasons([r for r in orders if not r.get("valued")]) or "no order"
        elif fills_only:
            direction, reason = "not_answerable", FILLS_ONLY_REASON
        elif lo is not None and hi is not None and lo > 0:
            direction = "sim_too_high"
        elif lo is not None and hi is not None and hi < 0:
            direction = "sim_too_low"
        else:
            direction = "no_significant_difference"
        finding = direction in ("sim_too_high", "sim_too_low") and mean is not None
        g: dict[str, Any] = {
            "horizon_ns": h,
            "currency": cur,
            "n_valued": len(valued),
            "n_unvalued": len(orders) - len(valued),
            "mean_ccy": mean,
            "ci_lo": lo,
            "ci_hi": hi,
            "direction": direction,
            "reason": reason,
            "parts_ccy": parts,
            "parts_ci_lo": {k: v[0] for k, v in cis.items()},
            "parts_ci_hi": {k: v[1] for k, v in cis.items()},
            "main_part": main_part(mean, parts, cis) if finding else None,  # type: ignore[arg-type]
        }
        g["headline"] = headline(g)
        groups.append(g)
    if any(g["currency"] is not None and g["n_valued"] for g in groups):
        groups = [g for g in groups if g["currency"] is not None]
    return groups


def pnl_gap_table(table: pa.Table, horizon_ns: int, **kw: Any) -> list[dict[str, Any]]:
    """`pnl_gap` of a `qt.score_shortfall` table, converting only the rows of the chosen horizon."""
    if table.num_rows == 0:
        return []
    h = pick_horizon(pc.unique(table["horizon_ns"]).to_pylist(), int(horizon_ns))
    return pnl_gap(_rows(table.filter(pc.equal(table["horizon_ns"], h))), int(horizon_ns), **kw)


__all__ = ["DIRECTIONS", "FILLS_ONLY_REASON", "PARTS", "PART_WORDS", "decimals", "duration_text", "headline",
           "main_part", "money", "part_values", "pick_horizon", "pnl_gap", "pnl_gap_table"]  # fmt: skip
