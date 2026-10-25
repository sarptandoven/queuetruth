"""The episode scorecard (`qt.sandbox_scorecard`): computed from one exact replay of every committed order on the
original files (the certification replay), never from the step-by-step windows.

- **Consistency**: every fill the session reported equals a row of the certification replay (all `qt.shadow_fills`
  columns), every certification fill knowable by the last visible cut was reported, at the step whose cut first
  covered its knowable time (`Order.known_ts`); and every order's shown states agree with the certification replay:
  it was first shown working at the step whose cut first covered its activation event, shown ended at the step whose
  cut first covered the event that decided its end (`decided_at`), with the replay's status, reason, end time and
  filled size. Exact when the data's timestamps never decrease and every event's records share one timestamp (the
  tape counts both); otherwise the attribution counts are reported as is.
- **PnL**: cash of every fill (fills while the final cancels were in flight included), the end position marked at
  the mid at `mark_ns` = max(finish, the last order's end), else the last print; fees per `FeeSchedule`; the
  conservative mark at the touch.
- **Markouts** with `queuetruth.score.markouts.compute_markouts` (mid reference, SPEC 5.7.5) on the certification
  replay's best quotes; **adverse selection** = minus the maker markouts; **fill rates** per order class over the
  orders that reached the venue; **execution cost** against the mid seen at decision time.
"""

from __future__ import annotations

import bisect
import heapq
import json
import shutil
import statistics
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.sandbox._util import digest, money
from queuetruth.sandbox.models import SandboxFinish, SandboxScorecard
from queuetruth.sandbox.orders import absorb, account, pnl
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.score._io import rows as table_rows
from queuetruth.score.markouts import compute_markouts

if TYPE_CHECKING:
    from queuetruth.sandbox.orders import Order
    from queuetruth.sandbox.session import Sandbox

CLASSES = ("passive_limit", "marketable_limit", "ioc_limit", "market")


def order_class(o: Order) -> str:
    """`market`, `ioc_limit`, `marketable_limit` (a day limit that took liquidity on arrival) or `passive_limit`."""
    if o.type == "market":
        return "market"
    if o.tif == "ioc":
        return "ioc_limit"
    if any(f["reason"] == "crossed_at_live" for f in o.fills):
        return "marketable_limit"
    return "passive_limit"


def _consistency(sb: Sandbox, final: dict[str, Any] | None) -> tuple[dict[str, Any], dict[tuple[str, int], int]]:
    """Compares the session's reported fills with the certification replay; returns the `Consistency` part and the
    step of every reported fill."""
    cuts = sb._cuts
    last_cut = cuts[-1][1] if cuts else sb.visible_ns
    reported: dict[tuple[str, int], tuple[dict[str, Any], int | None]] = {}
    for o in sb.orders:
        for f, step in zip(o.fills, o.fill_steps, strict=True):
            reported[(o.order_id, int(f["fill_seq"]))] = (f, step)
    matched = unreported = misattributed = after = 0
    cut_of = {step: v for step, v in cuts}
    prev_cut = {step: (cuts[k - 1][1] if k > 0 else None) for k, (step, _) in enumerate(cuts)}
    steps: dict[tuple[str, int], int] = {}
    if final is not None:
        for o in sb.orders:
            rows = sorted(final["fills"].get(o.order_id, []), key=lambda r: int(r["fill_seq"]))
            if o.aggressive:
                rows = [r for r in rows if r["reason"] == "crossed_at_live"]
            res = final["results"][o.order_id]
            act = res.get("ts_activated_event")
            for r in rows:
                key = (o.order_id, int(r["fill_seq"]))
                known = int(act) if r["reason"] == "crossed_at_live" and act is not None else int(r["ts_event"])
                rep = reported.get(key)
                if rep is None:
                    if known <= last_cut:
                        unreported += 1
                    else:
                        after += 1
                    continue
                f, step = rep
                if f == r:
                    matched += 1
                if step is not None:
                    steps[key] = step
                    lo = prev_cut.get(step)
                    hi = cut_of.get(step)
                    if hi is None or known > hi or (lo is not None and known <= lo):
                        misattributed += 1
    n_reported = len(reported)
    mono = sb.tape.cursor.ts_decreases == 0 and sb.tape.cursor.varying_events == 0
    doc = {
        "method": "single full exact replay of every committed order on the original partition files, compared "
        "fill by fill (all qt.shadow_fills columns) and order by order (the steps that showed it working and ended, "
        "and its end) with what the session reported step by step",
        "steps": sb.step,
        "window_replays": sb.window_replays,
        "fills_reported": n_reported,
        "fills_matched": matched,
        "fills_unreported": unreported,
        "fills_misattributed": misattributed,
        "fills_after_last_cut": after,
        "statuses_checked": 0,
        "statuses_mismatched": 0,
        "timestamps_monotone": mono,
        "equal": matched == n_reported and unreported == 0 and misattributed == 0,
    }
    return doc, steps


