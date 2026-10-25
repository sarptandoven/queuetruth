"""Streaming IEX DEEP+ symbol filter with a market-time cut (the `scripts/iex/stream_subset.py` rules, moved into the
package for `queuetruth demo`).

Input: an IEX HIST capture (pcapng or classic pcap, after gzip inflation) in arbitrary pieces. Each packet is walked
Ethernet / VLAN / IPv4 / UDP to its IEX-TP segment (version 1, Message Protocol ID 0x8005 = DEEP+). Kept messages:
every System Event (`S`, no symbol) and every message of a known DEEP+ type whose Symbol field (offset 10, 8 bytes)
is a requested symbol: `D H I O P E` (administrative) and `a M R L T B C` (trading); unknown types are dropped and
counted. A segment is parsed message by message only when its bytes contain one of the padded symbols or a System
Event pattern (one compiled-regex search per packet); every other segment contributes only its header to the
sequence bookkeeping.

Sequence numbers of the subset: kept messages are renumbered contiguously per (protocol, channel, session) and every
gap of the source stream is carried over as a jump of the same size, so `ingest --format iex_deep_plus` reports
exactly the source's gaps and none caused by the filter. Messages already seen (duplicate or overlapping segments)
are dropped and counted. Stream offsets are recomputed over the kept bytes; Send Time, session, channel and the
capture timestamps are kept.

Cut: the first DEEP+ segment whose Send Time (nanoseconds since the epoch) is at or after `cut_ns` ends the subset;
it and everything after it are not read.

Output: classic pcap (little endian, nanosecond timestamps, Ethernet frames carrying IPv4/UDP).
"""

from __future__ import annotations

import hashlib
import re
import struct
from collections import Counter
from typing import Any, BinaryIO

import numpy as np

from queuetruth.demo._chain import chain_from_zero

DEEP_PLUS_PROTOCOL = 0x8005
TP_HEADER = struct.Struct("<BBHIIHHqqq")  # version, reserved, protocol, channel, session, len, count, offset, seq, send
SYMBOL_TYPES = frozenset(b"DHIOPEaMRLTBC")
SYSTEM_EVENT = ord("S")
SYSTEM_EVENT_PATTERN = b"\x0a\x00S"  # Message Length 10 (LE) + type 'S'
_U16BE = struct.Struct(">H")


def _le(arr: np.ndarray, offs: np.ndarray, nbytes: int) -> np.ndarray:
    """Little-endian unsigned integers of `nbytes` bytes at byte offsets `offs` of `arr` (uint64)."""
    v = np.zeros(offs.size, dtype=np.uint64)
    for k in range(nbytes - 1, -1, -1):
        v = (v << np.uint64(8)) | arr[offs + k].astype(np.uint64)
    return v


class CaptureError(ValueError):
    """The bytes are not a pcap / pcapng capture (or a block is malformed)."""


