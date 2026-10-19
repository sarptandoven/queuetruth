"""Schedule adjustments of the scorer: instruction-sized legs (audit ruling A8) and evaluation windows (ruling A9).

A8: `queuetruth.live.to_schedule` sizes a re-priced / upsized leg `<order_key>#<k>` as the new total qty minus the
*live* cumulative fills (SPEC 5.7.2). The scorer replaces that size by the instruction's total qty: the `qty` of the
`amend_ack` (exchange_ack entry, matched on `ts_exchange == leg ts_submit`) or `amend_sent` (send_plus_latency,
`ts_local_send`) event that started the leg; without such an event, the live-derived size plus the live cumulative
fills before the leg start (undoing the derivation). Amends of a resized leg shift by the same amount. The arm then
truncates each leg to `total_k - same-arm fills of earlier legs` (`outcomes.merge_legs`). `to_schedule` emits no leg
for a re-price whose new total the LIVE fills already reach (review F11), although the simulator may have filled
less; the scorer restores such a leg (`<order_key>.leg<n>`, a free index): an instruction event (as above) at the time
an earlier leg was cancelled, with no leg starting then, starts a leg at the instruction's price and total that ends
at the next leg's start (or the next re-price, or the order's terminal event; `restored_legs`). Current
`to_schedule` versions emit that leg themselves at the instruction total; the restoration then changes nothing.
The instruction event of a leg is the `amend_ack` or `restate` at its `ts_exchange` (exchange_ack entry) or the
`amend_sent` at its `ts_local_send` (send_plus_latency).

A9: both arms are evaluated on `W = [entry, min(cancel instruction, entry + H, end of data)]` with `H` = 60 s by
default; the cancel instruction time is the live `cancel_sent.ts_local_send`, else `cancel_ack.ts_exchange`.
"""

from __future__ import annotations

from typing import Any

from queuetruth.score._io import Row
from queuetruth.score.outcomes import Outcome

DEFAULT_WINDOW_NS = 60_000_000_000


def legs_by_order(schedule: list[Row]) -> dict[str, list[Row]]:
    """Scheduled legs per `order_key`, ordered by (`ts_submit`, file order)."""
    out: dict[str, list[tuple[int, int, Row]]] = {}
    for i, r in enumerate(schedule):
        out.setdefault(str(r.get("order_key") or r["shadow_id"]), []).append((int(r["ts_submit"]), i, r))
    return {k: [x[2] for x in sorted(v, key=lambda x: (x[0], x[1]))] for k, v in out.items()}


TERMINAL = ("cancel_ack", "expire", "done", "reject", "post_only_reject")


def _instruction_events(evs: list[Row], entry: str) -> list[tuple[int, Row]]:
    """(time, {px, qty}) of the acknowledged re-price / resize instructions of one order, in time order: `amend_ack` /
    `restate` at `ts_exchange` (exchange_ack entry), or an acknowledged `amend_sent` at `ts_local_send` with the
    ack's price / qty (send_plus_latency), as `to_schedule` times its legs."""
    out: list[tuple[int, Row]] = []
    sent: Row | None = None
    for e in evs:
        et = e["event_type"]
        if et == "amend_sent":
            sent = e
        elif et in ("amend_ack", "restate"):
            if entry == "exchange_ack":
                if e.get("ts_exchange") is not None:
                    out.append((int(e["ts_exchange"]), {"px": e.get("px"), "qty": e.get("qty")}))
            elif et == "amend_ack" and sent is not None and sent.get("ts_local_send") is not None:
                px = e.get("px") if e.get("px") is not None else sent.get("px")
                qty = e.get("qty") if e.get("qty") is not None else sent.get("qty")
                out.append((int(sent["ts_local_send"]), {"px": px, "qty": qty}))
                sent = None
    return sorted(out, key=lambda x: x[0])


def _terminal_time(evs: list[Row], after: int, entry: str) -> int | None:
    """Time of the order's first terminal event at or after `after` (`to_schedule`'s end rule)."""
    sent: Row | None = None
    for e in evs:
        et = e["event_type"]
        if et == "cancel_sent":
            sent = e
        if et not in TERMINAL:
            continue
        t = None
        if entry != "exchange_ack" and et == "cancel_ack" and sent is not None:
            t = sent.get("ts_local_send")
        if t is None:
            t = e.get("ts_exchange") if e.get("ts_exchange") is not None else e.get("ts_local_recv")
        if t is not None and int(t) >= after:
            return int(t)
    return None