def decided_at(
    o: Order, res: dict[str, Any], traj_end: dict[str, Any] | None, first_event_after: Callable[[int], int | None]
) -> int | None:
    """Time of the event at which a replay decided the order's end, i.e. what a step's cut must cover for the
    session to show the order ended (None: not decided within the data). Fills, book clears and rejections at arrival
    end an order at their own event; cancels, reductions to the filled size and feed gaps take effect at the first
    event after their time (SPEC 5.2.1); IOC and market orders end at their activation event."""
    act = res.get("ts_activated_event")
    act = None if act is None else int(act)
    if o.aggressive:
        return act
    end = str(res["end_state"])
    if end == "filled":
        t = res.get("ts_full_fill")
        cands = [x for x in (None if t is None else int(t), act) if x is not None]
        return max(cands) if cands else None  # an arrival fill is stamped ts_live and known at activation
    if end in ("rejected_would_cross", "rejected_no_touch"):
        return act
    end_ts = None if traj_end is None or traj_end.get("ts_event") is None else int(traj_end["ts_event"])
    if end == "censored_clear":
        return end_ts
    if end == "cancelled":
        c = res.get("ts_cancel_effective")
        return None if c is None else first_event_after(int(c))
    if end in ("amended_to_filled", "censored_gap"):
        return None if end_ts is None else first_event_after(end_ts)
    return None  # open_at_end, censored_session_end: the data ended first


def _status_consistency(sb: Sandbox, final: dict[str, Any]) -> tuple[int, int, list[str]]:
    """(orders checked, orders whose shown states disagree with the certification replay, the first few of them):
    the step that first showed each order working and the step that showed it ended must be the first steps whose
    cuts covered its activation event and the event that decided its end, and the end shown must be the replay's
    (status, reason, end time, filled size). Run after the final `absorb`."""
    cuts = [c for _, c in sb._cuts]
    step_of = [k for k, _ in sb._cuts]

    def covering(t: int | None) -> int | None:
        if t is None:
            return None
        i = bisect.bisect_left(cuts, t)
        return step_of[i] if i < len(cuts) else None

    checked = 0
    bad: list[str] = []
    for o in sb.orders:
        res = final["results"][o.order_id]
        act = res.get("ts_activated_event")
        end_step = covering(decided_at(o, res, final["traj"].get(o.order_id), sb.book.first_event_after))
        act_step = None if o.aggressive or act is None else covering(int(act))
        work_step = act_step if act_step is not None and (end_step is None or act_step < end_step) else None
        ok = (o.working_step, o.final_step) == (work_step, end_step)
        if ok and o.final_step is not None:
            ok = o.shown_final == (o.status, o.reason, None if o.ts_done is None else int(o.ts_done), o.filled_qty)
        checked += 1
        if not ok:
            bad.append(o.order_id)
    return checked, len(bad), bad[:5]


def _bbo_rows(final: dict[str, Any] | None, out: Path) -> pa.Table | None:
    p = out / "bbo.parquet"
    if final is None or not p.exists():
        return None
    return pq.read_table(p)


