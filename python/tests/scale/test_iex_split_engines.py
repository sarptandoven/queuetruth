"""`scripts/scale/iex_split.py --engine fast` (Rust, `queuetruth._core.IexSplit`) against `--engine python`, the
reference, on generated captures of every layout the reader accepts, cut into pieces at random: the same bucket
bytes, counters and maps, the same files and manifest (but `timings`), and an error wherever the reference stops."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import random
import struct
from pathlib import Path
from typing import Any

import iex_split
import pytest

from queuetruth.demo.iex import SYMBOL_TYPES, CaptureError

pytestmark = pytest.mark.requires_core

MID = 1_791_518_400_000_000_000  # New York midnight of 2026-10-09, UTC ns
DATE = "2026-10-09"
MMDD = "10092026"
SYMBOLS = ["AAPL", "MSFT", "SPY", "TSLA", "ZIEXT", "A", "BRK.B", "X Y", "LONGSYMB", "aapl"]
# symbol fields that strip to one of the names above (trailing tab, separator 0x1c, newline)
ALIASES = [b"AAPL\t   ", b"SPY\x1c    ", b"A      \n"]
SYM_TYPES = sorted(SYMBOL_TYPES)
UNKNOWN_TYPES = [0x00, ord("Z"), ord("x"), 0x7F, 0xFF]
REASONS = [b"    ", b"LUDP", b" MW ", b"\xffAB ", b"\x1cX\t ", b"NONE"]
# Python exceptions the reference stops with on the inputs `wild` generates (the fast engine raises CaptureError)
REFERENCE_STOPS = (CaptureError, IndexError, struct.error, UnicodeEncodeError)
# the odd inputs of a `wild` capture, one kind per capture: a non-ASCII symbol field, renumbered sequence numbers past
# int64, a segment too long to renumber, an if_tsresol option cut by its block end, an Enhanced Packet Block shorter
# than its header, a capture time after 2106
WILD = ["ascii", "int64", "long", "tsresol", "short_epb", "time"]


# ---- generated captures ----------------------------------------------------------------------------------------


def _sym8(rng: random.Random, wild: str) -> bytes:
    if wild == "ascii" and rng.random() < 0.003:
        return b"AB\xc3\xa9    "  # a non-ASCII symbol field
    if rng.random() < 0.1:
        return rng.choice(ALIASES)
    return rng.choice(SYMBOLS).encode().ljust(8)[:8]


def message(rng: random.Random, t: int, wild: str) -> bytes:
    """One DEEP+ Message Data: mostly well formed, some short, empty, unknown or odd."""
    ts = struct.pack("<q", t)
    r = rng.random()
    if r < 0.04:
        m = b"S" + bytes([rng.choice(b"OSRMEC")]) + ts
        return m[: rng.randint(1, 9)] if rng.random() < 0.15 else m
    if r < 0.06:
        return b""
    if r < 0.09:
        return bytes([rng.choice(UNKNOWN_TYPES)]) + rng.randbytes(rng.randint(0, 30))
    if r < 0.15:
        m = b"H" + bytes([rng.choice(b"THOP")]) + ts + _sym8(rng, wild) + rng.choice(REASONS)
        return m[: rng.randint(18, 21)] if rng.random() < 0.1 else m + rng.randbytes(rng.randint(0, 3))
    if r < 0.20:
        return b"D" + bytes([rng.choice([0, 0x80, 0xFF, 0x7F])]) + ts + _sym8(rng, wild) + rng.randbytes(13)
    if r < 0.23:
        return bytes([rng.choice(SYM_TYPES)]) + rng.randbytes(rng.randint(0, 16))  # shorter than 18 bytes
    t_ = rng.choice(SYM_TYPES)
    return bytes([t_, rng.choice(b"\x0085B")]) + ts + _sym8(rng, wild) + rng.randbytes(rng.randint(0, 30))


def segments(rng: random.Random, n: int, wild: str) -> list[tuple[int, bytes]]:
    """(send time, IEX-TP segment) on three (channel, session) streams with gaps, duplicates, partial duplicates,
    heartbeats, other protocols and bad headers."""
    keys = [(1, 77), (1, 78), (2, 77)]
    nxt = {k: rng.randint(1, 5) for k in keys}
    history: list[tuple[tuple[int, int], int, list[bytes]]] = []
    t = MID + 3 * 3600 * 10**9
    out = []
    for _ in range(n):
        t += rng.randint(1, 10**7)
        key = rng.choice(keys) if rng.random() < 0.2 else keys[0]
        r = rng.random()
        if r < 0.05 and history:  # an earlier segment again
            key, seq, msgs = rng.choice(history)
        else:
            msgs = [message(rng, t, wild) for _ in range(rng.choice([1, 1, 1, 2, 2, 3, 5]))]
            if r < 0.10 and len(msgs) > 1:  # overlaps the end of what was seen
                seq = nxt[key] - rng.randint(1, len(msgs) - 1)
            elif r < 0.16:  # a gap
                seq = nxt[key] + rng.randint(1, 50)
            elif r < 0.20:  # a heartbeat
                msgs, seq = [], nxt[key]
            else:
                seq = nxt[key]
            nxt[key] = max(nxt[key], seq + len(msgs))
            history.append((key, seq, msgs))
        if wild == "long" and rng.random() < 0.01:  # a renumbered segment too long for one datagram (65520 bytes)
            msgs = [b"S" + bytes([ord("O")]) + struct.pack("<q", t)] * 5460
        body = b"".join(struct.pack("<H", len(m)) + m for m in msgs)
        plen, count, version, proto = len(body), len(msgs), 1, 0x8005
        q = rng.random()
        if q < 0.02:
            version = rng.choice([0, 2])
        elif q < 0.04:
            proto = rng.choice([0x8004, 0x8003, 0x0001])
        elif q < 0.06:
            plen = max(0, min(65535, plen + rng.randint(-6, 6)))
        elif q < 0.08:
            count = max(0, min(65535, count + rng.randint(-1, 2)))
        seq64 = (seq + 2**63) % 2**64 - 2**63  # as int64 wraps
        seg = struct.pack("<BBHIIHHqqq", version, 0, proto, key[0], key[1], plen, count, rng.randint(0, 9), seq64, t)
        seg += body
        if q > 0.97:
            seg += rng.randbytes(rng.randint(1, 20)) + rng.choice([b"", b"SPY     ", b"\x0a\x00S"])  # after plen
        if 0.08 <= q < 0.09:
            seg = seg[: rng.randint(0, 39)]
        out.append((t, seg))
    if wild == "int64":
        # a new stream whose five messages end past int64, then an overlap whose new messages are numbered there
        ev = struct.pack("<H", 10) + b"SO" + struct.pack("<q", t)
        hdr = [struct.pack("<BBHIIHHqqq", 1, 0, 0x8005, 3, 7, 5 * len(ev), 5, 0, s, t) for s in (2**63 - 3, 2**63 - 1)]
        at = rng.randint(len(out) // 2, len(out))
        out[at:at] = [(t, h + ev * 5) for h in hdr]
    return out


def frame(rng: random.Random, payload: bytes, linktype: int) -> bytes:
    """A frame of `linktype` carrying `payload` in IPv4 / UDP; some with VLAN tags, IPv4 options, odd lengths, or
    not IPv4 / UDP at all."""
    ulen = 8 + len(payload)
    r = rng.random()
    if r < 0.02:
        ulen = rng.randint(0, 7)
    elif r < 0.04:
        ulen = min(65535, ulen + rng.randint(1, 30))
    udp = struct.pack(">HHHH", 10378, 10378, ulen if ulen <= 0xFFFF else 0, 0) + payload
    ihl = 6 if rng.random() < 0.03 else 5
    total = ihl * 4 + len(udp)
    r = rng.random()
    if r < 0.02:
        total = 0
    elif r < 0.04:
        total = rng.randint(1, total)
    elif r < 0.05:
        total += rng.randint(1, 30)
    total = total if total <= 0xFFFF else 0  # 0: the frame's end
    flags = rng.choice([0x2000, 0x0010]) if rng.random() < 0.02 else 0x4000
    proto = 6 if rng.random() < 0.02 else 17
    dst = bytes([233, 215, rng.choice([21, 149]), rng.choice([8, 200])])
    ip = struct.pack(">BBHHHBBH", 0x40 | ihl, 0, total, 0, flags, 64, proto, 0) + bytes([10, 0, 0, 1]) + dst
    ip += b"\x01\x01\x01\x00" * (ihl - 5)
    if rng.random() < 0.01:
        ip = bytes([0x60]) + ip[1:]  # IPv6 version nibble
    if linktype == 1:
        eth = b"\x01\x00\x5e\x57\x15\x08" + b"\x02" * 6
        for _ in range(rng.choice([0] * 20 + [1, 1, 2])):
            eth += struct.pack(">HH", rng.choice([0x8100, 0x88A8]), rng.randint(0, 4095))
        eth += b"\x08\x00" if rng.random() > 0.02 else rng.choice([b"\x08\x06", b"\x86\xdd", b"\x81\x00"])
        fr = eth + ip + udp
    elif linktype == 113:
        fr = (
            b"\x00\x00\x00\x01\x00\x06" + b"\x02" * 8 + (b"\x08\x00" if rng.random() > 0.02 else b"\x08\x06") + ip + udp
        )
    else:
        fr = ip + udp
    if rng.random() < 0.02:
        fr = fr[: rng.randint(0, len(fr))]
    return fr


def _pcap(rng: random.Random, pkts: list[tuple[int, bytes]], big: bool, ns: bool, linktype: int) -> bytes:
    e = ">" if big else "<"
    out = bytearray(struct.pack(e + "IHHiIII", 0xA1B23C4D if ns else 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype))
    for ts, fr in pkts:
        sec, frac = divmod(ts, 10**9)
        frac = frac if ns else frac // 1000
        if rng.random() < 0.01:
            frac += 10**6 * rng.randint(1, 3)  # a fraction past one second, as the reader accepts it
        if sec > 0xFFFFFFFF:
            sec, frac = 0xFFFFFFFF, 0xFFFFFFFF  # the latest stamp: a few seconds after 2106
        out += struct.pack(e + "IIII", sec, frac, len(fr), len(fr)) + fr
    return bytes(out)


def _ticks(ts: int, tps: int) -> int:
    return ts * tps // 10**9 % 2**64  # a resolution too fine for 64 bits wraps (odd times, still valid input)


def _tps(resol: int | None) -> int:
    if resol is None:
        return 10**6
    return 2 ** (resol & 0x7F) if resol & 0x80 else 10 ** (resol & 0x7F)


def _pcapng(rng: random.Random, pkts: list[tuple[int, bytes]], big: bool, linktype: int, wild: str) -> bytes:
    """pcapng with one or more interfaces (any if_tsresol), Simple Packet Blocks, skipped blocks, a second section
    in the other byte order, and Enhanced Packet Blocks whose captured length runs past the block."""
    out = bytearray()
    state = {"e": ">" if big else "<", "ifaces": []}

    def blk(btype: int, body: bytes) -> bytes:
        e = state["e"]
        pad = (-len(body)) % 4
        n = 12 + len(body) + pad
        return struct.pack(e + "II", btype, n) + body + b"\x00" * pad + struct.pack(e + "I", n)

    def section() -> None:
        e = state["e"]
        out.extend(blk(0x0A0D0D0A, struct.pack(e + "IHHq", 0x1A2B3C4D, 1, 0, -1)))
        state["ifaces"] = []
        for _ in range(rng.choice([1, 1, 1, 2])):
            resol = rng.choice([None, None, 6, 9, 3, 0, 0x80 | 20, 0x80 | 30, 0x80 | 33, 12, 0x80 | 40])
            opts = b""
            if rng.random() < 0.2:
                opts += struct.pack(e + "HH", 2, 3) + b"eth\x00"  # if_name before if_tsresol
            if resol is not None:
                opts += struct.pack(e + "HHB3x", 9, 1, resol)
            if rng.random() < 0.5:
                opts += struct.pack(e + "HH", 0, 0)
            if wild == "tsresol" and rng.random() < 0.3:
                opts = struct.pack(e + "HH", 9, 1)  # if_tsresol cut by the end of the block
            out.extend(blk(1, struct.pack(e + "HHI", linktype, 0, 65535) + opts))
            state["ifaces"].append(_tps(resol))

    section()
    for ts, fr in pkts:
        e = state["e"]
        r = rng.random()
        if r < 0.01:
            state["e"] = "<" if e == ">" else ">"  # a new section in the other byte order
            section()
            e = state["e"]
        elif r < 0.02:
            out.extend(blk(rng.choice([5, 0xBAD, 4]), rng.randbytes(rng.randint(0, 3) * 4)))
        if rng.random() < 0.03:  # Simple Packet Block (no time)
            orig = len(fr) + rng.choice([0, 0, -2, 5])
            out.extend(blk(3, struct.pack(e + "I", max(0, orig)) + fr))
            continue
        iface = 0 if rng.random() > 0.03 else rng.choice([1, 5])
        tps = state["ifaces"][iface] if iface < len(state["ifaces"]) else 10**6
        ticks = _ticks(ts, tps)
        cap = len(fr)
        if rng.random() < 0.01:
            cap += rng.randint(1, 80)  # runs into the next block
        elif rng.random() < 0.01:
            cap = rng.randint(0, cap)
        out.extend(blk(6, struct.pack(e + "IIIII", iface, ticks >> 32, ticks & 0xFFFFFFFF, cap, len(fr)) + fr))
        if wild == "short_epb" and rng.random() < 0.01:
            out.extend(blk(6, b"\x00" * rng.choice([0, 4, 8, 12])))  # shorter than its header: reads the next block
    if wild == "short_epb":
        out.extend(blk(6, b"\x00" * rng.choice([0, 4, 8, 12])))  # ... and at the end, where there is none
    return bytes(out)


def capture(
    seed: int, *, n: int = 300, wild: bool = False, layout: str | None = None, linktype: int | None = None
) -> tuple[bytes, str]:
    """(capture bytes, "layout linktype [wild kind]") for a seed: pcap (either byte order, us or ns) or pcapng (either
    byte order), on Ethernet mostly, else raw IPv4, Linux SLL or an unknown link type; `wild` adds one kind of `WILD`
    input."""
    rng = random.Random(seed)
    layout = layout or rng.choice(["pcap-le-ns", "pcap-le-us", "pcap-be-ns", "pcap-be-us", "pcapng-le", "pcapng-be"])
    linktype = linktype or rng.choice([1] * 12 + [101, 228, 113, 147])
    kind = rng.choice(WILD) if wild else ""
    pkts = [(ts, frame(rng, seg, linktype)) for ts, seg in segments(rng, n, kind)]
    if kind == "time":  # a System Event (written to every bucket) captured after 2106
        ev = b"S" + b"O" + struct.pack("<q", MID)
        seg = struct.pack("<BBHIIHHqqq", 1, 0, 0x8005, 3, 99, 12, 1, 0, 1, MID) + struct.pack("<H", 10) + ev
        pkts.insert(rng.randint(0, len(pkts)), (2**32 * 10**9 + rng.randint(0, 10**9), frame(rng, seg, linktype)))
    desc = f"{layout} {linktype} {kind}".strip()
    if layout.startswith("pcapng"):
        return _pcapng(rng, pkts, layout.endswith("be"), linktype, kind), desc
    big, ns = "-be-" in layout, layout.endswith("ns")
    return _pcap(rng, pkts, big, ns, linktype), desc


# ---- the two engines ---------------------------------------------------------------------------------------------


def _pieces(data: bytes, rng: random.Random) -> list[bytes]:
    out, i = [], 0
    while i < len(data):
        n = rng.choice([1, 2, 3, 7, 16, 40, 100, 1000, 4096, 1 << 16, rng.randint(1, 3000)])
        out.append(data[i : i + n])
        i += n
    return out


def run_engine(engine: str, pieces: list[bytes], k: int, symbols: set[str] | None, threads: int = 2) -> Any:
    """The splitter after `pieces`, its bucket bytes, or the exception it stopped with."""
    outs = [io.BytesIO() for _ in range(k)]
    if engine == "fast":
        sp: Any = iex_split.FastIexSplitter(k, list(outs), MID, symbols, threads=threads)
    else:
        sp = iex_split.IexSplitter(k, list(outs), MID, symbols)
    try:
        for p in pieces:
            sp.feed(p)
        sp.finish()
    except Exception as e:
        return e, None
    finally:
        sp.close()
    return sp, [o.getvalue() for o in outs]


def state(sp: Any) -> dict[str, Any]:
    return {
        "c": dict(+sp.c),
        "msgs": dict(sp.msgs),
        "execs": dict(+sp.execs),
        "ids": sp.ids,
        "actions": sp.actions,
        "test_issues": sp.test_issues,
        "system_events": list(sp.system_events.items()),
        "bucket_msgs": sp.bucket_msgs,
        "bucket_segments": sp.bucket_segments,
        "skipped_packets": list(sp.skipped_packets.items()),
        "other_protocol": list(sp.other_protocol.items()),
        "unknown_types": list(sp.unknown_types.items()),
        "sessions": sp.sessions,
        "first_send_ns": sp.first_send_ns,
        "last_send_ns": sp.last_send_ns,
        "capture": (sp.capture.kind, sp.capture.pending, dict(sp.capture.blocks)),
        "content_sha256": [w.content_sha256.hexdigest() for w in sp.writers],
    }


def assert_same(data: bytes, seed: int, k: int, symbols: set[str] | None, *, threads: int = 2) -> str:
    """Runs both engines on the same random pieces of `data`; returns `ok` or `stopped` (both raised)."""
    pieces = _pieces(data, random.Random(seed ^ 0x5EED))
    ref, ref_out = run_engine("python", pieces, k, symbols)
    fast, fast_out = run_engine("fast", pieces, k, symbols, threads)
    if isinstance(ref, BaseException):
        assert isinstance(ref, REFERENCE_STOPS), repr(ref)
        assert isinstance(fast, CaptureError), f"the reference stopped ({ref!r}), the fast engine did not"
        if isinstance(ref, CaptureError):
            assert str(fast) == str(ref)
        return "stopped"
    assert not isinstance(fast, BaseException), f"the fast engine stopped ({fast!r}), the reference did not"
    assert fast_out == ref_out
    assert state(fast) == state(ref)
    return "ok"


@pytest.mark.parametrize("seed", range(120))
def test_engines_agree_on_generated_captures(seed: int) -> None:
    data, layout = capture(seed)
    rng = random.Random(seed)
    k = rng.choice([1, 2, 3, 7, 32])
    symbols = {rng.choice(SYMBOLS).upper() for _ in range(rng.randint(1, 3))} if rng.random() < 0.3 else None
    assert assert_same(data, seed, k, symbols, threads=rng.choice([1, 2, 3])) == "ok", layout


@pytest.mark.parametrize("layout", ["pcap-le-ns", "pcap-le-us", "pcap-be-ns", "pcap-be-us", "pcapng-le", "pcapng-be"])
def test_every_layout_routes_and_skips(layout: str) -> None:
    """Each layout on Ethernet, raw IPv4 and Linux SLL: routed symbols, skipped packets, and the counters the
    differential test compares are not all zero."""
    for seed, linktype in enumerate([1, 1, 101, 228, 113, 147]):
        data, _ = capture(1000 + seed, n=400, layout=layout, linktype=linktype)
        assert assert_same(data, seed, 5, None) == "ok"
    sp, _ = run_engine("fast", [capture(1000, n=400, layout=layout, linktype=1)[0]], 5, None)
    st = state(sp)
    assert st["c"]["routed"] > 0 and st["c"]["system"] > 0 and st["c"]["sequence_gaps"] > 0
    assert st["c"]["duplicate_messages"] > 0 and st["c"]["heartbeats"] > 0 and st["sessions"]
    assert st["skipped_packets"] and st["c"]["malformed"] > 0 and st["c"]["unknown_type"] > 0


def test_symbol_filter_and_prefilter_agree() -> None:
    for seed in range(30):
        data, _ = capture(2000 + seed)
        symbols = {"SPY", "AAPL"} if seed % 2 else {"X Y", "NOPE", "LONGSYMB"}
        assert assert_same(data, seed, 3, symbols) == "ok"
        sp, _ = run_engine("fast", [data], 3, symbols)
        assert sp.c["prefiltered"] > 0 or sp.c["messages"] == 0


@pytest.mark.parametrize("seed", range(60))
def test_engines_stop_on_the_same_inputs(seed: int) -> None:
    """Inputs on which the reference may stop: a non-ASCII symbol, a cut interface option, an Enhanced Packet Block
    shorter than its header, a capture time after 2106, a segment too long to renumber, sequence numbers past int64.
    The fast engine stops exactly where the reference does, and agrees with it everywhere else."""
    data, _ = capture(3000 + seed, wild=True)
    rng = random.Random(seed)
    symbols = {"SPY", "AAPL"} if rng.random() < 0.3 else None
    assert_same(data, seed, rng.choice([1, 3, 7]), symbols)


def test_every_wild_kind_stops_both_engines() -> None:
    """Each kind of `WILD` input does stop the reference (and so the fast engine) on some seeds."""
    stopped, ok = set(), 0
    for s in range(200):
        data, desc = capture(3000 + s, wild=True)
        if assert_same(data, s, 3, None) == "stopped":
            stopped.add(desc.split()[-1])
        else:
            ok += 1
    assert stopped == set(WILD) and ok > 0


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x0a\x0d\x0d",
        b"definitely not a pcap file at all",
        b"\x0a\x0d\x0d\x0a" + b"\x00" * 20,  # a section header with length 0
        struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1) + struct.pack("<IIII", 1, 0, 10**6, 10**6),
    ],
)
def test_odd_inputs_agree(data: bytes) -> None:
    assert_same(data, 0, 2, None)
    assert_same(data, 1, 2, None)


# ---- run_split: files and manifest ---------------------------------------------------------------------------------


def _args(
    src: Path, out: Path, engine: str, k: int, symbols: str | None = None, threads: int = 2
) -> argparse.Namespace:
    return argparse.Namespace(
        input=str(src),
        url=None,
        date=DATE,
        buckets=k,
        out=out,
        symbols=symbols,
        level=1,
        min_free_gib=0.0,
        engine=engine,
        threads=threads,
    )


def _files(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


def _manifest(d: Path) -> dict[str, Any]:
    m = json.loads((d / "split_manifest.json").read_text())
    m.pop("timings")
    return m


@pytest.mark.parametrize(
    ("seed", "gz", "k", "symbols"),
    [(1, True, 3, None), (2, False, 7, None), (3, True, 2, "SPY,AAPL"), (4, True, 1, None), (5, False, 4, "x y,TSLA")],
)
def test_run_split_writes_the_same_files_and_manifest(tmp_path: Path, seed: int, gz: bool, k: int, symbols) -> None:
    data, _ = capture(seed, n=600)
    src = tmp_path / f"in-{seed}.pcap{'.gz' if gz else ''}"
    src.write_bytes(gzip.compress(data, compresslevel=1, mtime=0) if gz else data)
    rc = {}
    for engine in ("python", "fast"):
        rc[engine] = iex_split.run_split(_args(src, tmp_path / engine, engine, k, symbols))
    assert rc["fast"] == rc["python"] in (iex_split.EXIT_OK, iex_split.EXIT_VERIFY)
    py, fast = tmp_path / "python" / MMDD, tmp_path / "fast" / MMDD
    assert _files(fast) == {**_files(py), "split_manifest.json": _files(fast)["split_manifest.json"]}
    assert _manifest(fast) == _manifest(py)
    assert _manifest(fast)["stream"]["messages"] > 0


def test_run_split_input_errors_agree(tmp_path: Path) -> None:
    """Not a capture, a corrupt gzip stream, a missing file: both engines exit 2 and leave no bucket file."""
    data, _ = capture(7)
    cases = {
        "garbage.pcap": b"not a capture at all" * 10,
        "corrupt.pcap.gz": gzip.compress(data)[:200] + b"\x00" * 64 + gzip.compress(data)[264:],
    }
    for name, body in cases.items():
        (tmp_path / name).write_bytes(body)
    for name in [*cases, "missing.pcap"]:
        for engine in ("python", "fast"):
            out = tmp_path / f"{engine}-{name}"
            assert iex_split.run_split(_args(tmp_path / name, out, engine, 2)) == iex_split.EXIT_INPUT, (name, engine)
            assert not list((out / MMDD).glob("*.pcap.gz*")), (name, engine)
