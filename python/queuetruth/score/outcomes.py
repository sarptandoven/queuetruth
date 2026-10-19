"""Per-order outcomes of each arm (SPEC 5.7.3, 5.7.4).

Live (and external-simulator) logs: fills are netted in `event_seq` order: `fill` adds a fill keyed by `fill_id`,
`fill_bust` removes the fill named by `ref_fill_id`, `fill_correct` replaces its qty/price (qty 0 removes it). A fill
is unique per (venue, instrument, side, fill_id): a repeated key (within or across orders) is dropped and counted;
the side is part of the key because a venue trade id is shared by both sides of a trade, so a self-trade between two
of the user's orders keeps both fills (review F10). A fill without a price keeps its quantity (fill status, filled
qty, time to fill) with `px = None` (counted in the `fills_without_px` warning; price-dependent metrics such as
markouts skip it; review F13); a fill without qty or time is ignored (`fills_incomplete`). The fill
time is `ts_exchange`, else `ts_local_recv`. `filled` = net filled qty > 0; the order qty is the largest total qty of
its life (`orig_qty`, amend acks). End: a reject without fills -> `rejected`; net filled >= order qty -> `filled`;
else `cancel_ack`/`done` -> `cancelled`, `expire` -> `expired`, else `open`.

Exact arm: shadow legs (`qt.shadow_results` + `qt.shadow_fills`) aggregated to `order_key`: filled qty = sum over
legs, first fill = earliest leg fill, fills merged by time. Fills with reason `crossed_at_live` (the book crossed the
order when it went live) are taker fills (audit ruling A5); a resting order crossed later (`crossed`) filled as maker
(A5 as refined by E48). Leg sizing follows the live instructions (ruling A8): when `leg_totals` gives the
instruction total qty of each leg, leg `k`'s fills are truncated (in time order) to `total_k - fills of legs < k` of
the same arm; the schedule passed to the engine carries the instruction totals, so the truncation is exact (a
smaller resting order at the same queue position fills a time-prefix of the larger one). Liquidity comes from the
fill's `liquidity` column when present, else `queuetruth.api.fill_liquidity(reason)` (`fill_liquidity` here).
Zoo arm: `qt.zoo_fills` of the model (quantities rounded
to integers), or, for models that write no fills (particle models), the `h_max` prediction (`p_fill >= 0.5`,
`e_fill_qty`, `model_ts_first_fill`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from queuetruth.errors import QtError
from queuetruth.score._io import Row

FILL_TYPES = ("fill", "fill_bust", "fill_correct")
CANCEL_END = ("cancelled", "expired")


@dataclass
class Fill:
    """One net fill."""

    ts: int
    qty: int
    #: None for a log fill reported without a price (quantity still counts; markouts skip it).
    px: int | None
    liquidity: str = "unknown"
    #: Fee of a log fill (positive = cost, negative = rebate) in `fee_ccy`; None when not reported.
    fee: float | None = None
    fee_ccy: str | None = None


@dataclass
class Outcome:
    """Outcome of one order in one arm."""

    order_key: str
    fills: list[Fill] = field(default_factory=list)
    end: str = "open"
    ts_end: int | None = None
    order_qty: int = 0
    matched: bool = True

    @property
    def filled_qty(self) -> int:
        """Net filled quantity."""
        return sum(f.qty for f in self.fills)

    @property
    def filled(self) -> bool:
        """Any net fill."""
        return self.filled_qty > 0

    @property
    def ts_first_fill(self) -> int | None:
        """Earliest fill time."""
        return min((f.ts for f in self.fills), default=None)


def _ts(ev: Row) -> int | None:
    for k in ("ts_exchange", "ts_local_recv", "ts_local_send"):
        v = ev.get(k)
        if v is not None:
            return int(v)
    return None


def log_outcomes(orders: list[Row], events: list[Row]) -> tuple[dict[str, Outcome], list[str]]:
    """Outcomes of every order of a canonical log (live or external simulator); returns (outcomes, warnings)."""
    info = {str(o["order_key"]): o for o in orders}
    by_order: dict[str, list[Row]] = {}
    for ev in events:
        by_order.setdefault(str(ev["order_key"]), []).append(ev)
    seen: set[tuple[str, str, str, str]] = set()
    dup = 0
    missing_qty = 0
    no_px = 0
    out: dict[str, Outcome] = {}
    for key in sorted(set(info) | set(by_order)):
        o = info.get(key, {})
        venue, inst = str(o.get("venue") or ""), str(o.get("instrument") or "")
        side = str(o.get("side") or "")
        qty = int(o.get("orig_qty") or 0)
        live: dict[str, Fill] = {}
        n_anon = 0
        flags = set()
        term: dict[str, int] = {}
        for ev in by_order.get(key, []):
            et = str(ev["event_type"])
            flags.add(et)
            if et == "amend_ack" and ev.get("qty") is not None:
                qty = max(qty, int(ev["qty"]))
            if et == "fill":
                if ev.get("qty") is None:
                    missing_qty += 1
                    continue
                fid = ev.get("fill_id")
                if fid is None:
                    n_anon += 1
                    fid = f"#anon{n_anon}"
                else:
                    gk = (venue, inst, side, str(fid))
                    if gk in seen:
                        dup += 1
                        continue
                    seen.add(gk)
                t = _ts(ev)
                if t is None:
                    missing_qty += 1
                    continue
                if ev.get("px") is None:
                    no_px += 1
                fee = ev.get("fee")
                live[str(fid)] = Fill(
                    t,
                    int(ev["qty"]),
                    None if ev.get("px") is None else int(ev["px"]),
                    str(ev.get("liquidity") or "unknown"),
                    None if fee is None or not math.isfinite(float(fee)) else float(fee),
                    None if ev.get("fee_ccy") is None else str(ev["fee_ccy"]),
                )
            elif et == "fill_bust":
                live.pop(str(ev.get("ref_fill_id")), None)
            elif et == "fill_correct":
                ref = str(ev.get("ref_fill_id"))
                if ref in live:
                    q = ev.get("qty")
                    if q is not None and int(q) == 0:
                        live.pop(ref)
                    else:
                        f0 = live[ref]
                        fee = ev.get("fee")
                        live[ref] = Fill(
                            f0.ts,
                            f0.qty if q is None else int(q),
                            f0.px if ev.get("px") is None else int(ev["px"]),
                            f0.liquidity,
                            f0.fee if fee is None or not math.isfinite(float(fee)) else float(fee),
                            f0.fee_ccy if ev.get("fee_ccy") is None else str(ev["fee_ccy"]),
                        )
            elif et in ("cancel_ack", "expire", "done", "reject", "post_only_reject"):
                t = _ts(ev)
                if t is not None:
                    term.setdefault(et, t)
        fills = sorted(live.values(), key=lambda f: (f.ts, f.qty, f.px is None, f.px or 0))
        oc = Outcome(order_key=key, fills=fills, order_qty=max(qty, sum(f.qty for f in fills)))
        if fills and oc.filled_qty >= oc.order_qty and oc.order_qty > 0:
            oc.end, oc.ts_end = "filled", max(f.ts for f in fills)
        elif not fills and flags & {"reject", "post_only_reject"}:
            oc.end, oc.ts_end = "rejected", term.get("reject", term.get("post_only_reject"))
        elif "cancel_ack" in flags or "done" in flags:
            oc.end, oc.ts_end = "cancelled", term.get("cancel_ack", term.get("done"))
        elif "expire" in flags:
            oc.end, oc.ts_end = "expired", term.get("expire")
        out[key] = oc
    warnings = []
    if dup:
        warnings.append(f"fill_dedupe: {dup} fill(s) with a repeated (venue, instrument, side, fill_id) dropped")
    if missing_qty:
        warnings.append(f"fills_incomplete: {missing_qty} fill event(s) without qty or timestamp ignored")
    if no_px:
        warnings.append(
            f"fills_without_px: {no_px} fill(s) without a price kept for fill status and quantity; left out of "
            "price-dependent outputs (score_fills, markouts, position)"
        )
    return out, warnings


def _leg_map(schedule: list[Row]) -> dict[str, str]:
    return {str(s["shadow_id"]): str(s.get("order_key") or s["shadow_id"]) for s in schedule}


#: Fallback taker reasons when neither a `liquidity` column nor the engine mapping is available (A5 refinement:
#: only `crossed_at_live`; a resting order crossed later is a maker fill).
TAKER_REASONS = ("crossed_at_live",)
_LIQ: dict[str, str] = {}


def fill_liquidity(f: Row) -> str:
    """`maker` | `taker` of an engine fill row (ruling A5): its `liquidity` column when present (`qt.shadow_fills`
    1.1), else the engine's own reason mapping `queuetruth.api.fill_liquidity`, else `TAKER_REASONS`."""
    liq = f.get("liquidity")
    if liq in ("maker", "taker"):
        return str(liq)
    reason = str(f.get("reason"))
    if reason not in _LIQ:
        try:
            from queuetruth import api

            got = str(api.fill_liquidity(reason))
        except (ImportError, AttributeError, QtError):  # no compiled extension, or a reason it does not know
            got = ""
        _LIQ[reason] = got if got in ("maker", "taker") else ("taker" if reason in TAKER_REASONS else "maker")
    return _LIQ[reason]


