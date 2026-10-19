"""Deliberately simple shadow simulator for O-REF-1 (test-only; written from SPEC 5.2, not from engine code) with
switchable bugs reproducing the referee bug classes of SPEC 5.8. Reads a dataset directory (SPEC 3.3) and a
`qt.shadow_spec` schedule (abs prices, zero LatencyModel) and returns a `qt.shadow_fills`-like table.

The clean model is the volume-driven reading of SPEC 5.2.4/5.2.5 that third-party simulators use (queue ahead from
pure cancels and priority losses of the orders ahead, fills once the volume traded at the price exceeds it), made
consistent with the exact rule's audit rulings: the traded volume is read from executions (`F` of an order resting
at the shadow's price, at its displayed price, or of an order ahead at any price; A4), trade-throughs from
executions of orders resting at a worse price (A4: off-price prints are not trade-throughs), Nasdaq side-`N`
prints per A3 (at the price only without displayed interest there, through the price only while the shadow is at
or inside its best), CME same-size modifies lose priority (A6) and crosses take the opposite displayed size as a
taker, FIFO, never twice (A5)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

LAST = 1 << 7
BUGS = (
    "unrelated_delete_to_front",  # 1  NT #4370
    "no_cap_on_shrink",  # 2  NT #5016
    "one_trade_per_event",  # 3  NT #5017: a second print with the same (ts_event, price) is not counted
    "pre_trade_book",  # 4  NT #3596
    "last_chunk_only",  # 5  hbt #316
    "exact_leaves_dropped",  # 6  hbt #312
    "upsize_keeps_priority",  # 7
    "reprice_keeps_priority",  # 8
    "hidden_consumes_queue",  # 9
    "no_trade_through",  # 10
    "cancel_at_send",  # 11
    "ignore_hidden_prints",  # 12 A3
    "print_driven_queue",  # 13 A4
    "off_price_print_trade_through",  # 14 A4
    "cme_modify_keeps_priority",  # 15 A6
    "crossed_fills_at_limit",  # 16 A5
)


def _events(root: Path) -> tuple[list[dict], str]:
    man = json.loads((root / "manifest.json").read_text())
    recs: list[dict] = []
    for p in man["partitions"]:
        for f in p["files"]:
            t = pq.read_table(root / f["path"])
            t = t.set_column(0, "ts_event", t["ts_event"].cast(pa.int64()))
            recs.extend(
                t.select(
                    ["ts_event", "instrument_id", "action", "side", "price", "size", "order_id", "flags"]
                ).to_pylist()
            )
    return recs, str(man.get("venue", "generic"))


class _Shadow:
    def __init__(self, s: dict, bugs: set[str]) -> None:
        self.id = s["shadow_id"]
        self.iid = s["instrument_id"]
        self.side = s["side"]
        self.px = s["price"]
        self.qty = s["qty"]
        lat = int(s.get("latency_ns") or 0)
        self.t_live = int(s["ts_submit"]) + lat
        tc = s.get("ts_cancel")
        self.t_cancel = None if tc is None else int(tc) + (0 if "cancel_at_send" in bugs else lat)
        self.state = "pending"
        self.ahead: set[int] = set()
        self.ahead_qty = 0
        self.traded = 0
        self.filled = 0
        self.fills: list[tuple[int, int]] = []
        self.taken: dict[int, int] = {}
        self.last_trade: tuple[int, int] | None = None  # (ts_event, price) of the last print (NT #5017 bug)
        self.inside_at_start = False
        self.reset()

    def reset(self) -> None:
        self.removed: dict[int, int] = {}
        self.ffill: dict[int, int] = {}
        self.ev_traded = 0
        self.ev_crossed = 0
        self.dup_trade = False
        self.hidden_at = 0
        self.hidden_through = 0


def simulate(dataset: str | Path, schedule: list[dict], bugs: set[str] | None = None) -> pa.Table:
    """Fills table (`shadow_id`, `fill_seq`, `ts_event`, `fill_qty`) of the schedule under the given bugs."""
    bugs = set(bugs or ())
    recs, venue = _events(Path(dataset))
    hidden_venue = venue in ("nasdaq", "generic")
    sh = [_Shadow(s, bugs) for s in schedule]

    seqno = 0
    orders: dict[int, dict[int, list]] = {}

    def best(iid: int, side: int) -> int | None:
        px = [v[1] for v in orders.get(iid, {}).values() if v[0] == side]
        if not px:
            return None
        return max(px) if side == ord("B") else min(px)

    def crossed(s: _Shadow, snap: dict[str, int | None] | None = None) -> bool:
        opp = ord("A") if s.side == ord("B") else ord("B")
        b = best(s.iid, opp) if snap is None else snap.get("opp")
        if b is None:
            return False
        return b <= s.px if s.side == ord("B") else b >= s.px

    def better(side: int, a: int, b: int) -> bool:
        """`a` is strictly better than `b` for a resting order of `side`."""
        return a > b if side == ord("B") else a < b

    def take(s: _Shadow, t: int, use_pre: bool = False) -> None:
        """A5 taker walk: opposite displayed orders at or through the price, best price first, FIFO, minus what the
        shadow took before and what same-side orders with priority (better price, or queue ahead) take first."""
        opp = ord("A") if s.side == ord("B") else ord("B")
        ob = orders.get(s.iid, {})
        if "crossed_fills_at_limit" in bugs:
            fill(s, s.qty - s.filled, t)
            return
        prio = max(0, s.ahead_qty - s.traded) + sum(
            v[2] for v in ob.values() if v[0] == s.side and better(s.side, v[1], s.px)
        )
        avail = sorted(
            (
                (v[1] if opp == ord("A") else -v[1], v[3], oid, v[2] - s.taken.get(oid, 0))
                for oid, v in ob.items()
                if v[0] == opp and (v[1] <= s.px if s.side == ord("B") else v[1] >= s.px) and v[2] > s.taken.get(oid, 0)
            ),
        )
        for _, _, oid, q in avail:
            skip = min(prio, q)
            prio -= skip
            s.taken[oid] = s.taken.get(oid, 0) + skip
            q -= skip
            got = min(q, s.qty - s.filled)
            if got > 0:
                s.taken[oid] += got
                fill(s, got, t)
            if s.state == "done":
                return

    def fill(s: _Shadow, q: int, t: int) -> None:
        if q <= 0:
            return
        s.filled += q
        s.fills.append((t, q))
        if s.filled >= s.qty:
            s.state = "done"

    i = 0
    n = len(recs)
    while i < n:
        j = i
        while j < n and not recs[j]["flags"] & LAST:
            j += 1
        ev = recs[i : min(j, n - 1) + 1]
        i = min(j, n - 1) + 1
        t = int(ev[0]["ts_event"])
        iid0 = ev[0]["instrument_id"]
        pre = {}
        for s in sh:
            if s.iid != iid0 or s.state == "done":
                continue
            if s.state == "live" and s.t_cancel is not None and t > s.t_cancel:
                s.state = "done"
                continue
            if s.state == "pending" and t > s.t_live:
                if s.t_cancel is not None and s.t_cancel <= s.t_live:
                    s.state = "done"
                    continue
                s.state = "live"
                ids = [o for o, v in orders.get(iid0, {}).items() if v[0] == s.side and v[1] == s.px]
                s.ahead = set(ids)
                s.ahead_qty = sum(orders[iid0][o][2] for o in ids)
                if crossed(s):
                    take(s, s.t_live)
            if s.state == "live":
                opp = ord("A") if s.side == ord("B") else ord("B")
                pre[s.id] = {"opp": best(iid0, opp)}
                own = best(iid0, s.side)
                s.inside_at_start = own is None or not better(s.side, own, s.px)
        for r in ev:
            iid, a, side, px, size, oid = (
                r["instrument_id"],
                r["action"],
                r["side"],
                r["price"],
                r["size"],
                r["order_id"],
            )
            ob = orders.setdefault(iid, {})
            resting = ob.get(oid) if a == ord("F") else None
            effect = None
            if a == ord("A") and size > 0:
                seqno += 1
                ob[oid] = [side, px, size, seqno]
            elif a == ord("C") and oid in ob:
                old = ob[oid]
                if size < old[2]:
                    old[2] -= size
                    effect = ("reduce", old[1], size, old[0])
                else:
                    del ob[oid]
                    effect = ("remove", old[1], old[2], old[0])
            elif a == ord("M") and oid in ob:
                old = list(ob[oid])
                if size == 0:
                    del ob[oid]
                    effect = ("remove", old[1], old[2], old[0])
                elif (
                    px == old[1]
                    and venue != "nasdaq"
                    and (size < old[2] or (size == old[2] and (venue != "cme" or "cme_modify_keeps_priority" in bugs)))
                ):
                    ob[oid][2] = size
                    effect = ("reduce", old[1], old[2] - size, old[0])
                else:
                    seqno += 1
                    ob[oid] = [side, px, size, seqno]
                    effect = ("replace", old[1], old[2], old[0], px, size)
            elif a == ord("R"):
                ob.clear()
            for s in sh:
                if s.state != "live" or s.iid != iid:
                    continue
                if effect is not None and oid in s.ahead:
                    kind = effect[0]
                    keep = (kind == "replace" and effect[4] == effect[1] and "upsize_keeps_priority" in bugs) or (
                        kind == "replace" and effect[4] != effect[1] and "reprice_keeps_priority" in bugs
                    )
                    if kind == "remove":
                        s.ahead.discard(oid)
                        s.removed[oid] = s.removed.get(oid, 0) + effect[2]
                    elif kind == "replace" and not keep:
                        s.ahead.discard(oid)
                        s.removed[oid] = s.removed.get(oid, 0) + effect[2]
                    elif kind == "reduce":
                        s.removed[oid] = s.removed.get(oid, 0) + effect[2]
                elif effect is not None and effect[0] == "remove" and effect[1] == s.px and effect[3] == s.side:
                    if "unrelated_delete_to_front" in bugs:
                        s.ahead.clear()
                        s.ahead_qty = 0
                        s.traded = 0
                if a == ord("F") and oid in s.ahead:
                    s.ffill[oid] = s.ffill.get(oid, 0) + size
                if a == ord("T"):
                    s.dup_trade = (t, px) == s.last_trade
                    s.last_trade = (t, px)
                    hidden = side == ord("N") and hidden_venue
                    if hidden and "hidden_consumes_queue" in bugs and px == s.px:
                        s.ev_traded += size
                    elif hidden and "ignore_hidden_prints" not in bugs:
                        if px == s.px:
                            s.hidden_at += size
                        elif better(s.side, s.px, px):
                            s.hidden_through += size
                    elif not hidden and side != s.side and "print_driven_queue" in bugs:
                        if "one_trade_per_event" in bugs and s.dup_trade:
                            continue
                        if px == s.px:
                            s.ev_traded += size
                        elif better(s.side, s.px, px):
                            s.ev_crossed += size
                    elif not hidden and side != s.side and "off_price_print_trade_through" in bugs:
                        if better(s.side, s.px, px):
                            s.ev_crossed += size
                if a == ord("F") and resting is not None and "print_driven_queue" not in bugs:
                    if "one_trade_per_event" in bugs and s.dup_trade:
                        continue
                    r_side, r_px = resting[0], resting[1]
                    if r_side != s.side:
                        continue
                    if r_px == s.px and (px == r_px or oid in s.ahead):
                        s.ev_traded += size
                    elif better(s.side, s.px, r_px) and px == r_px:
                        s.ev_crossed += size
        for s in sh:
            if s.state != "live" or s.iid != iid0:
                continue
            pure = sum(max(0, v - s.ffill.get(k, 0)) for k, v in s.removed.items())
            if "no_cap_on_shrink" not in bugs:
                s.ahead_qty = max(0, s.ahead_qty - pure)
            s.traded += s.ev_traded
            target = min(max(s.traded - s.ahead_qty, 0), s.qty)
            q = target - s.filled
            if q > 0 and not ("exact_leaves_dropped" in bugs and q == s.qty - s.filled):
                fill(s, q, t)
            ob_end = orders.get(iid0, {})
            displayed_at_px = any(v[0] == s.side and v[1] == s.px for v in ob_end.values())
            if s.state == "live" and s.hidden_at > 0 and not displayed_at_px and not crossed(s):
                fill(s, min(s.hidden_at, s.qty - s.filled), t)
            through = s.ev_crossed + (s.hidden_through if s.inside_at_start else 0)
            if s.state == "live" and through > 0 and "no_trade_through" not in bugs:
                fill(s, min(through, s.qty - s.filled), t)
            if s.state == "live" and crossed(s, pre.get(s.id) if "pre_trade_book" in bugs else None):
                take(s, t)
            s.reset()
    out = {"shadow_id": [], "fill_seq": [], "ts_event": [], "fill_qty": []}
    for s in sh:
        fl = s.fills[-1:] if "last_chunk_only" in bugs else s.fills
        for k, (t, q) in enumerate(fl, start=1):
            out["shadow_id"].append(s.id)
            out["fill_seq"].append(k)
            out["ts_event"].append(t)
            out["fill_qty"].append(q)
    return pa.table(
        {
            "shadow_id": pa.array(out["shadow_id"], pa.utf8()),
            "fill_seq": pa.array(out["fill_seq"], pa.uint32()),
            "ts_event": pa.array(out["ts_event"], pa.int64()),
            "fill_qty": pa.array(out["fill_qty"], pa.uint64()),
        }
    )
