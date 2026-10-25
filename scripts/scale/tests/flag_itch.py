"""A small ITCH 5.0 day whose symbols raise the replay checks' flags for known reasons (trace and atlas tests).

Every symbol trades on a calm background book (bids 9.95-9.99, asks 10.01-10.05, executions only at the touch, so
the background alone never flags anything); on top of it:

- `CALM`: nothing else (exact);
- `HALTD`: halted 10:05-10:10 (`H` state H, reason T1); an ask at 10.00 and bids at 10.05 and 10.02 are added
  during the halt (crossed book), then the halt cross at 10.01 executes the 10.02 bid before the 10.05 bid and a
  10.01 ask before the 10.00 ask (two price-priority violations, non-printable `C`), prints `Q` type H and resumes
  (`H` state T); one more crossed add after the resumption;
- `NEWIP`: an IPO (`R` IPO flag Y, `H` H/IPO1 at the start, `K`, `H` Q/IPOQ at 11:00): crossed orders during the
  quotation period, the IPO cross at 11:30 disseminated as Nasdaq does (the `Q` print and `H` T first, then the
  executions, the first of which leaves the book crossed in state T), then trading;
- `ZTSTX`: a test issue (`R` authenticity T) with a crossed add in the regular session;
- `LOCKD`: a locked book before the open, one in the regular session and one after the close, and one execution of
  a bid behind a better bid in the regular session (no cross);
- `PAUSD`: a LULD pause 13:30-13:35 (`H` P/LUDP, then T) with no order activity around it and no cross: no replay
  check flags it, but it was not trading in the session (the `trading_ok` label).

`EXPECTED` holds, per symbol, the crossed / negative-spread events, price-priority violations and causes.
"""

from __future__ import annotations

import gzip
import random
import struct
from pathlib import Path

NS = 1_000_000_000
H_ = 3600 * NS
M_ = 60 * NS
SYMBOLS = ["CALM", "HALTD", "NEWIP", "ZTSTX", "LOCKD", "PAUSD"]


class Day:
    def __init__(self) -> None:
        self.msgs: list[tuple[int, int, bytes]] = []  # (ts, order of insertion, framed message)
        self.n = 0
        self.ref = 1_000_000
        self.match = 1

    def put(self, ts: int, m: bytes) -> None:
        self.msgs.append((ts, self.n, struct.pack(">H", len(m)) + m))
        self.n += 1

    @staticmethod
    def hdr(t: str, loc: int, ts: int) -> bytes:
        return t.encode() + struct.pack(">HH", loc, 0) + ts.to_bytes(6, "big")

    def system(self, ts: int, code: str) -> None:
        self.put(ts, self.hdr("S", 0, ts) + code.encode())

    def directory(self, ts: int, loc: int, sym: str, auth: str = "P", ipo: str = "N") -> None:
        body = sym.encode().ljust(8) + b"QN" + struct.pack(">I", 100) + b"NCZ " + auth.encode() + b"N" + ipo.encode()
        body += b"1N" + struct.pack(">I", 0) + b"N"
        self.put(ts, self.hdr("R", loc, ts) + body)

    def action(self, ts: int, loc: int, sym: str, state: str, reason: str) -> None:
        self.put(ts, self.hdr("H", loc, ts) + sym.encode().ljust(8) + state.encode() + b" " + reason.encode().ljust(4))

    def ipo_quoting(self, ts: int, loc: int, sym: str, release_s: int, price: int) -> None:
        self.put(
            ts,
            self.hdr("K", loc, ts)
            + sym.encode().ljust(8)
            + struct.pack(">I", release_s)
            + b"A"
            + struct.pack(">I", price),
        )

    def add(self, ts: int, loc: int, sym: str, side: str, shares: int, price: int) -> int:
        self.ref += 1
        self.put(
            ts,
            self.hdr("A", loc, ts)
            + struct.pack(">Q", self.ref)
            + side.encode()
            + struct.pack(">I", shares)
            + sym.encode().ljust(8)
            + struct.pack(">I", price),
        )
        return self.ref

    def execute(self, ts: int, loc: int, ref: int, shares: int) -> None:
        self.match += 1
        self.put(ts, self.hdr("E", loc, ts) + struct.pack(">QIQ", ref, shares, self.match))

    def execute_px(self, ts: int, loc: int, ref: int, shares: int, price: int, printable: str = "N") -> None:
        self.match += 1
        self.put(
            ts,
            self.hdr("C", loc, ts)
            + struct.pack(">QIQ", ref, shares, self.match)
            + printable.encode()
            + struct.pack(">I", price),
        )

    def delete(self, ts: int, loc: int, ref: int) -> None:
        self.put(ts, self.hdr("D", loc, ts) + struct.pack(">Q", ref))

    def cross(self, ts: int, loc: int, sym: str, shares: int, price: int, kind: str) -> None:
        self.match += 1
        self.put(
            ts,
            self.hdr("Q", loc, ts)
            + struct.pack(">Q", shares)
            + sym.encode().ljust(8)
            + struct.pack(">IQ", price, self.match)
            + kind.encode(),
        )

    def frames(self) -> bytes:
        return b"".join(f for _, _, f in sorted(self.msgs))