def _last_quote(bbo: pa.Table | None, t: int) -> tuple[int | None, int | None]:
    if bbo is None or bbo.num_rows == 0:
        return None, None
    ts = bbo.column("ts_event").cast(pa.int64()).to_numpy()
    i = int(np.searchsorted(ts, t, side="right")) - 1
    if i < 0:
        return None, None
    b = bbo.column("bid_px")[i].as_py()
    a = bbo.column("ask_px")[i].as_py()
    return b, a


def _last_print(out: Path, t: int) -> int | None:
    p = out / "trades.parquet"
    if not p.exists():
        return None
    tr = pq.read_table(p, columns=["ts_event", "price"])
    if tr.num_rows == 0:
        return None
    ts = tr.column("ts_event").cast(pa.int64()).to_numpy()
    i = int(np.searchsorted(ts, t, side="right")) - 1
    return None if i < 0 else int(tr.column("price")[i].as_py())


def _markout_table(sb: Sandbox, fills: list[dict[str, Any]], bbo: pa.Table | None) -> pa.Table | None:
    if not fills or bbo is None or bbo.num_rows == 0:
        return None
    hs = sb.spec.markout_horizons_ns
    ts = bbo.column("ts_event").cast(pa.int64()).to_numpy()
    t_min = min(f["ts_event"] for f in fills)
    t_max = max(f["ts_event"] for f in fills) + max(hs)
    lo = max(0, int(np.searchsorted(ts, t_min, side="left")) - 1)
    hi = int(np.searchsorted(ts, t_max, side="right"))
    window = bbo.slice(lo, max(0, hi - lo))
    ftab = pa.table(
        {
            "order_key": [f["order_id"] for f in fills],
            "arm": ["sim"] * len(fills),
            "fill_seq": [f["fill_seq"] for f in fills],
            "ts": [f["ts_event"] for f in fills],
            "qty": [f["qty"] for f in fills],
            "px": [f["price"] for f in fills],
            "liquidity": [f["liquidity"] for f in fills],
            "side": [f["side"] for f in fills],
            "instrument_id": [sb.instrument["instrument_id"]] * len(fills),
            "fee": [f["fee"] for f in fills],
            "fee_ccy": [sb.instrument["currency"]] * len(fills),
        }
    )
    instruments = pq.read_table(sb.workdir / "view" / "instruments.parquet")
    last = {(sb.instrument["instrument_id"], sb.src.partition.session_date): sb.src.partition.ts_max}
    return compute_markouts(ftab, window, instruments, hs, ["mid"], last_event_ts=last)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _markout_rows(sb: Sandbox, fills: list[dict[str, Any]], mo: pa.Table | None) -> tuple[list, list]:
    liq = {(f["order_id"], f["fill_seq"]): f["liquidity"] for f in fills}
    by: dict[tuple[int, str], list[dict[str, Any]]] = {}
    if mo is not None:
        for r in table_rows(mo):
            key = (r["order_key"], int(r["fill_seq"]))
            for group in ("all", liq[key]):
                by.setdefault((int(r["horizon_ns"]), group), []).append(r)
    rows, adverse = [], []
    for h in sb.spec.markout_horizons_ns:
        for group in ("all", "maker", "taker"):
            rs = by.get((h, group), [])
            ok = [r for r in rs if r["mo_ticks"] is not None]
            rows.append(
                {
                    "horizon_ns": h,
                    "liquidity": group,
                    "n": len(rs),
                    "n_null": len(rs) - len(ok),
                    "mean_ticks": _mean([r["mo_ticks"] for r in ok]),
                    "mean_bps": _mean([r["mo_bps"] for r in ok if r["mo_bps"] is not None]),
                    "total_ccy": sum(r["mo_ccy"] for r in ok) if ok else None,
                    "n_adverse": sum(1 for r in ok if r["mo_ticks"] < 0),
                }
            )
        mk = [r for r in by.get((h, "maker"), []) if r["mo_ticks"] is not None]
        n_maker = sum(1 for f in fills if f["liquidity"] == "maker")
        adverse.append(
            {
                "horizon_ns": h,
                "n_maker_fills": n_maker,
                "cost_ccy": -sum(r["mo_ccy"] for r in mk) if mk else None,
                "mean_ticks": _mean([r["mo_ticks"] for r in mk]),
                "adverse_fraction": (sum(1 for r in mk if r["mo_ticks"] < 0) / len(mk)) if mk else None,
            }
        )
    return rows, adverse