def restored_legs(key: str, legs: list[Row], evs: list[Row], entry: str) -> list[Row]:
    """`legs` (one order, by start time) plus the re-priced legs `to_schedule` dropped because the live fills already
    reached the new total (module docstring). Restored legs copy the cancelled leg's other columns."""
    instr = _instruction_events(evs, entry)
    if not instr or not legs:
        return legs
    starts = sorted(int(x["ts_submit"]) for x in legs)
    ids = {str(x["shadow_id"]) for x in legs}
    out = list(legs)
    for leg in legs:
        tc = leg.get("ts_cancel")
        if tc is None or int(tc) in starts:
            continue
        t0 = int(tc)
        if not any(t == t0 for t, _ in instr):
            continue
        end = next((t for t in starts if t > t0), None)
        if end is None:
            end = _terminal_time(evs, t0, entry)
        px, qty = leg.get("price"), int(leg["qty"])
        cuts = [(t, e) for t, e in instr if t >= t0 and (end is None or t < end)]
        for i, (t, e) in enumerate(cuts):
            new_px = px if e.get("px") is None else int(e["px"])
            new_qty = qty if e.get("qty") is None else int(e["qty"])
            if i > 0 and new_px == px and new_qty <= qty:
                continue  # a priority-keeping change inside a restored leg: no new leg
            px, qty = new_px, new_qty
            nxt = next((u for u, _ in cuts[i + 1 :] if u > t), end)
            n = len(ids) + 1
            while f"{key}.leg{n}" in ids:
                n += 1
            sid = f"{key}.leg{n}"
            ids.add(sid)
            out.append(
                {
                    **leg,
                    "shadow_id": sid,
                    "price_mode": "abs",
                    "price": px,
                    "offset_ticks": 0,
                    "qty": qty,
                    "ts_submit": t,
                    "ts_cancel": nxt,
                    "order_key": key,
                }
            )
    return sorted(out, key=lambda x: (int(x["ts_submit"]), str(x["shadow_id"])))


def instruction_legs(
    schedule: list[Row], amends: list[Row], events: list[Row], live: dict[str, Outcome], entry: str
) -> tuple[list[Row], list[Row], dict[str, int]]:
    """(schedule rows with instruction-sized legs, including restored re-priced legs, shifted amend rows,
    `leg_totals` by shadow_id)."""
    ev_by: dict[str, list[Row]] = {}
    for e in events:
        ev_by.setdefault(str(e["order_key"]), []).append(e)
    am_by: dict[str, list[Row]] = {}
    for a in amends:
        am_by.setdefault(str(a["shadow_id"]), []).append(a)
    new_sched: list[Row] = []
    totals: dict[str, int] = {}
    delta: dict[str, int] = {}
    order: dict[str, float] = {str(r["shadow_id"]): float(i) for i, r in enumerate(schedule)}
    for key, legs0 in legs_by_order(schedule).items():
        evs = ev_by.get(key, [])
        legs = restored_legs(key, legs0, evs, entry)
        base = max(order[str(x["shadow_id"])] for x in legs0)
        restored = [str(r["shadow_id"]) for r in legs if str(r["shadow_id"]) not in order]
        for j, sid_r in enumerate(restored):
            order[sid_r] = base + (j + 1) / (len(restored) + 1)
        oc = live.get(key)
        for k, leg in enumerate(legs):
            sid = str(leg["shadow_id"])
            q = int(leg["qty"])
            total = q
            if k > 0 and sid not in restored:  # restored legs already carry the instruction total
                t0 = int(leg["ts_submit"])
                want, col = (
                    (("amend_ack", "restate"), "ts_exchange")
                    if entry == "exchange_ack"
                    else (("amend_sent",), "ts_local_send")
                )
                hit = next(
                    (
                        e
                        for e in evs
                        if e["event_type"] in want
                        and e.get(col) is not None
                        and int(e[col]) == t0
                        and e.get("qty") is not None
                    ),
                    None,
                )
                if hit is not None:
                    total = int(hit["qty"])
                else:
                    total = q + (sum(f.qty for f in oc.fills if f.ts < t0) if oc is not None else 0)
            delta[sid] = total - q
            r = dict(leg)
            r["qty"] = total
            new_sched.append(r)
            last = total
            for a in sorted(am_by.get(sid, []), key=lambda a: int(a["ts_send"])):
                last = int(a["new_qty"]) + delta[sid]
            totals[sid] = last
    new_amends = []
    for a in amends:
        r = dict(a)
        r["new_qty"] = int(a["new_qty"]) + delta.get(str(a["shadow_id"]), 0)
        new_amends.append(r)
    new_sched.sort(key=lambda r: order[str(r["shadow_id"])])
    return new_sched, new_amends, totals


def cancel_instruction(events: list[Row]) -> int | None:
    """Live cancel-instruction time: `cancel_sent.ts_local_send`, else `cancel_ack.ts_exchange`."""
    for et, col in (("cancel_sent", "ts_local_send"), ("cancel_ack", "ts_exchange")):
        for e in events:
            if e["event_type"] == et and e.get(col) is not None:
                return int(e[col])
    return None


def window_end(entry_ts: int, cancel_ts: int | None, end_of_data: int | None, window_ns: int) -> tuple[int, str]:
    """(end of W, what cut it: `cancel` | `horizon` | `end_of_data`)."""
    cands: list[tuple[int, int, str]] = [(entry_ts + int(window_ns), 1, "horizon")]
    if cancel_ts is not None:
        cands.append((max(cancel_ts, entry_ts), 0, "cancel"))
    if end_of_data is not None:
        cands.append((max(end_of_data, entry_ts), 2, "end_of_data"))
    t, _, why = min(cands)
    return t, why


def in_window(oc: Outcome | None, start: int, end: int) -> tuple[list[Any], list[Any]]:
    """(maker fills, taker fills) of an outcome inside `[start, end]`; `unknown` liquidity counts as maker."""
    if oc is None:
        return [], []
    w = [f for f in oc.fills if start <= f.ts <= end]
    return [f for f in w if f.liquidity != "taker"], [f for f in w if f.liquidity == "taker"]