def background(d: Day, loc: int, sym: str, rng: random.Random, quiet: list[tuple[int, int]], n: int = 400) -> None:
    """Calm two-sided activity 09:31-15:58 outside `quiet` windows; executions only at the best price."""
    live: dict[str, dict[int, list[int]]] = {"B": {}, "S": {}}
    ts = 9 * H_ + 31 * M_
    for _ in range(n):
        ts += rng.randrange(20, 60) * NS
        if ts > 15 * H_ + 58 * M_:
            break
        if any(a <= ts < b for a, b in quiet):
            continue
        side = rng.choice("BS")
        book = live[side]
        if rng.random() < 0.6 or not book:
            px = 99_900 - rng.randrange(0, 5) * 100 if side == "B" else 100_100 + rng.randrange(0, 5) * 100
            shares = rng.choice([100, 200, 300])
            book[d.add(ts, loc, sym, side, shares, px)] = [px, shares]
            continue
        best = max(v[0] for v in book.values()) if side == "B" else min(v[0] for v in book.values())
        ref = min(r for r, v in book.items() if v[0] == best)  # FIFO head of the best level
        if rng.random() < 0.6:
            d.execute(ts, loc, ref, 100)
            book[ref][1] -= 100
            if book[ref][1] <= 0:
                del book[ref]
        else:
            # cancels anywhere (never a flag)
            ref = rng.choice(sorted(book))
            d.delete(ts, loc, ref)
            del book[ref]


#: symbol -> (crossed events, negative-spread events, price-priority violations, causes)
EXPECTED = {
    "CALM": (0, 0, 0, {}),
    "HALTD": (4, 4, 2, {"halt_or_pause_quoting": 3, "cross_execution": 2, "regular_session": 1}),
    "NEWIP": (4, 4, 0, {"ipo_quoting_period": 4}),
    "ZTSTX": (1, 1, 0, {"test_symbol": 1}),
    "LOCKD": (3, 0, 1, {"pre_open_continuous": 1, "regular_session": 2, "post_close_continuous": 1}),
    "PAUSD": (0, 0, 0, {}),
}
#: The LULD pause of PAUSD (ns since midnight), the only non-T span besides HALTD's halt and NEWIP's IPO.
PAUSE = (13 * H_ + 30 * M_, 13 * H_ + 35 * M_)