def _fill_rates(orders: list[Order]) -> list[dict[str, Any]]:
    out = []
    for cls in (*CLASSES, "all"):
        os_ = [o for o in orders if cls == "all" or order_class(o) == cls]
        live = [o for o in os_ if o.ts_activated is not None and o.status != "rejected"]
        anyf = [o for o in live if o.filled_qty > 0]
        full = [o for o in live if o.filled_qty >= o.qty]
        qty_live = sum(o.qty for o in live)
        filled = sum(o.filled_qty for o in live)
        ttf = sorted(
            int(o.fills[0]["ts_event"]) - int(o.ts_activated)
            for o in anyf
            if order_class(o) == "passive_limit" and o.ts_activated is not None
        )
        out.append(
            {
                "order_class": cls,
                "n_orders": len(os_),
                "n_live": len(live),
                "n_any_fill": len(anyf),
                "n_full_fill": len(full),
                "qty_live": qty_live,
                "filled_qty": filled,
                "rate_any": len(anyf) / len(live) if live else None,
                "rate_full": len(full) / len(live) if live else None,
                "rate_qty": filled / qty_live if qty_live else None,
                "median_ttf_ns": int(statistics.median_low(ttf)) if ttf else None,
            }
        )
    return out


def overlap_counts(live: list[tuple[str, int, int, int]]) -> tuple[int, int]:
    """(same-price pairs, crossing pairs) among resting orders `(side, price, start, end)` whose intervals
    `[start, end)` overlap: two orders on one side at one price, and a buy and a sell with buy price >= sell price.
    A sweep in start order (each pair counted once, when its later-starting order starts), O(n log n) plus the
    active set, equal to comparing every pair."""
    same = cross = 0
    levels: dict[tuple[str, int], list[tuple[int, int]]] = {}
    for side, px, start, end in live:
        levels.setdefault((side, px), []).append((start, end))
    for ivs in levels.values():
        ivs.sort()
        ends: list[int] = []
        for start, end in ivs:
            while ends and ends[0] <= start:
                heapq.heappop(ends)
            same += len(ends)
            heapq.heappush(ends, end)
    active: dict[str, list[int]] = {"buy": [], "sell": []}
    expiry: list[tuple[int, int, str]] = []
    for side, px, start, end in sorted(live, key=lambda x: x[2]):
        while expiry and expiry[0][0] <= start:
            _, p, sd = heapq.heappop(expiry)
            prices = active[sd]
            del prices[bisect.bisect_left(prices, p)]
        if side == "buy":
            cross += bisect.bisect_right(active["sell"], px)
        else:
            cross += len(active["buy"]) - bisect.bisect_left(active["buy"], px)
        bisect.insort(active[side], px)
        heapq.heappush(expiry, (end, px, side))
    return same, cross


def _diagnostics(sb: Sandbox) -> dict[str, Any]:
    live = []
    for o in sb.orders:
        px = o.resolved_price if o.resolved_price is not None else o.price
        if o.aggressive or o.ts_activated is None or px is None:
            continue
        end = int(o.ts_done) if o.ts_done is not None else sb.src.partition.ts_max
        live.append((o.side, int(px), int(o.ts_activated), max(end, int(o.ts_activated) + 1)))
    same, cross = overlap_counts(live)
    groups: dict[tuple[str, int], int] = {}
    for o in sb.orders:
        if any(f["reason"] == "crossed_at_live" for f in o.fills) and o.ts_activated is not None:
            k = (o.side, int(o.ts_activated))
            groups[k] = groups.get(k, 0) + 1
    return {
        "same_price_overlaps": same,
        "taker_same_event_overlaps": sum(n - 1 for n in groups.values() if n > 1),
        "self_cross_pairs": cross,
        "out_of_session_orders": sum(1 for o in sb.orders if o.out_of_session),
        "rejected_instructions": sb.rejected,
    }


