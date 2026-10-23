"""Paths (puts `scripts/scale` on `sys.path`) and a small valid ITCH 5.0 day for the W1 scale-pipeline tests."""

from __future__ import annotations

import gzip
import random
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCALE = REPO / "scripts" / "scale"
FIXTURES = REPO / "tests" / "fixtures"
if str(SCALE) not in sys.path:
    sys.path.insert(0, str(SCALE))

NS = 1_000_000_000


def _hdr(t: bytes, loc: int, ts: int) -> bytes:
    return t + struct.pack(">HH", loc, 0) + ts.to_bytes(6, "big")


def itch_day(symbols: list[str], orders_per_symbol: int = 400, seed: int = 3) -> bytes:
    """A framed ITCH 5.0 day: system events, one `R` per symbol, then interleaved adds, executions, partial cancels
    and deletes around $100 (messages of all symbols mixed in time order)."""
    rng = random.Random(seed)
    out = bytearray()

    def frame(m: bytes) -> None:
        out.extend(struct.pack(">H", len(m)) + m)

    frame(_hdr(b"S", 0, 4 * 3600 * NS) + b"O")
    for i, s in enumerate(symbols):
        body = s.encode().ljust(8) + b"Q" + b"N" + struct.pack(">I", 100) + b"N" + b"C" + b"Z " + b"P" + b"N"
        body += b" " + b"1" + b"N" + struct.pack(">I", 0) + b"N"
        frame(_hdr(b"R", i + 1, 4 * 3600 * NS + i) + body)
    frame(_hdr(b"S", 0, 9 * 3600 * NS + 30 * 60 * NS) + b"Q")
    ref = 1
    live: dict[int, dict[int, list[int]]] = {i + 1: {} for i in range(len(symbols))}
    ts = 9 * 3600 * NS + 31 * 60 * NS
    match = 1
    for _ in range(orders_per_symbol * len(symbols) * 2):
        ts += rng.randrange(1, 50_000_000)
        loc = rng.randrange(1, len(symbols) + 1)
        book = live[loc]
        op = rng.random()
        if op < 0.5 or not book:
            side = rng.choice(b"BS")
            px = 1_000_000 + (rng.randrange(0, 5) if side == ord("S") else -rng.randrange(1, 6)) * 100
            sh = rng.choice([100, 200, 300, 500])
            frame(
                _hdr(b"A", loc, ts)
                + struct.pack(">Q", ref)
                + bytes([side])
                + struct.pack(">I", sh)
                + symbols[loc - 1].encode().ljust(8)
                + struct.pack(">I", px)
            )
            book[ref] = [sh]
            ref += 1
            continue
        oid = rng.choice(sorted(book))
        left = book[oid][0]
        if op < 0.7:
            n = min(left, rng.choice([100, 200]))
            frame(_hdr(b"E", loc, ts) + struct.pack(">QIQ", oid, n, match))
            match += 1
        elif op < 0.8 and left > 100:
            n = 100
            frame(_hdr(b"X", loc, ts) + struct.pack(">QI", oid, n))
        else:
            n = left
            frame(_hdr(b"D", loc, ts) + struct.pack(">Q", oid))
        book[oid][0] -= n
        if book[oid][0] <= 0:
            del book[oid]
    frame(_hdr(b"S", 0, 16 * 3600 * NS) + b"M")
    frame(_hdr(b"S", 0, 20 * 3600 * NS) + b"C")
    return bytes(out)


def write_itch_day(path: Path, symbols: list[str], **kw: int) -> bytes:
    raw = itch_day(symbols, **kw)
    path.write_bytes(gzip.compress(raw, mtime=0))
    return raw
