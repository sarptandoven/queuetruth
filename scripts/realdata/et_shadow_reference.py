"""Independent Python implementation of SPEC 5.2 shadow semantics (abs price, latency 0, cancels; Nasdaq rules)
over an et MBO CSV tape. Writes queuetruth-shaped `shadow_results`, `shadow_fills` and `queue_trajectory` (mode
`ahead`) Parquet tables, then checks them with validate_et.compare_shadow_tables against et's shadow_fills.csv and
queue_trace.csv.

Two uses:
1. Proves the RD-5/RD-6 comparison rules (in particular the trajectory -> et queue_trace mapping) on real data
   independently of queuetruth: if this passes 3,000/3,000 on both days, a later mismatch is a queuetruth bug.
2. A second oracle for the per-fill timeline (qt.shadow_fills), which et never wrote (et keeps first fill only):
   diff qt's shadow_fills.parquet against `<out>/shadow_fills.parquet` from this script.

    python -I et_shadow_reference.py --date 2019-12-30 --out <scratch>/shref-d1
Runs in ~1-2 min per day, < 1 GB RAM. Writes event-level rows: keep outputs under .realdata/ or scratch.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import validate_et as V  # noqa: E402

F_LAST = 128
TS = pa.timestamp("ns", tz="UTC")
DICT = pa.dictionary(pa.int8(), pa.utf8())


class Sh:
    __slots__ = (
        "ahead",
        "ahead_set",
        "cancel",
        "cancel_eff",
        "end",
        "end_ts",
        "ev_crossed",
        "ev_filled",
        "ev_removed",
        "ev_seq",
        "ev_traded",
        "filled",
        "inst",
        "nfill",
        "price",
        "qa_ff",
        "qa_live",
        "qty",
        "reason",
        "sid",
        "side",
        "status",
        "traded",
        "ts_act",
        "ts_ff",
        "ts_live",
        "ts_submit",
    )

    def __init__(self, row: list[str]):
        self.sid = row[0]
        self.inst = int(row[1])
        self.side = row[2]
        self.price = int(row[3])
        self.qty = int(row[4])
        self.ts_submit = int(row[5])
        self.ts_live = self.ts_submit + int(row[6])
        self.cancel = int(row[7]) if row[7] else None
        self.status = "pending"
        self.ahead_set: set[int] = set()
        self.ahead = 0
        self.traded = 0
        self.filled = 0
        self.qa_live = None
        self.ts_ff = None
        self.qa_ff = None
        self.reason = "never"
        self.end = None
        self.ev_removed: dict[int, int] = {}
        self.ev_filled: dict[int, int] = {}
        self.ev_seq = -1
        self.ev_traded = 0
        self.ev_crossed = 0
        self.nfill = 0
        self.end_ts = None
        self.ts_act = None
        self.cancel_eff = None

    def qa(self) -> int:
        return max(0, self.ahead - self.traded)


class Book:
    def __init__(self):
        self.orders: dict[int, list] = {}  # oid -> [side, price, size]
        self.levels: dict[tuple[str, int], list] = {}  # (side, price) -> [total, {oid: None}]
        self.heaps = {"B": [], "A": []}

    def best(self, side: str):
        h = self.heaps[side]
        while h:
            px = -h[0] if side == "B" else h[0]
            if (side, px) in self.levels:
                return px
            heapq.heappop(h)
        return None

    def add(self, oid, side, px, sz):
        lv = self.levels.get((side, px))
        if lv is None:
            lv = self.levels[(side, px)] = [0, {}]
            heapq.heappush(self.heaps[side], -px if side == "B" else px)
        lv[0] += sz
        lv[1][oid] = None
        self.orders[oid] = [side, px, sz]

    def cancel(self, oid, sz):
        o = self.orders[oid]
        side, px, rest = o
        lv = self.levels[(side, px)]
        if sz < rest:
            o[2] -= sz
            lv[0] -= sz
            return ("reduce", side, px, sz)
        del self.orders[oid]
        lv[0] -= rest
        del lv[1][oid]
        if not lv[1]:
            del self.levels[(side, px)]
        return ("remove", side, px, rest)


def run(et: Path, date: str, out: Path) -> dict:
    cfg = V.DAYS[date]
    shadows: list[Sh] = []
    with open(et / cfg["fidelity"] / "shadows.csv") as f:
        f.readline()
        for ln in f:
            shadows.append(Sh(ln.rstrip("\n").split(",")))
    pending: dict[int, list[Sh]] = {}
    for s in sorted(shadows, key=lambda s: (s.ts_live, s.sid), reverse=True):
        pending.setdefault(s.inst, []).append(s)
    live_px: dict[tuple[int, str, int], set] = {}
    live_by_inst: dict[int, dict[str, set]] = {}
    cancel_heap: dict[int, list] = {}
    books: dict[int, Book] = {}
    open_ev: dict[int, bool] = {}
    touched: dict[int, set] = {}
    fills_rows: list[tuple] = []
    traj_rows: list[tuple] = []
    last_ts: dict[int, int] = {}

    extreme: dict[tuple[int, str], int | None] = {}  # max live bid / min live ask shadow price per instrument

    def refresh(inst: int, side: str):
        ss = live_by_inst[inst][side]
        extreme[(inst, side)] = (max(x.price for x in ss) if side == "B" else min(x.price for x in ss)) if ss else None

    def go_live(s: Sh):
        live_px.setdefault((s.inst, s.side, s.price), set()).add(s)
        live_by_inst.setdefault(s.inst, {"B": set(), "A": set()})[s.side].add(s)
        refresh(s.inst, s.side)
        if s.cancel is not None:
            heapq.heappush(cancel_heap.setdefault(s.inst, []), (s.cancel, s.sid, s))

    def finish(s: Sh, state: str, ts: int, seq: int):
        s.status, s.end, s.end_ts = "done", state, ts
        live_px[(s.inst, s.side, s.price)].discard(s)
        live_by_inst[s.inst][s.side].discard(s)
        refresh(s.inst, s.side)
        if state == "cancelled":
            s.cancel_eff = ts
        traj_rows.append((s.sid, ts, seq, "end", s.qa(), s.qa(), len(s.ahead_set), len(s.ahead_set), s.filled))

    def do_fill(s: Sh, q: int, reason: str, ts: int, seq: int, qa_before: int, ev_tr: int, ev_x: int):
        if q == 0:
            return
        if s.filled == 0:
            s.reason, s.ts_ff, s.qa_ff = reason, ts, qa_before
        q = min(q, s.qty - s.filled)
        s.filled += q
        s.nfill += 1
        fills_rows.append((s.sid, s.nfill, ts, seq, q, s.filled, s.qty - s.filled, reason, qa_before, ev_tr, ev_x))

    def crossed(s: Sh, bk: Book) -> bool:
        if s.side == "B":
            a = bk.best("A")
            return a is not None and a <= s.price
        b = bk.best("B")
        return b is not None and b >= s.price

    def ev_filled(s: Sh, seq: int) -> dict:
        if s.ev_seq != seq:
            s.ev_filled.clear()
            s.ev_seq = seq
        return s.ev_filled

    t0 = time.time()
    n = 0
    with open(et / cfg["itch"] / "mbo.csv", "rb", buffering=1 << 22) as f:
        f.readline()
        for ln in f:
            n += 1
            p = ln.split(b",")
            ts = int(p[0])
            inst = int(p[2])
            act = p[3]
            seq = int(p[9])
            flags = int(p[8])
            bk = books.get(inst)
            if bk is None:
                bk = books[inst] = Book()
                touched[inst] = set()
            last_ts[inst] = ts
            if not open_ev.get(inst):
                open_ev[inst] = True
                pend = pending.get(inst)
                while pend and pend[-1].ts_live < ts:
                    s = pend.pop()
                    if s.cancel is not None and s.cancel < ts and s.cancel <= s.ts_live:
                        s.status, s.end, s.end_ts, s.cancel_eff = "done", "cancelled", s.cancel, s.cancel
                        continue
                    s.ts_act = ts
                    lv = bk.levels.get((s.side, s.price))
                    s.ahead = lv[0] if lv else 0
                    s.ahead_set = set(lv[1]) if lv else set()
                    s.qa_live = s.ahead
                    s.status = "live"
                    traj_rows.append(
                        (s.sid, s.ts_live, seq, "live", s.ahead, s.ahead, len(s.ahead_set), len(s.ahead_set), 0)
                    )
                    go_live(s)
                    if crossed(s, bk):
                        do_fill(s, s.qty, "crossed_at_live", s.ts_live, seq, s.ahead, 0, 0)
                        finish(s, "filled", s.ts_live, seq)
                ch = cancel_heap.get(inst)
                while ch and ch[0][0] < ts:
                    c_ts, _, s = heapq.heappop(ch)
                    if s.status == "live":
                        finish(s, "cancelled", c_ts, seq)
            # book + per-record shadow accounting (SPEC 5.2.4)
            if act == b"A":
                side = p[4].decode()
                bk.add(int(p[7]), side, int(p[5]), int(p[6]))
            elif act == b"C":
                oid = int(p[7])
                kind, side, px, sz = bk.cancel(oid, int(p[6]))
                for s in live_px.get((inst, side, px), ()):
                    if oid in s.ahead_set:
                        s.ev_removed[oid] = s.ev_removed.get(oid, 0) + sz
                        if kind == "remove":
                            s.ahead_set.discard(oid)
                        touched[inst].add(s)
            elif act == b"F":
                oid = int(p[7])
                o = bk.orders.get(oid)
                if o is not None:
                    for s in live_px.get((inst, o[0], o[1]), ()):
                        if oid in s.ahead_set:
                            d = ev_filled(s, seq)
                            d[oid] = d.get(oid, 0) + int(p[6])
                            touched[inst].add(s)
            elif act == b"T":
                aggr = p[4]
                if aggr != b"N":
                    px, sz = int(p[5]), int(p[6])
                    hit = "B" if aggr == b"A" else "A"
                    for s in live_by_inst.get(inst, {}).get(hit, ()):
                        if s.price == px:
                            s.ev_traded += sz
                            touched[inst].add(s)
                        elif (hit == "B" and px < s.price) or (hit == "A" and px > s.price):
                            s.ev_crossed += sz
                            touched[inst].add(s)
            if flags & F_LAST:
                open_ev[inst] = False
                cand = set(touched[inst])
                lbi = live_by_inst.get(inst)
                if lbi:
                    a, b = bk.best("A"), bk.best("B")
                    mb, ma = extreme.get((inst, "B")), extreme.get((inst, "A"))
                    if a is not None and mb is not None and mb >= a:
                        cand.update(s for s in lbi["B"] if s.price >= a)
                    if b is not None and ma is not None and ma <= b:
                        cand.update(s for s in lbi["A"] if s.price <= b)
                for s in sorted(cand, key=lambda s: s.sid):
                    if s.status != "live":
                        continue
                    qa0, rk0, f0 = s.qa(), len(s.ahead_set), s.filled
                    d = ev_filled(s, seq)
                    pure = sum(max(0, r - d.get(i, 0)) for i, r in s.ev_removed.items())
                    s.ahead = max(0, s.ahead - pure)
                    qa_before = s.qa()
                    s.traded += s.ev_traded
                    target = min(max(s.traded - s.ahead, 0), s.qty)
                    if target > s.filled:
                        do_fill(s, target - s.filled, "queue_depleted", ts, seq, qa_before, s.ev_traded, s.ev_crossed)
                    if s.ev_crossed > 0 and s.filled < s.qty:
                        do_fill(
                            s,
                            min(s.ev_crossed, s.qty - s.filled),
                            "trade_through",
                            ts,
                            seq,
                            qa_before,
                            s.ev_traded,
                            s.ev_crossed,
                        )
                    if s.filled < s.qty and crossed(s, bk):
                        do_fill(s, s.qty - s.filled, "crossed", ts, seq, qa_before, s.ev_traded, s.ev_crossed)
                    for i, r in s.ev_removed.items():  # fill_removal_shares_sequence (Nasdaq)
                        if i in d:
                            d[i] = max(0, d[i] - r)
                    for i in [i for i, v in d.items() if v == 0]:
                        del d[i]
                    s.ev_removed.clear()
                    s.ev_traded = 0
                    s.ev_crossed = 0
                    if (s.qa(), len(s.ahead_set), s.filled) != (qa0, rk0, f0):
                        traj_rows.append((s.sid, ts, seq, "event", qa0, s.qa(), rk0, len(s.ahead_set), s.filled))
                    if s.filled >= s.qty:
                        finish(s, "filled", ts, seq)
                touched[inst].clear()
    close_ns = V.DAYS[date]["midnight"] + 57600 * 10**9
    for s in shadows:
        if s.status == "live":
            finish(s, "censored_session_end" if last_ts[s.inst] >= close_ns else "open_at_end", last_ts[s.inst], 0)
    out.mkdir(parents=True, exist_ok=True)
    md = lambda name: {b"qt.schema": name.encode(), b"qt.schema_version": b"1", b"qt.engine_version": b"pyref"}  # noqa: E731
    res = pa.table(
        {
            "shadow_id": [s.sid for s in shadows],
            "qty": pa.array([s.qty for s in shadows], pa.uint64()),
            "ts_live": pa.array([s.ts_live for s in shadows], TS),
            "ts_activated_event": pa.array([s.ts_act for s in shadows], TS),
            "ts_cancel_effective": pa.array([s.cancel_eff for s in shadows], TS),
            "ts_first_fill": pa.array([s.ts_ff for s in shadows], TS),
            "filled_qty": pa.array([s.filled for s in shadows], pa.uint64()),
            "queue_ahead_at_live": pa.array([s.qa_live for s in shadows], pa.uint64()),
            "queue_ahead_at_first_fill": pa.array([s.qa_ff for s in shadows], pa.uint64()),
            "first_fill_reason": pa.array([s.reason for s in shadows]).dictionary_encode(),
            "end_state": pa.array([s.end for s in shadows]).dictionary_encode(),
        }
    ).replace_schema_metadata(md("qt.shadow_results"))
    cols = list(zip(*fills_rows, strict=False)) if fills_rows else [[]] * 11
    fl = pa.table(
        {
            "shadow_id": pa.array(cols[0], pa.utf8()),
            "fill_seq": pa.array(cols[1], pa.uint32()),
            "ts_event": pa.array(cols[2], TS),
            "sequence": pa.array(cols[3], pa.uint64()),
            "fill_qty": pa.array(cols[4], pa.uint64()),
            "cum_qty": pa.array(cols[5], pa.uint64()),
            "leaves_qty": pa.array(cols[6], pa.uint64()),
            "reason": pa.array(cols[7], pa.utf8()).dictionary_encode(),
            "queue_ahead_before": pa.array(cols[8], pa.uint64()),
            "traded_at_px_event": pa.array(cols[9], pa.uint64()),
            "trade_through_event": pa.array(cols[10], pa.uint64()),
        }
    ).replace_schema_metadata(md("qt.shadow_fills"))
    tc = list(zip(*traj_rows, strict=False))
    tr = pa.table(
        {
            "shadow_id": pa.array(tc[0], pa.utf8()),
            "ts_event": pa.array(tc[1], TS),
            "sequence": pa.array(tc[2], pa.uint64()),
            "kind": pa.array(tc[3], pa.utf8()).dictionary_encode(),
            "queue_ahead_before": pa.array(tc[4], pa.uint64()),
            "queue_ahead_after": pa.array(tc[5], pa.uint64()),
            "rank_before": pa.array(tc[6], pa.uint32()),
            "rank_after": pa.array(tc[7], pa.uint32()),
            "filled_qty": pa.array(tc[8], pa.uint64()),
        }
    ).replace_schema_metadata(md("qt.queue_trajectory"))
    pq.write_table(res, out / "shadow_results.parquet")
    pq.write_table(fl, out / "shadow_fills.parquet")
    pq.write_table(tr, out / "queue_trajectory.parquet")
    return {
        "records": n,
        "wall_s": round(time.time() - t0, 1),
        "fills_rows": fl.num_rows,
        "trajectory_rows": tr.num_rows,
        "n_filled": sum(1 for s in shadows if s.filled > 0),
        "end_states": V._count(s.end for s in shadows),
        "reasons": V._count(s.reason for s in shadows),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--et-root",
        type=Path,
        default=os.environ.get("QT_ET_ROOT"),
        required=not os.environ.get("QT_ET_ROOT"),
        help="local checkout of the et prototype (default: $QT_ET_ROOT)",
    )
    ap.add_argument("--date", required=True, choices=list(V.DAYS))
    ap.add_argument("--expected", type=Path, default=HERE.parent.parent / "expected.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--update-expected", action="store_true", help="store the per-fill digest etc. in --expected")
    a = ap.parse_args(argv)
    info = run(a.et_root, a.date, a.out)
    E = json.loads(a.expected.read_text())["days"][a.date]["shadows_mirror_et"]
    c = V.Check("pyref", f"python SPEC 5.2 reference vs et, {a.date}")
    c.eq("n_filled", info["n_filled"], E["n_filled"])
    cfg = V.DAYS[a.date]
    V.compare_shadow_tables(
        c,
        E,
        a.out / "shadow_results.parquet",
        a.out / "shadow_fills.parquet",
        a.out / "queue_trajectory.parquet",
        a.et_root / cfg["fidelity"] / "engine" / "shadow_fills.csv",
        a.et_root / cfg["fidelity"] / "engine" / "queue_trace.csv",
    )
    d = c.doc()
    d["run"] = info
    dg, nrows = V.fills_digest(a.out / "shadow_fills.parquet")
    d["spec_reference"] = {
        "fills_rows": nrows,
        "fills_digest": dg,
        "end_states": info["end_states"],
        "first_fill_reasons": info["reasons"],
        "trajectory_rows_ahead_mode": info["trajectory_rows"],
        "note": "predicted by et_shadow_reference.py (independent Python port of SPEC 5.2 / et "
        "shadow.rs); per-fill rows exclude behind_volume_executed",
    }
    if a.update_expected and d["status"] == "pass":
        doc = json.loads(a.expected.read_text())
        doc["days"][a.date]["shadows_mirror_et"]["spec_reference"] = d["spec_reference"]
        a.expected.write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(d, indent=1, default=str))
    return 0 if d["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