def flag_day(seed: int = 7) -> bytes:
    rng = random.Random(seed)
    d = Day()
    d.system(4 * H_, "O")
    locs = {s: i + 1 for i, s in enumerate(SYMBOLS)}
    for s, loc in locs.items():
        d.directory(4 * H_ + loc, loc, s, auth="T" if s == "ZTSTX" else "P", ipo="Y" if s == "NEWIP" else "N")
        if s == "NEWIP":
            d.action(4 * H_ + 100 + loc, loc, s, "H", "IPO1")
        else:
            d.action(4 * H_ + 100 + loc, loc, s, "T", "")
    d.system(7 * H_, "S")
    d.system(9 * H_ + 30 * M_, "Q")
    d.system(16 * H_, "M")
    d.system(20 * H_, "E")
    d.system(20 * H_ + 5 * M_, "C")
    for s, loc in locs.items():
        quiet = [(10 * H_ + 4 * M_, 10 * H_ + 11 * M_)] if s == "HALTD" else []
        if s == "NEWIP":
            quiet = [(9 * H_, 11 * H_ + 31 * M_)]
        if s == "PAUSD":
            quiet = [(PAUSE[0] - M_, PAUSE[1] + M_)]
        background(d, loc, s, rng, quiet)

    # HALTD: halt 10:05, crossed adds, halt cross at 10:10 in non-price order, resume
    loc, s = locs["HALTD"], "HALTD"
    t0 = 10 * H_ + 5 * M_
    d.action(t0, loc, s, "H", "T1")
    a1 = d.add(t0 + 1 * M_, loc, s, "S", 100, 100_000)  # below the background asks, above the bids: not crossed
    b1 = d.add(t0 + 2 * M_, loc, s, "B", 100, 100_500)  # crossed (1)
    b2 = d.add(t0 + 3 * M_, loc, s, "B", 100, 100_200)  # crossed (2)
    tc = t0 + 5 * M_
    d.execute_px(tc, loc, b2, 100, 100_100)  # violation: b1 at 10.05 is better; still crossed (3)
    d.execute_px(tc + 1, loc, b1, 100, 100_100)  # bids back to <= 9.99 under the 10.00 ask: not crossed
    a2 = d.add(tc + 2, loc, s, "S", 100, 100_100)  # an ask at the cross price: not crossed
    d.execute_px(tc + 3, loc, a2, 100, 100_100)  # violation: a1 at 10.00 is better
    d.execute_px(tc + 4, loc, a1, 100, 100_100)
    d.cross(tc + 5, loc, s, 200, 100_100, "H")
    d.action(tc + 6, loc, s, "T", "")
    # after the resumption, in the regular session: an ask at 10.06 and a bid through it (crossed (4)), deleted
    a3 = d.add(tc + 2 * M_, loc, s, "S", 100, 100_600)
    b3 = d.add(tc + 2 * M_ + 1, loc, s, "B", 100, 101_000)
    d.delete(tc + 2 * M_ + 2, loc, b3)
    d.delete(tc + 2 * M_ + 3, loc, a3)

    # NEWIP: K, quotation period from 11:00, two crossed adds, IPO cross at 11:30 (ask first), then trading
    loc, s = locs["NEWIP"], "NEWIP"
    d.ipo_quoting(9 * H_, loc, s, 11 * 3600, 100_000)
    d.action(11 * H_, loc, s, "Q", "IPOQ")
    nb = d.add(11 * H_ + 1 * M_, loc, s, "B", 300, 101_000)  # one side only: not crossed
    na = d.add(11 * H_ + 2 * M_, loc, s, "S", 200, 100_000)  # crossed (1)
    nb2 = d.add(11 * H_ + 3 * M_, loc, s, "B", 100, 100_500)  # crossed (2)
    tc = 11 * H_ + 30 * M_
    # as Nasdaq disseminates an IPO cross (2020-01-30 ANPC): the Q print and the resumption first, then the
    # non-printable executions, so the book is still crossed for one event in state T
    d.cross(tc, loc, s, 200, 100_500, "H")  # an event (a trade record) on the crossed book: crossed (3)
    d.action(tc + 1, loc, s, "T", "")
    d.execute_px(tc + 2, loc, nb, 200, 100_500)  # the best bid: no violation; 100 left at 10.10: crossed (4)
    d.execute_px(tc + 3, loc, na, 200, 100_500)  # the only ask: no violation; asks empty: not crossed
    d.delete(tc + 4, loc, nb)
    d.delete(tc + 5, loc, nb2)

    # ZTSTX: a test issue's crossed book in the regular session
    loc, s = locs["ZTSTX"], "ZTSTX"
    za = d.add(12 * H_ + 7, loc, s, "S", 100, 100_600)
    zb = d.add(12 * H_ + 8, loc, s, "B", 100, 101_000)  # crossed (1)
    d.delete(12 * H_ + 9, loc, zb)
    d.delete(12 * H_ + 10, loc, za)

    # LOCKD: locked before the open, in the session and after the close; one out-of-priority execution
    loc, s = locs["LOCKD"], "LOCKD"
    for t in (8 * H_, 13 * H_, 16 * H_ + 30 * M_):
        la = d.add(t, loc, s, "S", 100, 100_000)  # between the background bids and asks: not crossed
        lb = d.add(t + 1, loc, s, "B", 100, 100_000)  # locked
        d.delete(t + 2, loc, lb)
        d.delete(t + 3, loc, la)
    qa = d.add(14 * H_, loc, s, "B", 100, 99_200)
    qb = d.add(14 * H_ + 1, loc, s, "B", 100, 99_100)
    d.execute(14 * H_ + 2, loc, qb, 100)  # executed while a better bid (qa) is live: violation
    d.delete(14 * H_ + 3, loc, qa)

    # PAUSD: a LULD pause with nothing displayed or executed in it
    d.action(PAUSE[0], locs["PAUSD"], "PAUSD", "P", "LUDP")
    d.action(PAUSE[1], locs["PAUSD"], "PAUSD", "T", "")
    return d.frames()


def write_flag_day(path: Path, seed: int = 7) -> bytes:
    raw = flag_day(seed)
    path.write_bytes(gzip.compress(raw, mtime=0))
    return raw