def build(sb: Sandbox, final: dict[str, Any] | None, *, finish_ns: int) -> dict[str, Any]:
    """The scorecard content (without the certificate, timings and files)."""
    if final is not None:
        missing = [o.order_id for o in sb.orders if o.order_id not in final["results"]]
        if missing:
            raise QtError("QT-ENGINE-001", f"orders without a row in the certification replay: {missing[:10]}")
    consistency, steps = _consistency(sb, final)
    mismatched_ids: list[str] = []
    if final is not None:
        for o in sb.orders:
            res = final["results"][o.order_id]
            o.fills, o.fill_steps = [], []
            absorb(
                o,
                res,
                final["fills"].get(o.order_id, []),
                final["traj"].get(o.order_id),
                final_replay=True,
                step=None,
            )
            o.fill_steps = [steps.get((o.order_id, int(f["fill_seq"]))) for f in o.fills]
            o.queue_ahead = o.rank = None
        checked, mismatched, mismatched_ids = _status_consistency(sb, final)
        consistency["statuses_checked"] = checked
        consistency["statuses_mismatched"] = mismatched
        consistency["equal"] = consistency["equal"] and mismatched == 0
    fills = []
    for o in sb.orders:
        for f, step in zip(o.fills, o.fill_steps, strict=True):
            fills.append(sb._fill_view(o, f, step))
    order_index = {o.order_id: o.index for o in sb.orders}
    fills.sort(key=lambda f: (f["ts_event"], order_index[f["order_id"]], f["fill_seq"]))
    u = sb._units()
    exp = u["price_exp"] + u["size_exp"]
    out = sb.workdir / "runs" / "final"
    bbo = _bbo_rows(final, out)
    # ---- pnl
    sign = {o.order_id: o.sign for o in sb.orders}
    acc = account([(sign[f["order_id"]], f["qty"], f["price"], f["liquidity"]) for f in fills], sb.spec.fees, **u)
    pos = acc.qty
    ends = [int(o.ts_done) for o in sb.orders if o.ts_done is not None]
    mark_ns = max([finish_ns, *ends])
    b, a = _last_quote(bbo, mark_ns)
    mark_x2, source = None, None
    if b is not None and a is not None:
        mark_x2, source = int(b) + int(a), "mid"
    else:
        lp = _last_print(out, mark_ns) if final is not None else None
        if lp is None:
            lp = sb._last_print_px
        if lp is not None:
            mark_x2, source = 2 * lp, "last_trade"
    gross = pnl(acc, mark_x2, **u)
    touch_px = None if pos == 0 else (b if pos > 0 else a)
    touch = pnl(acc, None if touch_px is None else 2 * int(touch_px), **u)
    pnl_doc = {
        "currency": sb.instrument["currency"],
        "position_end": pos,
        "bought_qty": acc.bought,
        "sold_qty": acc.sold,
        "cash": float(money(acc.cash_raw, exp, u["mult"])),
        "mark_ns": mark_ns,
        "mark_price": None if mark_x2 is None else float(Decimal(mark_x2).scaleb(u["price_exp"]) / 2),
        "mark_source": source,
        "pnl_gross": None if gross is None else float(gross),
        "fees": float(acc.fees),
        "fees_maker": float(acc.fees_maker),
        "fees_taker": float(acc.fees_taker),
        "pnl_net": None if gross is None else float(gross - acc.fees),
        "pnl_net_at_touch": None if touch is None else float(touch - acc.fees),
        "notional_traded": float(money(acc.notional_raw, exp, u["mult"])),
    }
    # ---- markouts
    mo = _markout_table(sb, fills, bbo)
    markouts, adverse = _markout_rows(sb, fills, mo)
    sb._final_markouts = mo
    # ---- execution cost
    dec_mid = {o.order_id: o.decision_mid_x2 for o in sb.orders}
    edges: dict[str, list[Decimal]] = {"maker": [], "taker": []}
    ticks = []
    for f in fills:
        m2 = dec_mid[f["order_id"]]
        if m2 is None:
            continue
        x2 = sign[f["order_id"]] * (m2 - 2 * f["price"])
        edges[f["liquidity"]].append(money(Decimal(x2 * f["qty"]) / 2, exp, u["mult"]))
        ticks.append(float(Decimal(x2) / (2 * sb.instrument["tick_size"])))
    n_edge = len(edges["maker"]) + len(edges["taker"])
    cost = {
        "n_fills": n_edge,
        "edge_ccy": float(sum(edges["maker"]) + sum(edges["taker"])) if n_edge else None,
        "edge_maker_ccy": float(sum(edges["maker"])) if edges["maker"] else None,
        "edge_taker_ccy": float(sum(edges["taker"])) if edges["taker"] else None,
        "mean_edge_ticks": _mean(ticks),
    }
    # ---- warnings
    warnings = []
    if not consistency["equal"]:
        warnings.append(
            "consistency: what the session reported differs from the single full replay "
            f"({consistency['fills_reported'] - consistency['fills_matched']} fill(s) changed, "
            f"{consistency['fills_unreported']} unreported, {consistency['fills_misattributed']} misattributed; "
            f"{consistency['statuses_mismatched']} order(s) shown in another state or at another step"
            + (f": {', '.join(mismatched_ids)}" if mismatched_ids else "")
            + ")"
        )
    if not consistency["timestamps_monotone"]:
        warnings.append(
            "the data has decreasing timestamps or events whose records differ in time: the step attribution of "
            "fills is approximate"
        )
    if sb.spec.latency_ns == 0:
        warnings.append("latency_ns = 0: orders and cancels arrived at the decision time (no order-entry delay)")
    if pos != 0:
        warnings.append(f"open position {pos} at the end, marked at the {source or 'unavailable'} price")
    diag = _diagnostics(sb)
    if diag["same_price_overlaps"] or diag["taker_same_event_overlaps"] or diag["self_cross_pairs"]:
        warnings.append(
            "your orders overlapped (they never see each other): "
            f"{diag['same_price_overlaps']} same-price pair(s), {diag['taker_same_event_overlaps']} taker fill(s) "
            f"in an event another of your orders also took, {diag['self_cross_pairs']} crossing pair(s)"
        )
    expired = sum(1 for o in sb.orders if o.status == "expired")
    if expired:
        warnings.append(f"{expired} order(s) were still open when the data ended (status expired)")
    censored = sum(1 for o in sb.orders if o.status == "censored")
    if censored:
        warnings.append(f"{censored} order(s) censored by a book clear or a feed gap: their outcome is unknown")
    dq = "not_replayed"
    if final is not None:
        dq = str(final["summary"].data_quality.verdict)
        if dq != "clean":
            warnings.append(f"data quality verdict of the replayed partition: {dq}")
    return {
        "schema": "qt.sandbox_scorecard",
        "schema_version": 1,
        "spec_id": sb.spec_id,
        "engine_version": sb.log.engine_version,
        "dataset_id": sb.dataset_id,
        "instrument": sb.instrument,
        "start_ns": sb.start_ns,
        "end_ns": sb.end_ns,
        "finish_ns": finish_ns,
        "steps": sb.step,
        "orders": [sb._order_view(o) for o in sb.orders],
        "fills": fills,
        "fill_rates": _fill_rates(sb.orders),
        "pnl": pnl_doc,
        "markouts": markouts,
        "adverse_selection": adverse,
        "execution_cost": cost,
        "consistency": consistency,
        "diagnostics": diag,
        "data_quality": dq,
        "warnings": warnings,
    }


