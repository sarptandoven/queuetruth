"""Tiny synthetic order-level days for the `queuetruth demo` tests, written by our own code from the public format
documents (Nasdaq TotalView-ITCH 5.0; IEX DEEP+ v1.06 inside IEX-TP v1.26 inside pcapng). Every value is invented.

`book_events` simulates a price-time-priority book per symbol (adds near the touch, cancels, executions of the head
order of the best level, never crossed) and returns abstract events; `itch_day` and `iex_day` encode the same
events in each format, with the system events and directory messages of a real day.
"""

from __future__ import annotations

import datetime as dt
import gzip
import random
import struct
from collections import deque
from dataclasses import dataclass
from zoneinfo import ZoneInfo

NS = 1_000_000_000
ET = ZoneInfo("America/New_York")


def hms(h: int, m: int, s: float = 0.0) -> int:
    """Nanoseconds after midnight."""
    return int((h * 3600 + m * 60 + s) * NS)


def midnight_utc_ns(date: str) -> int:
    d = dt.date.fromisoformat(date)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=ET).timestamp()) * NS


@dataclass
class Ev:
    ts: int  # ns after midnight
    sym: str
    kind: str  # add | delete | exec | cancel
    oid: int
    side: str = ""  # B | S
    price: int = 0  # 1/10000 dollars
    size: int = 0
    match: int = 0


def book_events(symbols: list[str], start: int, end: int, *, mean_step_ns: int = 20_000_000, seed: int = 7) -> list[Ev]:
    """A never-crossed price-time book per symbol between `start` and `end` (ns after midnight)."""
    rng = random.Random(seed)
    tick = 100  # $0.01 in 1/10000 dollars
    books = {s: {"B": {}, "S": {}} for s in symbols}
    live: dict[int, tuple[str, str, int]] = {}
    out: list[Ev] = []
    oid = 1
    match = 1
    ts = start

    def best(sym: str, side: str) -> int | None:
        lv = books[sym][side]
        if not lv:
            return None
        return max(lv) if side == "B" else min(lv)

    def add(sym: str, side: str, px: int, size: int) -> None:
        nonlocal oid
        books[sym][side].setdefault(px, deque()).append([oid, size])
        live[oid] = (sym, side, px)
        out.append(Ev(ts, sym, "add", oid, side, px, size))
        oid += 1

    for i, sym in enumerate(symbols):
        mid = (100 + 20 * i) * 10_000
        for k in range(1, 6):
            for _ in range(3):
                add(sym, "B", mid - k * tick, rng.choice([100, 200, 300]))
                add(sym, "S", mid + k * tick, rng.choice([100, 200, 300]))
                ts += 1000
    while ts < end:
        ts += max(1, int(rng.expovariate(1.0 / mean_step_ns)))
        if ts >= end:
            break
        sym = rng.choice(symbols)
        r = rng.random()
        bb, ba = best(sym, "B"), best(sym, "S")
        if r < 0.47 or bb is None or ba is None:
            side = rng.choice("BS")
            back = rng.choice([0, 0, 0, 1, 1, 2, 3])
            if side == "B":
                px = (bb if bb is not None else (ba or 1_000_000) - tick) - back * tick
                if ba is not None and px >= ba:
                    px = ba - tick
            else:
                px = (ba if ba is not None else (bb or 1_000_000) + tick) + back * tick
                if bb is not None and px <= bb:
                    px = bb + tick
            add(sym, side, px, rng.choice([100, 100, 200, 300, 500]))
        elif r < 0.72:
            cand = [o for o, (s, _, _) in live.items() if s == sym]
            o = rng.choice(cand)
            s, side, px = live.pop(o)
            q = books[s][side][px]
            for j, e in enumerate(q):
                if e[0] == o:
                    del q[j]
                    break
            if not q:
                del books[s][side][px]
            out.append(Ev(ts, sym, "delete", o))
        else:
            side = rng.choice("BS")
            px = best(sym, side)
            if px is None:
                continue
            q = books[sym][side][px]
            head = q[0]
            n = min(head[1], rng.choice([100, 100, 200, 300]))
            out.append(Ev(ts, sym, "exec", head[0], side, px, n, match))
            match += 1
            head[1] -= n
            if head[1] == 0:
                q.popleft()
                live.pop(head[0])
                if not q:
                    del books[sym][side][px]
    return out


