"""Hand-built Kraken WebSocket v2 `level3` capture (synthetic, no real data).

An independent mini publisher: it keeps the *true* full-depth book (price-time FIFO, Kraken amend rules: quantity
down keeps the queue position, quantity up or a new price goes to the back), and after every batch of book
operations emits what the level3 channel with depth D shows (docs.kraken.com/api/docs/websocket-v2/level3, accessed
2026-10-19): `add` for orders entering the top-D window (a level coming back into view sends all its orders in
queue order), `modify` for quantity changes, `delete` for orders removed from the book while in view, nothing for
orders that only fall out of the window. The CRC32 checksum follows the L3 checksum guide (top 10 levels, asks low
to high then bids high to low, order by order, price / qty at the pair precision with `.` and leading zeros
removed) and uses Python's zlib.crc32, so the fixture's checksums do not come from the Rust code under test.

Run: python3 gen.py  (writes level3.jsonl and expected_view.json next to this file)
"""

import json
import os
import zlib
from decimal import Decimal

D = 10
SYM = "BTC/USD"
PX_DP, QTY_DP = 2, 8
T0 = 1_791_676_800_000_000_000  # 2026-10-11T00:00:00Z in ns


def iso(ns):
    s, f = divmod(ns, 1_000_000_000)
    import datetime

    t = datetime.datetime.fromtimestamp(s, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{t}.{f:09d}Z"


def num(d):
    """JSON number text as a float repr would print it (Kraken sends floats): 100.0, 0.5."""
    s = format(Decimal(d), "f")
    if "." in s:
        s = s.rstrip("0")
        if s.endswith("."):
            s += "0"
    return s


class Book:
    def __init__(self):
        self.orders = {}  # id -> [side, px(Decimal), qty(Decimal), ts]
        self.fifo = {"b": {}, "a": {}}  # side -> px -> [ids]

    def add(self, oid, side, px, qty, ts):
        px, qty = Decimal(px), Decimal(qty)
        self.orders[oid] = [side, px, qty, ts]
        self.fifo[side].setdefault(px, []).append(oid)

    def remove(self, oid):
        side, px, _, _ = self.orders.pop(oid)
        q = self.fifo[side][px]
        q.remove(oid)
        if not q:
            del self.fifo[side][px]

    def amend(self, oid, qty=None, px=None, ts=None):
        side, opx, oqty, _ots = self.orders[oid]
        nqty = Decimal(qty) if qty is not None else oqty
        npx = Decimal(px) if px is not None else opx
        if npx == opx and nqty <= oqty:
            self.orders[oid][2] = nqty  # keeps the queue position
        else:
            self.remove(oid)
            self.add(oid, side, npx, nqty, ts)

    def levels(self, side):
        return sorted(self.fifo[side], reverse=(side == "b"))

    def view(self):
        """(side, px) -> [ids] of the top-D levels."""
        v = {}
        for side in "ba":
            for px in self.levels(side)[:D]:
                v[(side, px)] = list(self.fifo[side][px])
        return v


def ord_json(b, oid, event=None):
    _side, px, qty, ts = b.orders[oid]
    o = {}
    if event:
        o["event"] = event
    o.update({"order_id": oid, "limit_price": num(px), "order_qty": num(qty), "timestamp": iso(ts)})
    return o


def checksum(b):
    v = b.view()
    s = ""
    for side, rev in (("a", False), ("b", True)):
        for px in sorted([p for (sd, p) in v if sd == side], reverse=rev)[:10]:
            for oid in v[(side, px)]:
                q = b.orders[oid][2]
                s += str(int(px * 10**PX_DP)).lstrip("0") + str(int(q * 10**QTY_DP)).lstrip("0")
    return zlib.crc32(s.encode()) & 0xFFFFFFFF


def raw_num(text):
    """Marker so json.dumps writes the number text verbatim."""
    return {"__num__": text}


def dumps(x):
    s = json.dumps(x, separators=(",", ":"))
    # limit_price / order_qty are written as bare JSON numbers
    import re

    return re.sub(r'"(limit_price|order_qty)":"([0-9.]+)"', r'"\1":\2', s)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    b = Book()
    t = T0
    oid = iter(f"O{n:05d}-SYN-L3" for n in range(1, 10_000))
    named = {}

    def new(name, side, px, qty):
        nonlocal t
        i = next(oid)
        named[name] = i
        b.add(i, side, px, qty, t)
        return i

    # true book: 11 bid levels (99.0 .. 100.0) and 11 ask levels (100.1 .. 101.1); two orders on some levels
    t -= 60_000_000_000
    for k in range(11):
        px = Decimal("100.0") - Decimal("0.1") * k
        new(f"b{k}a", "b", px, "0.5")
        if k in (0, 9, 10):
            new(f"b{k}b", "b", px, "0.25")
        if k == 9:
            new(f"b{k}c", "b", px, "0.125")
    for k in range(11):
        px = Decimal("100.1") + Decimal("0.1") * k
        new(f"a{k}a", "a", px, "0.75")
        if k in (0, 9):
            new(f"a{k}b", "a", px, "0.3")
    t = T0
    lines = []
    recv = [T0 + 500_000]

    def emit(msg):
        recv[0] += 1_000_000
        lines.append(dumps({"recv_ns": recv[0], "msg": msg}))

    emit({"method": "subscribe", "result": {"channel": "level3", "symbol": SYM, "depth": D, "snapshot": True},
          "success": True, "time_in": iso(t), "time_out": iso(t)})
    view = b.view()
    snap = {"symbol": SYM, "checksum": checksum(b), "timestamp": iso(t), "bids": [], "asks": []}
    def level_order(kv):
        (side, px), _ids = kv
        return (side != "b", -px if side == "b" else px)

    for (side, _px), ids in sorted(view.items(), key=level_order):
        for i in ids:
            snap["bids" if side == "b" else "asks"].append(ord_json(b, i))
    emit({"channel": "level3", "type": "snapshot", "data": [snap]})

    def step(ops):
        """Applies ops to the true book and emits the level3 update (if any event)."""
        nonlocal t
        t += 1_000_000
        before = b.view()
        before_ids = {i: k for k, ids in before.items() for i in ids}
        before_state = {i: (b.orders[i][1], b.orders[i][2]) for i in before_ids}
        removed = []
        for op in ops:
            kind = op[0]
            if kind == "add":
                _, name, side, px, qty = op
                new(name, side, px, qty)
            elif kind == "del":
                i = named[op[1]]
                removed.append(i)
                b.remove(i)
            elif kind == "amend":
                _, name, qty, px = op
                b.amend(named[name], qty, px, t)
        after = b.view()
        after_ids = {i: k for k, ids in after.items() for i in ids}
        ev = {"b": [], "a": []}
        for i in removed:
            if i in before_ids:
                side = before_ids[i][0]
                ev[side].append({"event": "delete", "order_id": i, "limit_price": num(before_state[i][0]),
                                 "order_qty": num(before_state[i][1]), "timestamp": iso(t)})
        # orders in view before whose price changed: delete (and an add if the new price is in view); quantity
        # changes at the same price: modify
        for i, k in before_ids.items():
            if i in b.orders and before_state[i][0] != b.orders[i][1]:
                ev[k[0]].append({"event": "delete", "order_id": i, "limit_price": num(before_state[i][0]),
                                 "order_qty": num(before_state[i][1]), "timestamp": iso(t)})
        for i, k in after_ids.items():
            if i in before_ids and before_state[i][0] == b.orders[i][1] and before_state[i][1] != b.orders[i][2]:
                ev[k[0]].append(ord_json(b, i, "modify"))
        # adds: per level in view after, orders not in view before (or moved), in queue order
        for (side, _px), ids in after.items():
            for i in ids:
                if i not in before_ids or before_state[i][0] != b.orders[i][1]:
                    ev[side].append(ord_json(b, i, "add"))
        if ev["b"] or ev["a"]:
            emit({"channel": "level3", "type": "update", "data": [{
                "checksum": checksum(b), "symbol": SYM, "timestamp": iso(t),
                "bids": ev["b"], "asks": ev["a"]}]})

    step([("add", "nb1", "b", "100.05", "1.0")])            # 1: bid level 99.1 leaves the window
    step([("amend", "nb1", "0.4", None)])                   # 2: partial fill / amend down at the best bid
    step([("del", "nb1")])                                  # 3: 99.1 re-enters unchanged (2 kept orders)
    step([("add", "nb2", "b", "100.05", "2.0")])            # 4: 99.1 leaves again
    step([("del", "b9a"), ("amend", "b9b", "0.2", None),    # 5: out of view: cancel, amend down, unseen add
          ("add", "u1", "b", "99.1", "0.6")])               #    (the feed shows nothing)
    step([("del", "nb2")])                                  # 6: 99.1 re-enters: vanished, M, unseen add
    step([("add", "na1", "a", "100.05", "0.9")])            # 7: ask level 101.0 leaves the window
    step([("del", "a9a"), ("del", "a9b"), ("del", "a10a")])  # 8: out of view: everything beyond 101.0 cancelled
    step([("del", "na1")])                                  # 9: ask side shrinks to 9 levels: 101.0 vanished
    step([("amend", "b0a", "0.9", None)])                   # 10: amend up: back of the 100.0 queue
    step([("amend", "a1a", None, "100.25")])                # 11: price amend (delete + add at a new level)
    step([("del", "a0a"), ("amend", "a0b", "0.1", None)])   # 12: sweep: best ask removed, next decreased

    with open(os.path.join(here, "level3.jsonl"), "w") as f:
        f.write("\n".join(lines) + "\n")
    final = b.view()
    exp = {"bids": [], "asks": []}
    for (side, px), ids in final.items():
        for i in ids:
            exp["bids" if side == "b" else "asks"].append([i, num(px), num(b.orders[i][2])])
    exp["bids"].sort(key=lambda r: -Decimal(r[1]))
    exp["asks"].sort(key=lambda r: Decimal(r[1]))
    # within a level keep queue order (sort is stable on the view's FIFO order)
    with open(os.path.join(here, "expected_view.json"), "w") as f:
        json.dump(exp, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main()