class Capture:
    """Incremental pcapng / pcap reader: `feed(bytes)` then iterate `packets()` -> (ts_ns, linktype, frame)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0
        self.kind: str | None = None
        self.endian = "<"
        self.ifaces: list[tuple[int, int]] = []  # (linktype, ticks per second)
        self.pcap_linktype = 1
        self.pcap_ns = 1000
        self.blocks: Counter[int] = Counter()

    def feed(self, b: bytes) -> None:
        if self.pos > (1 << 22):
            del self.buf[: self.pos]
            self.pos = 0
        self.buf += b

    @property
    def pending(self) -> int:
        """Bytes received but not yet consumed (an incomplete block at the end of the input)."""
        return len(self.buf) - self.pos

    def _detect(self) -> bool:
        if len(self.buf) - self.pos < 24:
            return False
        magic = bytes(self.buf[self.pos : self.pos + 4])
        if magic == b"\x0a\x0d\x0d\x0a":
            self.kind = "pcapng"
            return True
        table = {
            b"\xd4\xc3\xb2\xa1": ("<", 1000),
            b"\xa1\xb2\xc3\xd4": (">", 1000),
            b"\x4d\x3c\xb2\xa1": ("<", 1),
            b"\xa1\xb2\x3c\x4d": (">", 1),
        }
        if magic not in table:
            raise CaptureError(f"not a pcap / pcapng capture (magic {magic.hex()})")
        self.kind = "pcap"
        self.endian, self.pcap_ns = table[magic]
        self.pcap_linktype = struct.unpack_from(self.endian + "I", self.buf, self.pos + 20)[0] & 0xFFFF
        self.pos += 24
        return True

    @staticmethod
    def _tsresol(opts: bytes, endian: str) -> int:
        """Ticks per second of an IDB (option 9, if_tsresol; default microseconds)."""
        i = 0
        while i + 4 <= len(opts):
            code, ln = struct.unpack_from(endian + "HH", opts, i)
            if code == 0:
                break
            if code == 9 and ln >= 1:
                v = opts[i + 4]
                return 2 ** (v & 0x7F) if v & 0x80 else 10 ** (v & 0x7F)
            i += 4 + ((ln + 3) & ~3)
        return 1_000_000

    def packets(self):
        buf = self.buf
        if self.kind is None and not self._detect():
            return
        if self.kind == "pcap":
            e = self.endian
            while len(buf) - self.pos >= 16:
                sec, frac, incl, _orig = struct.unpack_from(e + "IIII", buf, self.pos)
                if len(buf) - self.pos < 16 + incl:
                    return
                start = self.pos + 16
                self.pos = start + incl
                yield sec * 1_000_000_000 + frac * self.pcap_ns, self.pcap_linktype, bytes(buf[start : self.pos])
            return
        while len(buf) - self.pos >= 12:
            if buf[self.pos : self.pos + 4] == b"\x0a\x0d\x0d\x0a":
                bom = bytes(buf[self.pos + 8 : self.pos + 12])
                self.endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            e = self.endian
            btype, blen = struct.unpack_from(e + "II", buf, self.pos)
            if blen < 12 or blen % 4:
                raise CaptureError(f"pcapng block length {blen} at block {sum(self.blocks.values())}")
            if len(buf) - self.pos < blen:
                return
            body = self.pos + 8
            end = self.pos + blen - 4
            self.pos += blen
            self.blocks[btype] += 1
            if btype == 0x0A0D0D0A:
                self.ifaces = []
            elif btype == 1:
                linktype = struct.unpack_from(e + "H", buf, body)[0]
                tps = self._tsresol(bytes(buf[body + 8 : end]), e)
                self.ifaces.append((linktype, tps))
            elif btype == 6:
                iface, th, tl, cap, _orig = struct.unpack_from(e + "IIIII", buf, body)
                linktype, tps = self.ifaces[iface] if iface < len(self.ifaces) else (1, 1_000_000)
                ticks = (th << 32) | tl
                ts = ticks * (1_000_000_000 // tps) if tps <= 1_000_000_000 else ticks * 1_000_000_000 // tps
                yield ts, linktype, bytes(buf[body + 20 : body + 20 + cap])
            elif btype == 3:
                linktype = self.ifaces[0][0] if self.ifaces else 1
                orig = struct.unpack_from(e + "I", buf, body)[0]
                yield 0, linktype, bytes(buf[body + 4 : min(end, body + 4 + orig)])


def udp_payload(linktype: int, frame: bytes) -> tuple[bytes, bytes, bytes] | str:
    """(ip src+dst, udp ports, payload) of an IPv4/UDP frame, or the reason it is skipped."""
    if linktype == 1:
        if len(frame) < 14:
            return "short_frame"
        et, i = _U16BE.unpack_from(frame, 12)[0], 14
        while et in (0x8100, 0x88A8) and len(frame) >= i + 4:
            et, i = _U16BE.unpack_from(frame, i + 2)[0], i + 4
        if et != 0x0800:
            return "not_ipv4"
    elif linktype in (101, 228):
        i = 0
    elif linktype == 113:
        if len(frame) < 16 or _U16BE.unpack_from(frame, 14)[0] != 0x0800:
            return "not_ipv4"
        i = 16
    else:
        return "linktype"
    if len(frame) < i + 20 or frame[i] >> 4 != 4:
        return "not_ipv4"
    ihl = (frame[i] & 0x0F) * 4
    total = _U16BE.unpack_from(frame, i + 2)[0]
    flags_frag = _U16BE.unpack_from(frame, i + 6)[0]
    if frame[i + 9] != 17:
        return "not_udp"
    if flags_frag & 0x3FFF:
        return "ip_fragment"
    u = i + ihl
    if len(frame) < u + 8:
        return "short_frame"
    ulen = _U16BE.unpack_from(frame, u + 4)[0]
    end = min(len(frame), i + total if total else len(frame), u + ulen if ulen >= 8 else len(frame))
    return frame[i + 12 : i + 20], frame[u : u + 4], frame[u + 8 : end]


def ip_checksum(h: bytes) -> int:
    s = sum(struct.unpack(">10H", h))
    s = (s & 0xFFFF) + (s >> 16)
    s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


class PcapWriter:
    """Classic pcap, little endian, nanosecond timestamps, Ethernet frames carrying IPv4/UDP."""

    def __init__(self, f: BinaryIO | None) -> None:
        self.f = f
        self.content_sha256 = hashlib.sha256()
        self._write(struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1))

    def _write(self, b: bytes) -> None:
        self.content_sha256.update(b)
        if self.f is not None:
            self.f.write(b)

    def write(self, ts_ns: int, addrs: bytes, ports: bytes, payload: bytes) -> None:
        udp = ports + struct.pack(">HH", 8 + len(payload), 0) + payload
        ip = bytearray(struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0) + addrs)
        struct.pack_into(">H", ip, 10, ip_checksum(bytes(ip)))
        dst = addrs[4:8]
        mac = b"\x01\x00\x5e" + bytes([dst[1] & 0x7F, dst[2], dst[3]])  # IPv4 multicast MAC of the group
        frame = mac + b"\x00\x00\x00\x00\x00\x00" + b"\x08\x00" + bytes(ip) + udp
        hdr = struct.pack("<IIII", ts_ns // 1_000_000_000, ts_ns % 1_000_000_000, len(frame), len(frame))
        self._write(hdr + frame)


class IexCutFilter:
    """DEEP+ segment filter, renumbering and time cut (module docstring)."""

    def __init__(self, symbols: list[str], cut_ns: int, out: BinaryIO | None) -> None:
        self.symbols = [s.upper() for s in symbols]
        padded = sorted({s.ljust(8).encode("ascii") for s in self.symbols})
        self.wanted = frozenset(padded)
        self.rx = re.compile(b"|".join(re.escape(p) for p in [*padded, SYSTEM_EVENT_PATTERN]))
        self.cut_ns = int(cut_ns)
        self.writer = PcapWriter(out)
        self.capture = Capture()
        self.cut_reached = False
        self.packets = 0
        self.skipped_packets: Counter[str] = Counter()
        self.segments = 0
        self.heartbeats = 0
        self.messages = 0
        self.messages_parsed = 0
        self.other_protocol_segments: Counter[str] = Counter()
        self.bad_segments = 0
        self.sequence_gaps = 0
        self.sequence_gap_messages = 0
        self.duplicate_messages = 0
        self.sessions: list[dict[str, Any]] = []
        self.kept_by_type: Counter[str] = Counter()
        self.kept_by_symbol: dict[str, Counter[str]] = {}
        self.unknown_types_seen: Counter[str] = Counter()
        self.system_events: dict[str, int] = {}
        self.first_send_ns: int | None = None
        self.last_send_ns: int | None = None
        #: Send Time of the first DEEP+ segment seen, also when it is already past the cut (the date check)
        self.first_seen_ns: int | None = None
        self.kept = 0
        self.kept_segments = 0
        self._chans: dict[tuple[int, int, int], list[int]] = {}  # key -> [expected_src, next_new, new_offset]

    @property
    def market_ns(self) -> int | None:
        """Send Time of the last segment read (nanoseconds since the epoch)."""
        return self.last_send_ns

    def feed(self, data: bytes) -> bool:
        """Consumes `data`; True once the cut is reached (later input is ignored)."""
        if self.cut_reached:
            return True
        cap = self.capture
        cap.feed(data)
        fast = self._fast()
        if fast is not None:
            return fast
        for ts, lt, frame in cap.packets():
            r = udp_payload(lt, frame)
            if isinstance(r, str):
                self.packets += 1
                self.skipped_packets[r] += 1
                continue
            if self.segment(ts, *r):
                self.cut_reached = True
                return True
        return False

    def _register(self, chan: int, sess: int, seq: int) -> list[int]:
        key = (DEEP_PLUS_PROTOCOL, chan, sess)
        ch = self._chans.get(key)
        if ch is None:
            ch = self._chans[key] = [seq, seq, 0]
            self.sessions.append(
                {"protocol": hex(DEEP_PLUS_PROTOCOL), "channel": chan, "session": sess, "first_seq": seq}
            )
        return ch

    def segment(self, ts_ns: int, addrs: bytes, ports: bytes, p: bytes) -> bool:
        """One IEX-TP segment; True when it is at or past the cut (and was not consumed)."""
        if len(p) < 40 or p[0] != 1:
            self.packets += 1
            self.bad_segments += 1
            return False
        _v, _r, proto, chan, sess, plen, count, _off, seq, send = TP_HEADER.unpack_from(p, 0)
        if proto != DEEP_PLUS_PROTOCOL:
            self.packets += 1
            self.other_protocol_segments[hex(proto)] += 1
            return False
        if self.first_seen_ns is None:
            self.first_seen_ns = send
        if send >= self.cut_ns:
            return True
        self.packets += 1
        self.segments += 1
        if self.first_send_ns is None:
            self.first_send_ns = send
        self.last_send_ns = send
        ch = self._register(chan, sess, seq)
        if count == 0:
            self.heartbeats += 1
            return False
        self.messages += count
        expected = ch[0]
        if seq > expected:
            self.sequence_gaps += 1
            self.sequence_gap_messages += seq - expected
            ch[1] += seq - expected
            ch[0] = seq
        elif seq + count <= expected:
            self.duplicate_messages += count
            return False
        skip = expected - seq if seq < expected else 0
        self.duplicate_messages += skip
        ch[0] = seq + count
        if self.rx.search(p, 40) is None:
            return False  # nothing kept: the renumbered stream counts kept messages only
        self.messages_parsed += count
        kept = self._keep(memoryview(p)[40 : 40 + plen], count, skip)
        if kept:
            self._write(ch, ts_ns, addrs, ports, chan, sess, send, kept)
        return False

    def _keep(self, body: memoryview, count: int, skip: int) -> list[bytes]:
        """The kept messages of one segment body (the first `skip` were already seen)."""
        kept: list[bytes] = []
        i, n = 0, 0
        while n < count and i + 2 <= len(body):
            ln = body[i] | (body[i + 1] << 8)
            m = body[i + 2 : i + 2 + ln]
            i += 2 + ln
            n += 1
            if n <= skip or not len(m):
                continue
            t = m[0]
            if t == SYSTEM_EVENT:
                if len(m) >= 10:
                    self.system_events[chr(m[1])] = struct.unpack_from("<q", m, 2)[0]
                kept.append(bytes(m))
                self.kept_by_type["S"] += 1
            elif t in SYMBOL_TYPES:
                if len(m) >= 18 and bytes(m[10:18]) in self.wanted:
                    kept.append(bytes(m))
                    c = chr(t)
                    self.kept_by_type[c] += 1
                    sym = bytes(m[10:18]).decode("ascii").rstrip()
                    self.kept_by_symbol.setdefault(sym, Counter())[c] += 1
            else:
                self.unknown_types_seen[hex(t)] += 1
        return kept

    def _write(
        self, ch: list[int], ts_ns: int, addrs: bytes, ports: bytes, chan: int, sess: int, send: int, kept: list[bytes]
    ) -> None:
        payload = b"".join(struct.pack("<H", len(m)) + m for m in kept)
        hdr = TP_HEADER.pack(1, 0, DEEP_PLUS_PROTOCOL, chan, sess, len(payload), len(kept), ch[2], ch[1], send)
        ch[1] += len(kept)
        ch[2] += len(payload)
        self.kept += len(kept)
        self.kept_segments += 1
        self.writer.write(ts_ns, addrs, ports, hdr + payload)

    def _fast(self) -> bool | None:
        """Vectorized pass over the complete blocks of the capture buffer when every block in it is an Enhanced
        Packet Block of the single Ethernet interface carrying a plain IPv4 (IHL 5, unfragmented) / UDP / IEX-TP v1
        DEEP+ segment; None (nothing consumed) otherwise, and the scalar path takes the chunk. Same results as
        `segment` per packet: the sequence rule `expected <- max(expected, seq + count)` over non-heartbeat
        segments is a running maximum."""
        cap = self.capture
        if cap.kind != "pcapng" or cap.endian != "<" or len(cap.ifaces) != 1 or cap.ifaces[0][0] != 1:
            return None
        tps = cap.ifaces[0][1]
        if tps > 1_000_000_000 or 1_000_000_000 % tps:
            return None
        b = bytes(cap.buf[cap.pos :])
        n4 = len(b) // 4
        if n4 < 3:
            return False
        w = np.frombuffer(b, dtype="<u4", count=n4)
        cand = np.flatnonzero((w[:-1] == 6) & (w[1:] >= 32) & ((w[1:] & 3) == 0))
        if cand.size == 0 or int(cand[0]) != 0:
            return None
        end = cand + (w[cand + 1] // 4).astype(np.int64)
        inside = end <= n4
        ok = np.ones(cand.size, dtype=bool)
        ok[inside] = w[end[inside] - 1] == w[cand[inside] + 1]  # pcapng repeats the block length at the end
        cand, end = cand[ok], end[ok]
        path = chain_from_zero(cand, end, n4, 2)
        if path is None:
            return None
        if path.size == 0:
            return False
        blk = cand[path]
        arr = np.frombuffer(b, dtype=np.uint8)
        caplen = w[blk + 5].astype(np.int64)
        fr = blk * 4 + 28
        if int(caplen.min()) < 42 + 40 or bool((w[blk + 2] != 0).any()):
            return None
        total = (arr[fr + 16].astype(np.int64) << 8) | arr[fr + 17]
        ulen = (arr[fr + 38].astype(np.int64) << 8) | arr[fr + 39]
        P = fr + 42
        layout = (
            (arr[fr + 12] == 8)
            & (arr[fr + 13] == 0)
            & (arr[fr + 14] == 0x45)
            & (arr[fr + 23] == 17)
            & ((arr[fr + 20] & 0x3F) == 0)
            & (arr[fr + 21] == 0)
            & (total >= 28)
            & (ulen >= 8)
            & (arr[P] == 1)
            & (arr[P + 2] == 0x05)
            & (arr[P + 3] == 0x80)
        )
        if not bool(layout.all()):
            return None
        paylen = np.minimum(np.minimum(caplen - 42, total - 28), ulen - 8)
        if int(paylen.min()) < 40:
            return None
        send = _le(arr, P + 32, 8).astype(np.int64)
        if self.first_seen_ns is None:
            self.first_seen_ns = int(send[0])
        over = np.flatnonzero(send >= self.cut_ns)
        k = int(over[0]) if over.size else blk.size
        if over.size:
            self.cut_reached = True
        blk, fr, P, paylen, send = blk[:k], fr[:k], P[:k], paylen[:k], send[:k]
        if k == 0:
            return self.cut_reached
        cap.pos += int(end[path[k - 1]]) * 4
        chan = _le(arr, P + 4, 4).astype(np.int64)
        sess = _le(arr, P + 8, 4).astype(np.int64)
        plen = _le(arr, P + 12, 2).astype(np.int64)
        count = _le(arr, P + 14, 2).astype(np.int64)
        seq = _le(arr, P + 24, 8).astype(np.int64)
        ticks = (w[blk + 3].astype(np.uint64) << np.uint64(32)) | w[blk + 4].astype(np.uint64)
        ts = (ticks * np.uint64(1_000_000_000 // tps)).astype(np.int64)
        cap.blocks[6] += k
        self.packets += k
        self.segments += k
        if self.first_send_ns is None:
            self.first_send_ns = int(send[0])
        self.last_send_ns = int(send[-1])
        nh = count > 0
        self.heartbeats += int((~nh).sum())
        self.messages += int(count[nh].sum())
        # per (channel, session): register in order of first appearance, then the running-maximum sequence rule
        keys = chan * (1 << 32) + sess
        uk, first = np.unique(keys, return_index=True)
        dup = np.zeros(k, dtype=bool)
        skip = np.zeros(k, dtype=np.int64)
        gap_before = np.zeros(k, dtype=np.int64)  # gap messages of the key up to and including the segment
        chs: dict[int, list[int]] = {}
        base_new: dict[int, int] = {}
        for key, i0 in sorted(zip(uk.tolist(), first.tolist(), strict=True), key=lambda t: t[1]):
            ch = self._register(int(chan[i0]), int(sess[i0]), int(seq[i0]))
            chs[key] = ch
            base_new[key] = ch[1]
            idx = np.flatnonzero((keys == key) & nh)
            if idx.size == 0:
                continue
            s, c = seq[idx], count[idx]
            reach = np.maximum.accumulate(s + c)
            expected = np.maximum(ch[0], np.concatenate([[ch[0]], reach[:-1]]))
            gap = s > expected
            gmsgs = np.where(gap, s - expected, 0)
            d = ~gap & (s + c <= expected)
            sk = np.where(~gap & ~d & (s < expected), expected - s, 0)
            dup[idx], skip[idx] = d, sk
            gap_before[idx] = np.cumsum(gmsgs)
            self.sequence_gaps += int(gap.sum())
            self.sequence_gap_messages += int(gmsgs.sum())
            self.duplicate_messages += int(c[d].sum()) + int(sk.sum())
            ch[0] = max(ch[0], int(reach[-1]))
            ch[1] += int(gmsgs.sum())  # kept messages are added as they are written
        # segments whose body holds a wanted symbol or a System Event pattern
        spans = np.array([m.span() for m in self.rx.finditer(b)], dtype=np.int64).reshape(-1, 2)
        hits = np.zeros(k, dtype=bool)
        if spans.size:
            pk = np.searchsorted(P, spans[:, 0], side="right") - 1
            good = pk >= 0
            pk, st, en = pk[good], spans[good, 0], spans[good, 1]
            inside = (st >= P[pk] + 40) & (en <= P[pk] + paylen[pk])
            hits[pk[inside]] = True
        sel = np.flatnonzero(hits & nh & ~dup)
        if sel.size:
            self.messages_parsed += int(count[sel].sum())
        kept_new: dict[int, int] = dict.fromkeys(chs, 0)
        for i in sel.tolist():
            p0, pl = int(P[i]), int(paylen[i])
            body = memoryview(b)[p0 + 40 : p0 + min(pl, 40 + int(plen[i]))]
            kept = self._keep(body, int(count[i]), int(skip[i]))
            if not kept:
                continue
            key = int(keys[i])
            ch = chs[key]
            # the segment's new sequence number: the key's start, its gaps so far, its kept messages so far
            new_seq = base_new[key] + int(gap_before[i]) + kept_new[key]
            saved = ch[1]
            ch[1] = new_seq
            f0 = int(fr[i])
            self._write(
                ch,
                int(ts[i]),
                b[f0 + 26 : f0 + 34],
                b[f0 + 34 : f0 + 38],
                int(chan[i]),
                int(sess[i]),
                int(send[i]),
                kept,
            )
            kept_new[key] += len(kept)
            ch[1] = saved + len(kept)
        return self.cut_reached

    def finish(self) -> dict[str, Any]:
        """Statistics of the pass (call after the last `feed`)."""
        return {
            "packets_read": self.packets,
            "segments": self.segments,
            "heartbeats": self.heartbeats,
            "messages_read": self.messages,
            "messages_parsed": self.messages_parsed,
            "kept_messages": self.kept,
            "kept_segments": self.kept_segments,
            "content_sha256": self.writer.content_sha256.hexdigest(),
            "kept_by_type": dict(sorted(self.kept_by_type.items())),
            "kept_by_symbol": {k: dict(sorted(v.items())) for k, v in sorted(self.kept_by_symbol.items())},
            "missing_symbols": [s for s in self.symbols if s not in self.kept_by_symbol],
            "skipped_packets": dict(self.skipped_packets),
            "other_protocol_segments": dict(self.other_protocol_segments),
            "bad_segments": self.bad_segments,
            "sequence_gaps": self.sequence_gaps,
            "sequence_gap_messages": self.sequence_gap_messages,
            "duplicate_messages": self.duplicate_messages,
            "unknown_types_seen": dict(self.unknown_types_seen),
            "sessions": self.sessions,
            "system_events": self.system_events,
            "capture_format": self.capture.kind,
            "partial_block_bytes": self.capture.pending if not self.cut_reached else 0,
            "first_send_ns": self.first_send_ns,
            "last_send_ns": self.last_send_ns,
            "cut_reached": self.cut_reached,
        }


__all__ = ["Capture", "CaptureError", "IexCutFilter", "PcapWriter", "udp_payload"]
