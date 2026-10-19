#!/usr/bin/env python3
"""Stream one public IEX HIST DEEP+ day and keep only the messages of a few symbols.

    python3 -I scripts/iex/stream_subset.py --date 20261009 --symbols SPY,QQQ,AAPL,NVDA,TSLA
    python3 -I scripts/iex/stream_subset.py --self-test

IEX publishes every trading day of its DEEP+ feed (`DPLS`, order by order, since 2024-10-01) as one gzipped pcapng
capture of the IEX-TP multicast stream (about 8-18 GB compressed). The raw file is never stored. One pass:

    curl -fsSL <url> (resumed with a byte range after a dropped connection)  ->  sha256 of the compressed bytes  ->
    gzip inflate (multi-member)  ->  pcapng / pcap block walk  ->  Ethernet / VLAN / IPv4 / UDP  ->  IEX-TP segment
    (version 1, Message Protocol ID 0x8005 = DEEP+)  ->  per-message symbol filter  ->  subset pcap

Kept messages: every System Event (`S`, no symbol) and every message of a known DEEP+ type whose Symbol field (offset
10, 8 bytes) is a requested symbol: `D H I O P E` (administrative) and `a M R L T B C` (trading); unknown types are
dropped and counted. A packet is parsed message by message only when its bytes contain one of the padded symbols or
a System Event pattern (a substring pre-check at C speed); every other segment contributes only its header (sequence
numbers and message count) to the bookkeeping.

Sequence numbers of the subset: kept messages are renumbered contiguously per (protocol, channel, session) and every
gap of the source stream (a segment whose First Message Sequence Number is above the next expected one) is carried
over as a jump of the same size, so `queuetruth ingest --format iex_deep_plus` reports exactly the source's gaps (as
`sequence_gaps` / `feed_gaps`) and none caused by the filter. Messages already seen (duplicate or overlapping
segments) are dropped and counted. Stream offsets are recomputed over the kept bytes; Send Time, session, channel
and the capture timestamps are kept.

Outputs (gitignored), in `--out` (default `.realdata/iex`):
- `<date>_DPLS.subset.<SYM-SYM...>.pcap.gz`: classic pcap (nanosecond timestamps, Ethernet), gzip (mtime 0).
- `<date>_DPLS.subset.<SYM-SYM...>.manifest.json`: source URL, listed size, bytes streamed, sha256 of the
  compressed stream, decompressed bytes, packets / segments / heartbeats / messages, gaps and duplicates of the full
  stream, kept messages by type and symbol, the subset's sha256, timings (download MB/s, packets/s) and IEX's
  attribution line.

Guards: the free space on `/` is checked every few seconds; below `--min-free-gib` (default 2.5) the stream stops,
the partial subset is deleted and the exit code is 4. A subset above `--max-subset-mb` (default 300) stops with exit
code 4 as well. Exit codes: 0 ok, 1 a self-test failed, 2 network / input error, 4 disk or size guard.
Stdlib only; run with `python3 -I`.

Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data Terms
of Use (https://iextrading.com/iex-historical-data-terms/).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import shutil
import struct
import subprocess
import sys
import time
import zlib
from collections import Counter
from pathlib import Path
from typing import Any, BinaryIO

REPO = Path(__file__).resolve().parents[2]
OUT_DEFAULT = REPO / ".realdata" / "iex"
HIST_API = "https://iextrading.com/api/1.0/hist"
ATTRIBUTION = (
    "Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data "
    "Terms of Use."
)
TERMS_URL = "https://iextrading.com/iex-historical-data-terms/"
GIB = 1 << 30
READ_CHUNK = 4 << 20
EXIT_OK, EXIT_VERIFY, EXIT_INPUT, EXIT_DISK = 0, 1, 2, 4

DEEP_PLUS_PROTOCOL = 0x8005
TP_HEADER = struct.Struct("<BBHIIHHqqq")  # version, reserved, protocol, channel, session, len, count, offset, seq, send
SYMBOL_TYPES = frozenset(b"DHIOPEaMRLTBC")
SYSTEM_EVENT = ord("S")
SYSTEM_EVENT_PATTERN = b"\x0a\x00S"  # Message Length 10 (LE) + type 'S'


class Guard(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def free_gib(path: str = "/") -> float:
    return shutil.disk_usage(path).free / GIB


# ---------------------------------------------------------------------------------------------- listing and stream


def hist_entry(date: str, feed: str, listing: Path | None) -> dict[str, Any]:
    """The HIST listing entry of `date` / `feed` (e.g. DPLS)."""
    if listing is not None:
        data = json.loads(listing.read_text())
    else:
        r = subprocess.run(["curl", "-fsSL", "--max-time", "120", HIST_API], capture_output=True)
        if r.returncode != 0:
            raise OSError(f"GET {HIST_API}: curl exit {r.returncode}: {r.stderr.decode(errors='replace').strip()}")
        data = json.loads(r.stdout)
    for e in data.get(date, []) if isinstance(data, dict) else data:
        if e.get("feed") == feed and e.get("date") == date:
            return e
    raise OSError(f"no {feed} file for {date} in the IEX HIST listing")


class Download:
    """`curl` byte stream with up to `retries` resumptions from the current offset (HTTP range)."""

    def __init__(self, url: str, retries: int = 6, limit: int | None = None) -> None:
        self.url, self.retries, self.limit = url, retries, limit
        self.offset = 0
        self.resumes = 0
        self.sha256 = hashlib.sha256()
        self.proc: subprocess.Popen[bytes] | None = None

    def _start(self) -> None:
        cmd = ["curl", "-fsSL", "--retry", "3", "--connect-timeout", "30"]
        cmd += ["--speed-time", "120", "--speed-limit", "1024"]
        if self.offset:
            cmd += ["-r", f"{self.offset}-"]
        self.proc = subprocess.Popen([*cmd, self.url], stdout=subprocess.PIPE)

    def chunks(self):
        if self.url.startswith("file://"):
            with open(self.url[7:], "rb") as f:
                while self.limit is None or self.offset < self.limit:
                    b = f.read(READ_CHUNK)
                    if not b:
                        return
                    if self.limit is not None:
                        b = b[: self.limit - self.offset]
                    self.offset += len(b)
                    self.sha256.update(b)
                    yield b
            return
        self._start()
        while True:
            assert self.proc is not None and self.proc.stdout is not None
            b = self.proc.stdout.read(READ_CHUNK)
            if b:
                if self.limit is not None:
                    b = b[: self.limit - self.offset]
                self.offset += len(b)
                self.sha256.update(b)
                yield b
                if self.limit is not None and self.offset >= self.limit:
                    self.close()
                    return
                continue
            rc = self.proc.wait()
            if rc == 0:
                return
            if self.resumes >= self.retries:
                raise OSError(f"curl exit {rc} at byte {self.offset} after {self.resumes} resumptions")
            self.resumes += 1
            print(f"  connection dropped (curl exit {rc}) at byte {self.offset}; resuming", file=sys.stderr)
            time.sleep(min(30, 2**self.resumes))
            self._start()

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def inflate(chunks, counter: list[int]):
    """Multi-member gzip inflate; `counter[0]` accumulates the decompressed bytes."""
    d = zlib.decompressobj(31)
    for c in chunks:
        while c:
            out = d.decompress(c)
            if out:
                counter[0] += len(out)
                yield out
            if d.eof:
                c = d.unused_data
                d = zlib.decompressobj(31)
            else:
                c = b""
    tail = d.flush()
    if tail:
        counter[0] += len(tail)
        yield tail


# ---------------------------------------------------------------------------------------------- capture walk


class Capture:
    """Incremental pcapng / pcap reader: `feed(bytes)` then iterate `packets()` -> (ts_ns, linktype, frame)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0
        self.kind: str | None = None
        self.endian = "<"
        self.ifaces: list[tuple[int, int]] = []  # (linktype, ns per tick numerator/denominator encoded as divisor)
        self.pcap_linktype = 1
        self.pcap_ns = 1000
        self.blocks = Counter()

    def feed(self, b: bytes) -> None:
        if self.pos > (1 << 22):
            del self.buf[: self.pos]
            self.pos = 0
        self.buf += b

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
            raise ValueError(f"not a pcap / pcapng stream (magic {magic.hex()})")
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
                raise ValueError(f"pcapng block length {blen} at block {sum(self.blocks.values())}")
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
        et, i = struct.unpack_from(">H", frame, 12)[0], 14
        while et in (0x8100, 0x88A8) and len(frame) >= i + 4:
            et, i = struct.unpack_from(">H", frame, i + 2)[0], i + 4
        if et != 0x0800:
            return "not_ipv4"
    elif linktype in (101, 228):
        i = 0
    elif linktype == 113:
        if len(frame) < 16 or struct.unpack_from(">H", frame, 14)[0] != 0x0800:
            return "not_ipv4"
        i = 16
    else:
        return "linktype"
    if len(frame) < i + 20 or frame[i] >> 4 != 4:
        return "not_ipv4"
    ihl = (frame[i] & 0x0F) * 4
    total = struct.unpack_from(">H", frame, i + 2)[0]
    flags_frag = struct.unpack_from(">H", frame, i + 6)[0]
    if frame[i + 9] != 17:
        return "not_udp"
    if flags_frag & 0x3FFF:
        return "ip_fragment"
    u = i + ihl
    if len(frame) < u + 8:
        return "short_frame"
    ulen = struct.unpack_from(">H", frame, u + 4)[0]
    end = min(len(frame), i + total if total else len(frame), u + ulen if ulen >= 8 else len(frame))
    return frame[i + 12 : i + 20], frame[u : u + 4], frame[u + 8 : end]