#: Scorecard fields a `qt.sandbox_finish` summary carries as they are.
SUMMARY_FIELDS = (
    "run_id",
    "spec_id",
    "engine_version",
    "dataset_id",
    "instrument",
    "start_ns",
    "end_ns",
    "finish_ns",
    "steps",
    "pnl",
    "fill_rates",
    "markouts",
    "adverse_selection",
    "execution_cost",
    "consistency",
    "diagnostics",
    "data_quality",
    "warnings",
    "certificate",
    "files",
)


def finish_summary(
    card: SandboxScorecard, *, session_id: str | None, out_dir: str | None, detail: str = "summary"
) -> SandboxFinish:
    """The `qt.sandbox_finish` document of a scorecard: every aggregate and the certificate without the per-order and
    per-fill arrays (its size does not grow with the episode), plus the full scorecard when `detail = "full"`."""
    doc = card.model_dump(mode="json")
    body = {k: doc[k] for k in SUMMARY_FIELDS}
    body.update(
        {
            "schema": "qt.sandbox_finish",
            "schema_version": 1,
            "session_id": session_id,
            "n_orders": len(doc["orders"]),
            "n_fills": len(doc["fills"]),
            "n_fills_maker": sum(1 for f in doc["fills"] if f["liquidity"] == "maker"),
            "out_dir": out_dir,
            "scorecard": doc if detail == "full" else None,
        }
    )
    return SandboxFinish.model_validate(body)