# ---- ITCH 5.0 ---------------------------------------------------------------------------------------------------


def _ih(t: str, loc: int, ts: int) -> bytes:
    return t.encode() + struct.pack(">HH", loc, 0) + ts.to_bytes(6, "big")


def itch_frame(m: bytes) -> bytes:
    return struct.pack(">H", len(m)) + m


def itch_directory(sym: str, loc: int, ts: int) -> bytes:
    body = sym.encode().ljust(8) + b"Q" + b"N" + struct.pack(">I", 100) + b"N" + b"C" + b"Z " + b"P" + b"N"
    body += b" " + b"1" + b"N" + struct.pack(">I", 0) + b"N"
    return _ih("R", loc, ts) + body


def itch_system(code: str, ts: int) -> bytes:
    return _ih("S", 0, ts) + code.encode()


def itch_day(
    symbols: list[str], events: list[Ev], *, extra_symbols: tuple[str, ...] = ("ZZZT",), close: bool = True
) -> bytes:
    """Framed ITCH 5.0 bytes: system events, a directory (requested and extra symbols interleaved), the events."""
    names = [*symbols, *extra_symbols]
    loc = {s: i + 1 for i, s in enumerate(sorted(names))}
    out = bytearray()
    out += itch_frame(itch_system("O", hms(3, 0)))
    for i, s in enumerate(sorted(names)):
        out += itch_frame(itch_directory(s, loc[s], hms(3, 5) + i))
    out += itch_frame(itch_system("S", hms(4, 0)))
    out += itch_frame(itch_system("Q", hms(9, 30)))
    for e in events:
        lc = loc[e.sym]
        if e.kind == "add":
            m = _ih("A", lc, e.ts) + struct.pack(">Q", e.oid) + e.side.encode() + struct.pack(">I", e.size)
            m += e.sym.encode().ljust(8) + struct.pack(">I", e.price)
        elif e.kind == "delete":
            m = _ih("D", lc, e.ts) + struct.pack(">Q", e.oid)
        else:
            m = _ih("E", lc, e.ts) + struct.pack(">QIQ", e.oid, e.size, e.match)
        out += itch_frame(m)
    if close:
        out += itch_frame(itch_system("M", hms(16, 0)))
        out += itch_frame(itch_system("C", hms(20, 0)))
    return bytes(out)


# ---- IEX DEEP+ in IEX-TP in pcapng --------------------------------------------------------------------------------

TP = struct.Struct("<BBHIIHHqqq")


def iex_msg(kind: str, ts: int, sym: str = "", **f: int) -> bytes:
    """One DEEP+ Message Data (no length prefix); `ts` in ns since the epoch."""
    s8 = sym.encode().ljust(8)
    if kind == "S":
        return b"S" + bytes([f["event"]]) + struct.pack("<q", ts)
    if kind == "D":
        return b"D" + b"\x00" + struct.pack("<q", ts) + s8 + struct.pack("<Iqb", 100, 0, 1)
    if kind == "a":
        side = b"8" if f["side"] == ord("B") else b"5"
        return b"a" + side + struct.pack("<q", ts) + s8 + struct.pack("<qIq", f["oid"], f["size"], f["price"])
    if kind == "R":
        return b"R" + b"\x00" + struct.pack("<q", ts) + s8 + struct.pack("<q", f["oid"])
    if kind == "L":
        return (
            b"L"
            + b"\x00"
            + struct.pack("<q", ts)
            + s8
            + struct.pack("<qIqq", f["oid"], f["size"], f["price"], f["trade"])
        )
    raise ValueError(kind)


def tp_segment(seq: int, msgs: list[bytes], send: int, *, chan: int = 1, sess: int = 77, proto: int = 0x8005) -> bytes:
    payload = b"".join(struct.pack("<H", len(m)) + m for m in msgs)
    return TP.pack(1, 0, proto, chan, sess, len(payload), len(msgs), 0, seq, send) + payload