# ---------------------------------------------------------------------------------------------- subset writer


def ip_checksum(h: bytes) -> int:
    s = sum(struct.unpack(">10H", h))
    s = (s & 0xFFFF) + (s >> 16)
    s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


class PcapWriter:
    """Classic pcap, little endian, nanosecond timestamps, Ethernet frames carrying IPv4/UDP."""

    def __init__(self, f: BinaryIO) -> None:
        self.f = f
        f.write(struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1))

    def write(self, ts_ns: int, addrs: bytes, ports: bytes, payload: bytes) -> None:
        udp = ports + struct.pack(">HH", 8 + len(payload), 0) + payload
        ip = bytearray(struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0) + addrs)
        struct.pack_into(">H", ip, 10, ip_checksum(bytes(ip)))
        dst = addrs[4:8]
        mac = b"\x01\x00\x5e" + bytes([dst[1] & 0x7F, dst[2], dst[3]])  # IPv4 multicast MAC of the group
        frame = mac + b"\x00\x00\x00\x00\x00\x00" + b"\x08\x00" + bytes(ip) + udp
        self.f.write(struct.pack("<IIII", ts_ns // 1_000_000_000, ts_ns % 1_000_000_000, len(frame), len(frame)))
        self.f.write(frame)


class Filter:
    """IEX-TP segment filter with the renumbering rules of the module docstring."""

    def __init__(self, symbols: list[str], writer: PcapWriter | None) -> None:
        self.symbols = {s.upper().ljust(8).encode("ascii") for s in symbols}
        self.patterns = sorted(self.symbols)
        self.w = writer
        self.st: dict[str, Any] = {
            "packets": 0,
            "skipped_packets": Counter(),
            "segments": 0,
            "heartbeats": 0,
            "messages": 0,
            "messages_parsed": 0,
            "other_protocol_segments": Counter(),
            "bad_segments": 0,
            "sequence_gaps": 0,
            "sequence_gap_messages": 0,
            "duplicate_messages": 0,
            "sessions": [],
            "kept_by_type": Counter(),
            "kept_by_symbol": {},
            "unknown_types_seen": Counter(),
            "system_events": {},
            "first_send_time_ns": None,
            "last_send_time_ns": None,
        }
        self.chans: dict[tuple[int, int, int], list[int]] = {}  # key -> [expected_src, next_new, new_offset]

    def segment(self, ts_ns: int, addrs: bytes, ports: bytes, p: bytes) -> None:
        st = self.st
        st["packets"] += 1
        if len(p) < 40 or p[0] != 1:
            st["bad_segments"] += 1
            return
        _v, _r, proto, chan, sess, plen, count, _off, seq, send = TP_HEADER.unpack_from(p, 0)
        if proto != DEEP_PLUS_PROTOCOL:
            st["other_protocol_segments"][hex(proto)] += 1
            return
        st["segments"] += 1
        if st["first_send_time_ns"] is None:
            st["first_send_time_ns"] = send
        st["last_send_time_ns"] = send
        key = (proto, chan, sess)
        ch = self.chans.get(key)
        if ch is None:
            ch = self.chans[key] = [seq, seq, 0]
            st["sessions"].append({"protocol": hex(proto), "channel": chan, "session": sess, "first_seq": seq})
        if count == 0:
            st["heartbeats"] += 1
            return
        st["messages"] += count
        expected = ch[0]
        if seq > expected:
            st["sequence_gaps"] += 1
            st["sequence_gap_messages"] += seq - expected
            ch[1] += seq - expected
            ch[0] = seq
        elif seq + count <= expected:
            st["duplicate_messages"] += count
            return
        skip = expected - seq if seq < expected else 0
        st["duplicate_messages"] += skip
        ch[0] = seq + count
        body = memoryview(p)[40 : 40 + plen]
        if not any(s in p for s in self.patterns) and SYSTEM_EVENT_PATTERN not in p:
            return  # nothing kept: the renumbered stream counts kept messages only
        st["messages_parsed"] += count
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
                    st["system_events"][chr(m[1])] = struct.unpack_from("<q", m, 2)[0]
                kept.append(bytes(m))
                st["kept_by_type"]["S"] += 1
            elif t in SYMBOL_TYPES:
                if len(m) >= 18 and bytes(m[10:18]) in self.symbols:
                    kept.append(bytes(m))
                    c = chr(t)
                    st["kept_by_type"][c] += 1
                    sym = bytes(m[10:18]).decode("ascii").rstrip()
                    st["kept_by_symbol"].setdefault(sym, Counter())[c] += 1
            else:
                st["unknown_types_seen"][hex(t)] += 1
        if not kept or self.w is None:
            return
        payload = b"".join(struct.pack("<H", len(m)) + m for m in kept)
        hdr = TP_HEADER.pack(1, 0, proto, chan, sess, len(payload), len(kept), ch[2], ch[1], send)
        ch[1] += len(kept)
        ch[2] += len(payload)
        self.w.write(ts_ns, addrs, ports, hdr + payload)


# ---------------------------------------------------------------------------------------------- run


def run(args: argparse.Namespace) -> int:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("--symbols: at least one symbol", file=sys.stderr)
        return EXIT_INPUT
    if args.url:
        entry = {"link": args.url, "date": args.date, "feed": args.feed, "size": None}
    else:
        entry = hist_entry(args.date, args.feed, Path(args.listing) if args.listing else None)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = args.tag or "-".join(symbols)
    base = out_dir / f"{args.date}_{args.feed}.subset.{tag}"
    pcap_path = base.with_name(base.name + ".pcap.gz")
    man_path = base.with_name(base.name + ".manifest.json")
    tmp = pcap_path.with_name(pcap_path.name + ".part")
    print(f"streaming {entry['link']} ({entry.get('size')} bytes listed) -> {pcap_path}", file=sys.stderr)
    dl = Download(entry["link"], limit=args.max_bytes)
    inflated = [0]
    cap = Capture()
    t0 = time.monotonic()
    last_check = t0
    status = "complete"
    with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
        flt = Filter(symbols, PcapWriter(gz))
        try:
            for chunk in inflate(dl.chunks(), inflated):
                cap.feed(chunk)
                for ts, lt, frame in cap.packets():
                    r = udp_payload(lt, frame)
                    if isinstance(r, str):
                        flt.st["packets"] += 1
                        flt.st["skipped_packets"][r] += 1
                        continue
                    flt.segment(ts, *r)
                now = time.monotonic()
                if now - last_check > 5:
                    last_check = now
                    if free_gib() < args.min_free_gib:
                        raise Guard(f"free space below {args.min_free_gib} GiB")
                    size_mb = raw.tell() / 1e6
                    if size_mb > args.max_subset_mb:
                        raise Guard(f"subset above {args.max_subset_mb} MB")
                    mbps = dl.offset / 1e6 / (now - t0)
                    print(
                        f"  {dl.offset / 1e9:6.2f} GB in ({mbps:5.1f} MB/s), {inflated[0] / 1e9:6.2f} GB out, "
                        f"{flt.st['messages'] / 1e6:8.1f} M msgs, subset {size_mb:6.1f} MB",
                        file=sys.stderr,
                        flush=True,
                    )
        except Guard as e:
            dl.close()
            gz.close()
            tmp.unlink(missing_ok=True)
            print(f"guard: {e}; partial subset deleted", file=sys.stderr)
            return EXIT_DISK
        except (OSError, ValueError, zlib.error) as e:
            dl.close()
            gz.close()
            tmp.unlink(missing_ok=True)
            print(f"input error: {e}", file=sys.stderr)
            return EXIT_INPUT
        if args.max_bytes is not None and dl.offset >= args.max_bytes:
            status = f"prefix ({args.max_bytes} compressed bytes)"
    tmp.replace(pcap_path)
    wall = time.monotonic() - t0
    st = flt.st
    h = hashlib.sha256()
    with open(pcap_path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    manifest = {
        "schema": "queuetruth.iex_subset",
        "created": now_iso(),
        "status": status,
        "attribution": ATTRIBUTION,
        "terms_url": TERMS_URL,
        "source": {
            "url": entry["link"],
            "date": entry.get("date"),
            "feed": entry.get("feed"),
            "protocol": entry.get("protocol"),
            "version": entry.get("version"),
            "listed_bytes": int(entry["size"]) if entry.get("size") else None,
            "bytes_streamed": dl.offset,
            "resumptions": dl.resumes,
            "sha256_streamed": dl.sha256.hexdigest(),
            "decompressed_bytes": inflated[0],
            "pcapng_blocks": {hex(k): v for k, v in sorted(cap.blocks.items())},
            "capture_format": cap.kind,
        },
        "symbols": symbols,
        "renumbering": "kept messages renumbered contiguously per (protocol, channel, session); source gaps kept "
        "as jumps of the same size; duplicates dropped",
        "stream": {
            k: (dict(v) if isinstance(v, Counter) else v) for k, v in st.items() if k not in ("kept_by_symbol",)
        },
        "kept_by_symbol": {k: dict(v) for k, v in sorted(st["kept_by_symbol"].items())},
        "kept_messages": sum(st["kept_by_type"].values()),
        "subset": {"path": str(pcap_path), "bytes": pcap_path.stat().st_size, "sha256": h.hexdigest()},
        "timing": {
            "wall_s": round(wall, 1),
            "download_mb_per_s": round(dl.offset / 1e6 / wall, 2) if wall else None,
            "packets_per_s": round(st["packets"] / wall) if wall else None,
            "messages_per_s": round(st["messages"] / wall) if wall else None,
        },
    }
    man_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    print(json.dumps({k: manifest[k] for k in ("status", "kept_messages", "subset", "timing")}, indent=2))
    if st["sequence_gaps"]:
        print(f"note: {st['sequence_gaps']} source sequence gap(s) carried into the subset", file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------------------------------------- self-test


def _seg(seq: int, msgs: list[bytes], send: int = 1_000, chan: int = 1, sess: int = 7) -> bytes:
    payload = b"".join(struct.pack("<H", len(m)) + m for m in msgs)
    return TP_HEADER.pack(1, 0, DEEP_PLUS_PROTOCOL, chan, sess, len(payload), len(msgs), 0, seq, send) + payload


def _msg(t: str, sym: str, extra: bytes = b"") -> bytes:
    return t.encode() + b"\x00" + struct.pack("<q", 5) + sym.ljust(8).encode() + extra


def self_test() -> int:
    sysev = b"S" + b"O" + struct.pack("<q", 1)
    segs = [
        _seg(1, [sysev, _msg("D", "AAA", b"\x00" * 13), _msg("D", "BBB", b"\x00" * 13)]),
        _seg(4, [_msg("a", "BBB", b"\x00" * 20), _msg("a", "AAA", b"\x00" * 20)]),
        _seg(4, [_msg("a", "BBB", b"\x00" * 20), _msg("a", "AAA", b"\x00" * 20)]),  # duplicate
        _seg(9, [_msg("R", "AAA", b"\x00" * 8), b"\xfe" + b"\x00" * 17]),  # 3 missing (6..8), one unknown type
        _seg(11, []),  # heartbeat
    ]
    # pcapng with one Ethernet interface (microsecond resolution) carrying the segments
    ng = io.BytesIO()
    shb_body = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
    ng.write(struct.pack("<II", 0x0A0D0D0A, 12 + len(shb_body)) + shb_body + struct.pack("<I", 12 + len(shb_body)))
    idb_body = struct.pack("<HHI", 1, 0, 65535) + struct.pack("<HHB3x", 9, 1, 6) + struct.pack("<HH", 0, 0)
    ng.write(struct.pack("<II", 1, 12 + len(idb_body)) + idb_body + struct.pack("<I", 12 + len(idb_body)))
    for k, s in enumerate(segs):
        udp = struct.pack(">HHHH", 10378, 10378, 8 + len(s), 0) + s
        ip = bytearray(struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0))
        ip += bytes([23, 226, 155, 133, 233, 215, 21, 8])
        frame = b"\x01\x00\x5e\x57\x15\x08" + b"\x00" * 6 + b"\x08\x00" + bytes(ip) + udp
        pad = (-len(frame)) % 4
        body = struct.pack("<IIIII", 0, 0, 1_000_000 + k, len(frame), len(frame)) + frame + b"\x00" * pad
        ng.write(struct.pack("<II", 6, 12 + len(body)) + body + struct.pack("<I", 12 + len(body)))
    out = io.BytesIO()
    flt = Filter(["AAA"], PcapWriter(out))
    cap = Capture()
    data = ng.getvalue()
    for i in range(0, len(data), 7):  # feed in small pieces to exercise the incremental walk
        cap.feed(data[i : i + 7])
        for ts, lt, frame in cap.packets():
            r = udp_payload(lt, frame)
            assert not isinstance(r, str), r
            flt.segment(ts, *r)
    st = flt.st
    checks = {
        "segments": st["segments"] == 5,
        "heartbeats": st["heartbeats"] == 1,
        "duplicates": st["duplicate_messages"] == 2,
        "gap": (st["sequence_gaps"], st["sequence_gap_messages"]) == (1, 3),
        "kept": dict(st["kept_by_type"]) == {"S": 1, "D": 1, "a": 1, "R": 1},
        "unknown": dict(st["unknown_types_seen"]) == {"0xfe": 1},
    }
    # read the subset back: renumbered 1,2 | 3 | jump of 3 -> 7
    back = Capture()
    back.feed(out.getvalue())
    seqs = []
    for _ts, lt, frame in back.packets():
        r = udp_payload(lt, frame)
        assert not isinstance(r, str), r
        h = TP_HEADER.unpack_from(r[2], 0)
        seqs.append((h[8], h[6]))
    checks["renumbered"] = seqs == [(1, 2), (3, 1), (7, 1)]
    stamps = [t for t, _, _ in _packets(out.getvalue())]
    checks["timestamps_ns"] = stamps == [1_000_000_000, 1_000_001_000, 1_000_003_000]
    bad = [k for k, ok in checks.items() if not ok]
    print(json.dumps({"self_test": "fail" if bad else "ok", "failed": bad, "seqs": seqs}))
    return EXIT_VERIFY if bad else EXIT_OK


def _packets(b: bytes):
    c = Capture()
    c.feed(b)
    yield from c.packets()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default="20261009", help="HIST date YYYYMMDD (default 20261009)")
    ap.add_argument("--feed", default="DPLS", help="HIST feed code (DPLS = DEEP+)")
    ap.add_argument("--symbols", default="SPY,QQQ,AAPL,NVDA,TSLA")
    ap.add_argument("--tag", default=None, help="output name instead of the joined symbols")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--url", default=None, help="explicit source URL (https:// or file://) instead of the listing")
    ap.add_argument("--listing", default=None, help="a saved copy of the HIST listing JSON")
    ap.add_argument("--max-bytes", type=int, default=None, help="stop after this many compressed bytes (a prefix)")
    ap.add_argument("--min-free-gib", type=float, default=2.5)
    ap.add_argument("--max-subset-mb", type=float, default=300.0)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if free_gib() < args.min_free_gib:
        print(f"free space {free_gib():.1f} GiB is below --min-free-gib {args.min_free_gib}", file=sys.stderr)
        return EXIT_DISK
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