def scorecard_digest(card: dict[str, Any]) -> str:
    """sha256 of the scorecard (canonical JSON) without `certificate`, `timings` and `files`."""
    return digest({k: v for k, v in card.items() if k not in ("certificate", "timings", "files")})


def certify(sb: Sandbox, card: dict[str, Any], *, timings: dict[str, float]) -> SandboxScorecard:
    """Adds the run id and the certificate (after the log's `finish` entry)."""
    from queuetruth.sandbox.session import _identity

    root = sb.log.root
    run_id = digest(
        {"engine_version": sb.log.engine_version, "spec": _identity(sb.spec, sb.dataset_id), "log_root": root}
    )[:16]
    body = dict(card)
    body["run_id"] = run_id
    sha = scorecard_digest(body)
    body["certificate"] = {
        "dataset_id": sb.dataset_id,
        "engine_version": sb.log.engine_version,
        "spec_id": sb.spec_id,
        "log_root": root,
        "run_id": run_id,
        "scorecard_sha256": sha,
    }
    body["timings"] = {k: round(v, 6) for k, v in sorted(timings.items())}
    body["files"] = []
    return SandboxScorecard.model_validate(body)


def write_outputs(sb: Sandbox, doc: SandboxScorecard, out: Path, final: dict[str, Any] | None) -> SandboxScorecard:
    """Writes `scorecard.json`, `order_log.json`, `markouts.parquet` (`qt.score_markouts`) and the certification
    replay's tables (`replay/`) under `out`; returns the scorecard with `files` listed."""
    out.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []

    def entry(p: Path, rows: int | None = None) -> None:
        files.append(
            {"name": p.relative_to(out).as_posix(), "rows": rows, "bytes": p.stat().st_size, "sha256": sha256_file(p)}
        )

    log_path = out / "order_log.json"
    log_path.write_text(json.dumps(sb.log.document().model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    entry(log_path)
    mo = sb._final_markouts
    if mo is not None:
        p = out / "markouts.parquet"
        write_table(mo, p)
        entry(p, mo.num_rows)
    if final is not None:
        src = sb.workdir / "runs" / "final"
        dst = out / "replay"
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        for name in sorted(x.name for x in src.iterdir() if x.is_file()):
            shutil.copy2(src / name, dst / name)
            rows = pq.read_metadata(dst / name).num_rows if name.endswith(".parquet") else None
            entry(dst / name, rows)
    body = doc.model_dump(mode="json")
    body["files"] = files
    card = SandboxScorecard.model_validate(body)
    (out / "scorecard.json").write_text(json.dumps(card.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return card


__all__ = [
    "CLASSES",
    "SUMMARY_FIELDS",
    "build",
    "certify",
    "decided_at",
    "finish_summary",
    "order_class",
    "overlap_counts",
    "scorecard_digest",
    "write_outputs",
]