def eth_ipv4_udp(payload: bytes, *, vlan: bool = False) -> bytes:
    udp = struct.pack(">HHHH", 10378, 10378, 8 + len(payload), 0) + payload
    ip = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0) + bytes([10, 0, 0, 1, 233, 215, 21, 8])
    eth = b"\x01\x00\x5e\x57\x15\x08" + b"\x02" * 6
    if vlan:
        eth += b"\x81\x00\x00\x05"
    return eth + b"\x08\x00" + ip + udp


def pcapng(frames: list[tuple[int, bytes]], *, tsresol: int | None = None, linktype: int = 1) -> bytes:
    """SHB + one IDB (default microsecond resolution) + one EPB per (capture ns, frame)."""
    out = bytearray()
    shb = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
    out += struct.pack("<II", 0x0A0D0D0A, 12 + len(shb)) + shb + struct.pack("<I", 12 + len(shb))
    opts = b""
    if tsresol is not None:
        opts = struct.pack("<HHB3x", 9, 1, tsresol) + struct.pack("<HH", 0, 0)
    idb = struct.pack("<HHI", linktype, 0, 65535) + opts
    out += struct.pack("<II", 1, 12 + len(idb)) + idb + struct.pack("<I", 12 + len(idb))
    per_s = 10**tsresol if tsresol is not None else 1_000_000
    for ts, fr in frames:
        ticks = ts * per_s // NS
        pad = (-len(fr)) % 4
        body = struct.pack("<IIIII", 0, ticks >> 32, ticks & 0xFFFFFFFF, len(fr), len(fr)) + fr + b"\x00" * pad
        out += struct.pack("<II", 6, 12 + len(body)) + body + struct.pack("<I", 12 + len(body))
    return bytes(out)


def iex_day(
    date: str,
    symbols: list[str],
    events: list[Ev],
    *,
    extra_symbols: tuple[str, ...] = ("ZZZT",),
    per_segment: int = 3,
    gap_at: int | None = None,
    duplicate_at: int | None = None,
    heartbeat_every: int = 0,
    seed: int = 5,
) -> bytes:
    """pcapng bytes of the events as DEEP+ segments on one channel; `gap_at` drops the segment with that index (a
    sequence gap), `duplicate_at` repeats one, `heartbeat_every` inserts heartbeats."""
    rng = random.Random(seed)
    mid = midnight_utc_ns(date)
    msgs: list[tuple[int, bytes]] = [(mid + hms(3, 0), iex_msg("S", mid + hms(3, 0), event=ord("O")))]
    for i, s in enumerate(sorted([*symbols, *extra_symbols])):
        msgs.append((mid + hms(3, 5) + i, iex_msg("D", mid + hms(3, 5) + i, s)))
    msgs.append((mid + hms(4, 0), iex_msg("S", mid + hms(4, 0), event=ord("S"))))
    msgs.append((mid + hms(9, 30), iex_msg("S", mid + hms(9, 30), event=ord("R"))))
    for e in events:
        t = mid + e.ts
        if e.kind == "add":
            m = iex_msg("a", t, e.sym, side=ord(e.side), oid=e.oid, size=e.size, price=e.price)
        elif e.kind == "delete":
            m = iex_msg("R", t, e.sym, oid=e.oid)
        else:
            m = iex_msg("L", t, e.sym, oid=e.oid, size=e.size, price=e.price, trade=e.match)
        msgs.append((t, m))
    frames: list[tuple[int, bytes]] = []
    seq = 1
    i = 0
    k = 0
    while i < len(msgs):
        n = rng.randint(1, per_segment)
        chunk = msgs[i : i + n]
        send = chunk[-1][0]
        seg = tp_segment(seq, [m for _, m in chunk], send)
        if k != gap_at:
            frames.append((send, eth_ipv4_udp(seg)))
        if k == duplicate_at:
            frames.append((send, eth_ipv4_udp(seg)))
        if heartbeat_every and k % heartbeat_every == heartbeat_every - 1:
            frames.append((send, eth_ipv4_udp(tp_segment(seq + len(chunk), [], send))))
        seq += len(chunk)
        i += n
        k += 1
    return pcapng(frames)


def write_gz(path, data: bytes, members: int = 1) -> None:
    """gzip with `members` members (mtime 0)."""
    with open(path, "wb") as f:
        step = max(1, len(data) // members + 1)
        for j in range(0, len(data), step):
            f.write(gzip.compress(data[j : j + step], mtime=0))