def merge_legs(per_leg: dict[str, list[Fill]], leg_seq: list[str], leg_totals: dict[str, int] | None) -> list[Fill]:
    """Fills of one order's legs in leg order, each leg truncated to its instruction total minus the earlier legs'
    fills (no truncation without `leg_totals`), merged by time."""
    out: list[Fill] = []
    cum = 0
    for sid in leg_seq:
        fl = sorted(per_leg.get(sid, []), key=lambda f: f.ts)
        if leg_totals is not None and sid in leg_totals:
            cap = max(0, int(leg_totals[sid]) - cum)
            kept: list[Fill] = []
            used = 0
            for f in fl:
                if used >= cap:
                    break
                q = min(f.qty, cap - used)
                kept.append(replace(f, qty=q))
                used += q
            fl = kept
        cum += sum(f.qty for f in fl)
        out.extend(fl)
    out.sort(key=lambda f: f.ts)
    return out


def exact_outcomes(
    schedule: list[Row],
    results: list[Row],
    fills: list[Row],
    order_qty: dict[str, int],
    leg_totals: dict[str, int] | None = None,
) -> tuple[dict[str, Outcome], dict[str, Row]]:
    """Exact-arm outcomes by `order_key` and the first leg's `qt.shadow_results` row per order."""
    legs = _leg_map(schedule)
    leg_order = {str(s["shadow_id"]): i for i, s in enumerate(schedule)}
    res_by_order: dict[str, list[Row]] = {}
    for r in results:
        sid = str(r["shadow_id"])
        res_by_order.setdefault(legs.get(sid, sid), []).append(r)
    per_leg: dict[str, list[Fill]] = {}
    for f in sorted(fills, key=lambda f: (str(f["shadow_id"]), int(f["fill_seq"]))):
        liq = fill_liquidity(f)
        per_leg.setdefault(str(f["shadow_id"]), []).append(
            Fill(int(f["ts_event"]), int(f["fill_qty"]), int(f["fill_px"]), liq)
        )
    out: dict[str, Outcome] = {}
    first: dict[str, Row] = {}
    for key, rs in res_by_order.items():
        rs.sort(key=lambda r: leg_order.get(str(r["shadow_id"]), 0))
        first[key] = rs[0]
        fl = merge_legs(per_leg, [str(r["shadow_id"]) for r in rs], leg_totals)
        qty = order_qty.get(key, max(int(r["qty"]) for r in rs))
        oc = Outcome(order_key=key, fills=fl, order_qty=qty)
        last = rs[-1]
        state = str(last["end_state"])
        never_live = all(r.get("ts_activated_event") is None for r in rs)
        if never_live and all(str(r["end_state"]) == "censored_gap" for r in rs):
            oc.matched = False
        if fl and oc.filled_qty >= qty:
            oc.end, oc.ts_end = "filled", max(f.ts for f in fl)
        elif state in ("cancelled", "amended_to_filled"):
            oc.end, oc.ts_end = "cancelled", last.get("ts_cancel_effective")
        elif state.startswith("rejected"):
            oc.end = "rejected"
        else:
            oc.end = "open"
        out[key] = oc
    return out, first


def _round_qty(x: float) -> int:
    return math.floor(x + 0.5 + 1e-9) if math.isfinite(x) and x > 0 else 0


def zoo_outcomes(
    model_id: str,
    schedule: list[Row],
    predictions: list[Row],
    fills: list[Row],
    order_qty: dict[str, int],
    exact: dict[str, Outcome] | None = None,
    leg_totals: dict[str, int] | None = None,
) -> tuple[dict[str, Outcome], dict[str, Row]]:
    """Zoo-arm outcomes by `order_key` and the first leg's longest-horizon prediction row per order."""
    legs = _leg_map(schedule)
    leg_order = {str(s["shadow_id"]): i for i, s in enumerate(schedule)}
    price = {str(s["shadow_id"]): s.get("price") for s in schedule}
    preds = [p for p in predictions if str(p["model_id"]) == model_id] or list(predictions)
    hmax: dict[str, Row] = {}
    for p in preds:
        sid = str(p["shadow_id"])
        if sid not in hmax or int(p["horizon_ns"]) > int(hmax[sid]["horizon_ns"]):
            hmax[sid] = p
    zf = [f for f in fills if str(f["model_id"]) == model_id] or list(fills)
    with_fills = {str(f["shadow_id"]) for f in zf}
    per_leg: dict[str, list[Fill]] = {}
    for f in sorted(zf, key=lambda f: (str(f["shadow_id"]), int(f["fill_seq"]))):
        sid = str(f["shadow_id"])
        q = _round_qty(float(f["fill_qty"]))
        if q <= 0:
            continue
        liq = fill_liquidity(f)
        per_leg.setdefault(sid, []).append(Fill(int(f["ts_event"]), q, int(price.get(sid) or 0), liq))
    for sid, p in hmax.items():
        if sid in with_fills:
            continue
        q = _round_qty(float(p["e_fill_qty"])) if float(p["p_fill"]) >= 0.5 else 0
        t = p.get("model_ts_first_fill")
        if q > 0 and t is not None:
            per_leg.setdefault(sid, []).append(Fill(int(t), q, int(price.get(sid) or 0), "maker"))
    sids_by_order: dict[str, list[str]] = {}
    for sid in sorted(set(hmax) | set(per_leg), key=lambda x: leg_order.get(x, 0)):
        sids_by_order.setdefault(legs.get(sid, sid), []).append(sid)
    keys = sorted(sids_by_order)
    out: dict[str, Outcome] = {}
    first: dict[str, Row] = {}
    for key in keys:
        sids = sorted((s for s in hmax if legs.get(s, s) == key), key=lambda s: leg_order.get(s, 0))
        if sids:
            first[key] = hmax[sids[0]]
        fl = merge_legs(per_leg, sids_by_order[key], leg_totals)
        qty = order_qty.get(key, 0)
        oc = Outcome(order_key=key, fills=fl, order_qty=qty)
        ex = (exact or {}).get(key)
        if fl and oc.filled_qty >= qty > 0:
            oc.end, oc.ts_end = "filled", max(f.ts for f in fl)
        elif ex is not None and ex.end in ("cancelled", "rejected"):
            oc.end, oc.ts_end = ex.end, ex.ts_end
        out[key] = oc
    return out, first


def order_rows(outcomes: Iterable[Outcome]) -> dict[str, Any]:
    """Debug view: order_key -> (filled_qty, first fill, end)."""
    return {o.order_key: (o.filled_qty, o.ts_first_fill, o.end) for o in outcomes}
