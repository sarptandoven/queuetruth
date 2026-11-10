#!/usr/bin/env python3
"""Stream one IEX DEEP+ day once and route every message into K symbol buckets (W1 scale runs on IEX).

    uv run --frozen --no-sync python scripts/scale/iex_split.py --input 20261009_IEXTP1_DPLS1.0.pcap.gz \
        --buckets 32 --out .realdata/scale/split
    uv run --frozen --no-sync python scripts/scale/iex_split.py --url URL --date 2026-10-09 --buckets 8 \
        --symbols AAPL,MSFT,SPY,TSLA --out DIR

The IEX counterpart of `itch_split.py`: the capture (pcapng or classic pcap, gzip or raw, from a file or streamed
from a URL; never stored) is walked once with `queuetruth.demo.iex`'s capture reader, and every DEEP+ message is
routed instead of kept or dropped:

- a symbol's bucket is `crc32(symbol padded to 8 bytes) mod K`, the rule of `itch_split.py`, so a symbol is in the same
  bucket on every day and in both pipelines;
- every message of a known DEEP+ type that carries a symbol (`D H I O P E a M R L T B C`) goes to its symbol's bucket;
  System Events (`S`) go to every bucket; unknown types are counted and dropped;
- with `--symbols`, only those symbols are routed (the rest dropped), for local proofs on a few names; a segment
  holding none of them and no System Event is then skipped after one regex search (as `IexCutFilter` does), so a
  few names of a full day split in minutes instead of parsing every message.

Each bucket is renumbered exactly as `queuetruth.demo.iex.IexCutFilter` renumbers a symbol subset: kept messages are
numbered contiguously per (channel, session), every gap of the source is carried over as a jump of the same size,
and messages already seen (duplicate or overlapping segments) are dropped. So a bucket file is byte-identical to the
`IexCutFilter` subset of its symbols with no cut, and `ingest --format iex_deep_plus` of a bucket reports exactly the
source's sequence gaps (tested in `python/tests/scale/test_iex_split.py`, with both engines).

Two engines write the same files and the same manifest (only `timings` differ);
`python/tests/scale/test_iex_split_engines.py` runs both on generated captures and compares every byte:

- `--engine fast` (the default) reads, routes and renumbers in Rust (`queuetruth._core.IexSplit`, from
  `crates/ingest/src/iex/split.rs`; `make develop` builds it) with the GIL released. A reader thread downloads,
  hashes and inflates the input, and `--threads` writer threads hash and gzip each bucket's records with Python's
  `gzip`, so the bucket bytes cannot drift from the Python engine's. On real 2026-10-09 bucket files it ran 10 to 14
  times as fast as the Python engine on a 4-core machine (1.0M to 1.7M messages/s against 79k to 167k), and the
  whole 2026-10-09 day (331M messages, streamed from IEX) split in 5.6 minutes, every bucket file byte-identical to
  the Python engine's split of it on a runner (61 minutes there).
- `--engine python` is the reference: `IexSplitter` below, in pure Python (about 81k messages/s on a 4-vCPU GitHub
  runner, where an 898M-message day took 185 minutes to split).

On a few malformed inputs the Python engine stops with an uncaught Python exception (a routed symbol field with a
byte above 0x7f, a capture time after 2106, a pcapng interface option cut by its block end, an Enhanced Packet Block
shorter than its header at the end of the input, a renumbered segment too long for one datagram, a renumbered
sequence number outside int64). The fast engine stops there too, as an input error (exit 2).

Outputs in `<out>/<MMDDYYYY>/` (the date format of the ITCH pipeline, which `plan.py` and `atlas.py` share):
`<MMDDYYYY>.iex.bucket-<kk>-of-<KK>.pcap.gz` (classic pcap, gzip `--level`, mtime 0, one member) and
`split_manifest.json`, read by `plan.py` like an `itch_split.py` manifest: `format` `iex_deep_plus` and `ingest`
(the unit ingest spec: format, venue `iex`), the input and stream hashes, per bucket the file sha256, the sha256 of
its pcap content, messages and per symbol `[instrument id, messages, executions]` (the id `ingest` assigns to the
symbol in that bucket, its order of first appearance there; executions count `L` and `T` messages, the planner's
cost and quota input); `routing.test_issues` (Security Directory test securities, flag 0x80, which `plan.py`
excludes); `session_events` (`R` opens and `M` closes the regular session, which `plan.py` reads for half days);
`trading_state`, per symbol the spans in which its Trading Status (`H`) was not T, in ns since midnight New York;
and `checks`. Exit codes: 0 ok, 1 a check failed, 2 input error, 4 disk guard.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import queue
import re
import shutil
import struct
import sys
import threading
import time
import zlib
from collections import Counter, deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "python"))

from queuetruth.demo.fetch import Stream  # noqa: E402
from queuetruth.demo.iex import (  # noqa: E402
    DEEP_PLUS_PROTOCOL,
    SYMBOL_TYPES,
    SYSTEM_EVENT,
    SYSTEM_EVENT_PATTERN,
    TP_HEADER,
    Capture,
    CaptureError,
    PcapWriter,
    udp_payload,
)

EXIT_OK, EXIT_VERIFY, EXIT_INPUT, EXIT_DISK = 0, 1, 2, 4
#: writer threads of the fast engine (gzip is most of its work; routing in Rust is under a tenth)
DEFAULT_THREADS = max(2, min(8, os.cpu_count() or 4))
DROP = -1
NS = 1_000_000_000
ET = ZoneInfo("America/New_York")
EXECUTION_TYPES = frozenset(b"LT")
TRADING_STATUS = ord("H")
SECURITY_DIRECTORY = ord("D")
TEST_SECURITY_FLAG = 0x80
#: The unit ingest spec of an IEX bucket (merged over `common.INGEST_DEFAULTS` by `plan.py`).
INGEST = {"format": "iex_deep_plus", "venue": "iex"}
TRADING_STATE_RULE = (
    "per routed symbol (from its DEEP+ Trading Status `H` messages of the whole day), every `H` whose status "
    "is not T (H halted, O order acceptance period, P paused) opens a span [its timestamp, the symbol's next `H` "
    "(null: the end of the day)] with that status and reason; ns since midnight New York; a symbol whose every `H` "
    "says T is not listed"
)


def bucket_of(symbol: str, k: int) -> int:
    """Bucket of a symbol: `crc32(symbol, ASCII, right-padded with spaces to 8 bytes) mod k` (as `itch_split.py`)."""
    return zlib.crc32(symbol.encode("ascii").ljust(8)) % k


def bucket_name(date: str, i: int, k: int) -> str:
    return f"{date}.iex.bucket-{i:02d}-of-{k:02d}.pcap.gz"


def midnight_ns(date: dt.date) -> int:
    """New York midnight of `date` in UTC ns."""
    return int(dt.datetime(date.year, date.month, date.day, tzinfo=ET).timestamp()) * NS


def non_t_spans(actions: dict[str, list[tuple[int, str, str]]]) -> dict[str, list[list[Any]]]:
    """`{symbol: [[start_ns, end_ns or None, status, reason], ...]}` for the symbols with any `H` status other than T
    (`actions`: per symbol its `H` messages `(ns since midnight, status, reason)` in feed order; the rule of
    `itch_split.non_t_spans`)."""
    out: dict[str, list[list[Any]]] = {}
    for sym, acts in sorted(actions.items()):
        spans = [
            [ts, acts[i + 1][0] if i + 1 < len(acts) else None, state, reason]
            for i, (ts, state, reason) in enumerate(acts)
            if state != "T"
        ]
        if spans:
            out[sym] = spans
    return out


class IexSplitter:
    """Routes the DEEP+ messages of a capture into `k` bucket pcaps (module docstring): the Python engine, the
    reference of `FastIexSplitter`."""

    def __init__(self, k: int, outs: list[BinaryIO | None], midnight_utc_ns: int, symbols: set[str] | None = None):
        if k < 1 or len(outs) != k:
            raise ValueError("one output per bucket")
        self.k = k
        self.midnight = midnight_utc_ns
        self.only = {s.upper() for s in symbols} if symbols else None
        # with a symbol filter, a segment holding none of the symbols and no System Event is not parsed (one regex
        # search per segment, as `IexCutFilter`): it only advances the sequence bookkeeping
        self.rx = (
            re.compile(
                b"|".join(re.escape(x) for x in [*sorted(s.ljust(8).encode() for s in self.only), SYSTEM_EVENT_PATTERN])
            )
            if self.only
            else None
        )
        self.writers = [PcapWriter(f) for f in outs]
        self.capture = Capture()
        self.route: dict[bytes, int] = {}
        self.src: dict[tuple[int, int], int] = {}  # (channel, session) -> next expected source sequence
        self.dst: list[dict[tuple[int, int], list[int]]] = [{} for _ in range(k)]  # -> [next sequence, offset]
        self.sessions: list[dict[str, Any]] = []
        self.ids: list[dict[str, int]] = [{} for _ in range(k)]  # first appearance order per bucket
        self.msgs: Counter[str] = Counter()
        self.execs: Counter[str] = Counter()
        self.actions: dict[str, list[tuple[int, str, str]]] = {}
        self.test_issues: set[str] = set()
        self.system_events: dict[str, int] = {}
        self.bucket_msgs = [0] * k
        self.bucket_segments = [0] * k
        self.c: Counter[str] = Counter()
        self.skipped_packets: Counter[str] = Counter()
        self.other_protocol: Counter[str] = Counter()
        self.unknown_types: Counter[str] = Counter()
        self.first_send_ns: int | None = None
        self.last_send_ns: int | None = None

    def _bucket(self, sym8: bytes) -> int:
        b = self.route.get(sym8)
        if b is None:
            name = sym8.decode("ascii", "replace").rstrip()
            b = DROP if self.only is not None and name not in self.only else bucket_of(name, self.k)
            self.route[sym8] = b
        return b

    def finish(self) -> None:
        """Nothing to wait for: every packet is written as it is routed."""

    def close(self) -> None:
        """Nothing to stop."""

    def feed(self, data: bytes) -> None:
        self.capture.feed(data)
        for ts, lt, frame in self.capture.packets():
            r = udp_payload(lt, frame)
            self.c["packets"] += 1
            if isinstance(r, str):
                self.skipped_packets[r] += 1
                continue
            self.segment(ts, *r)

    def segment(self, ts_ns: int, addrs: bytes, ports: bytes, p: bytes) -> None:
        if len(p) < 40 or p[0] != 1:
            self.c["bad_segments"] += 1
            return
        _v, _r, proto, chan, sess, plen, count, _off, seq, send = TP_HEADER.unpack_from(p, 0)
        if proto != DEEP_PLUS_PROTOCOL:
            self.other_protocol[hex(proto)] += 1
            return
        self.c["segments"] += 1
        if self.first_send_ns is None:
            self.first_send_ns = send
        self.last_send_ns = send
        key = (chan, sess)
        if key not in self.src:
            self.src[key] = seq
            for d in self.dst:
                d[key] = [seq, 0]
            self.sessions.append({"channel": chan, "session": sess, "first_seq": seq})
        if count == 0:
            self.c["heartbeats"] += 1
            return
        self.c["messages"] += count
        expected = self.src[key]
        if seq > expected:
            self.c["sequence_gaps"] += 1
            self.c["sequence_gap_messages"] += seq - expected
            for d in self.dst:
                d[key][0] += seq - expected
            expected = seq
        elif seq + count <= expected:
            self.c["duplicate_messages"] += count
            return
        skip = expected - seq if seq < expected else 0
        self.c["duplicate_messages"] += skip
        self.src[key] = seq + count
        if self.rx is not None and self.rx.search(p, 40) is None:
            self.c["prefiltered"] += count - skip
            return
        kept: dict[int, list[bytes]] = {}
        body = memoryview(p)[40 : 40 + plen]
        i = n = 0
        while n < count and i + 2 <= len(body):
            ln = body[i] | (body[i + 1] << 8)
            m = bytes(body[i + 2 : i + 2 + ln])
            i += 2 + ln
            n += 1
            if n <= skip:
                continue
            self.c["parsed"] += 1
            if not m:
                self.c["malformed"] += 1
                continue
            t = m[0]
            if t == SYSTEM_EVENT:
                if len(m) >= 10:
                    self.system_events[chr(m[1])] = struct.unpack_from("<q", m, 2)[0] - self.midnight
                self.c["system"] += 1
                for b in range(self.k):
                    kept.setdefault(b, []).append(m)
            elif t in SYMBOL_TYPES:
                if len(m) < 18:
                    self.c["malformed"] += 1
                    continue
                sym8 = m[10:18]
                b = self._bucket(sym8)
                if b == DROP:
                    self.c["dropped"] += 1
                    continue
                sym = sym8.decode("ascii", "replace").rstrip()
                self.c["routed"] += 1
                self.msgs[sym] += 1
                ids = self.ids[b]
                if sym not in ids:
                    ids[sym] = len(ids) + 1
                if t in EXECUTION_TYPES:
                    self.execs[sym] += 1
                elif t == TRADING_STATUS and len(m) >= 22:
                    ts = struct.unpack_from("<q", m, 2)[0] - self.midnight
                    reason = m[18:22].decode("ascii", "replace").strip()
                    self.actions.setdefault(sym, []).append((ts, chr(m[1]), reason))
                elif t == SECURITY_DIRECTORY and m[1] & TEST_SECURITY_FLAG:
                    self.test_issues.add(sym)
                kept.setdefault(b, []).append(m)
            else:
                self.c["unknown_type"] += 1
                self.unknown_types[hex(t)] += 1
        for b in sorted(kept):
            msgs = kept[b]
            payload = b"".join(struct.pack("<H", len(m)) + m for m in msgs)
            st = self.dst[b][key]
            hdr = TP_HEADER.pack(1, 0, DEEP_PLUS_PROTOCOL, chan, sess, len(payload), len(msgs), st[1], st[0], send)
            st[0] += len(msgs)
            st[1] += len(payload)
            self.bucket_msgs[b] += len(msgs)
            self.bucket_segments[b] += 1
            self.writers[b].write(ts_ns, addrs, ports, hdr + payload)


@dataclass
class CaptureState:
    """`kind`, `pending` and `blocks` of a `Capture`, as the Rust engine reports them."""

    kind: str | None = None
    pending: int = 0
    blocks: Counter[int] = field(default_factory=Counter)


def fast_engine_available() -> bool:
    """True when `queuetruth._core` is built with `IexSplit`."""
    try:
        from queuetruth import _core
    except ImportError:
        return False
    return hasattr(_core, "IexSplit")


def _write_records(group: list[tuple[PcapWriter, bytes]]) -> None:
    for w, records in group:
        w._write(records)


class FastIexSplitter:
    """The split of `IexSplitter` by the Rust engine `queuetruth._core.IexSplit` (module docstring).

    `feed` hands each piece of the capture to the engine, which routes it with the GIL released and returns each
    bucket's new pcap records. `threads` writer threads write them through the buckets' `PcapWriter`s (content hash,
    then gzip), bucket b always on thread `b mod threads`, so every bucket gets its records in order. `finish` waits
    for the writers and copies the engine's counters and maps into the attributes `IexSplitter` keeps; `close` stops
    the writers."""

    #: pieces whose records may still be waiting for a writer (bounds the memory held)
    MAX_INFLIGHT = 4

    def __init__(
        self,
        k: int,
        outs: list[BinaryIO | None],
        midnight_utc_ns: int,
        symbols: set[str] | None = None,
        threads: int = 1,
    ):
        if k < 1 or len(outs) != k:
            raise ValueError("one output per bucket")
        from queuetruth import _core

        self.k = k
        self.midnight = midnight_utc_ns
        self.only = {s.upper() for s in symbols} if symbols else None
        # the prefilter patterns of `IexSplitter.rx`, built the same way (the engine searches them literally)
        patterns = [*sorted(s.ljust(8).encode() for s in self.only), SYSTEM_EVENT_PATTERN] if self.only else None
        self._core_error = _core.QtCoreError
        self.core = _core.IexSplit(k, midnight_utc_ns, sorted(self.only) if self.only else None, patterns)
        self.writers = [PcapWriter(f) for f in outs]
        self.capture = CaptureState()
        self.sessions: list[dict[str, Any]] = []
        self.ids: list[dict[str, int]] = [{} for _ in range(k)]
        self.msgs: Counter[str] = Counter()
        self.execs: Counter[str] = Counter()
        self.actions: dict[str, list[tuple[int, str, str]]] = {}
        self.test_issues: set[str] = set()
        self.system_events: dict[str, int] = {}
        self.bucket_msgs = [0] * k
        self.bucket_segments = [0] * k
        self.c: Counter[str] = Counter()
        self.skipped_packets: Counter[str] = Counter()
        self.other_protocol: Counter[str] = Counter()
        self.unknown_types: Counter[str] = Counter()
        self.first_send_ns: int | None = None
        self.last_send_ns: int | None = None
        self._pools = [
            ThreadPoolExecutor(1, thread_name_prefix=f"iex-split-write-{i}") for i in range(max(1, min(threads, k)))
        ]
        self._inflight: deque[list[Future[None]]] = deque()

    def feed(self, data: bytes) -> None:
        try:
            parts = self.core.feed(data)
        except self._core_error as e:
            raise CaptureError(e.message) from None
        if parts:
            n = len(self._pools)
            groups: list[list[tuple[PcapWriter, bytes]]] = [[] for _ in range(n)]
            for b, records in parts:
                groups[b % n].append((self.writers[b], records))
            self._inflight.append([self._pools[i].submit(_write_records, g) for i, g in enumerate(groups) if g])
        while len(self._inflight) > self.MAX_INFLIGHT:
            for f in self._inflight.popleft():
                f.result()

    def finish(self) -> None:
        """Waits for the writers (raising their error, if any) and takes the counters and maps from the engine."""
        while self._inflight:
            for f in self._inflight.popleft():
                f.result()
        st = self.core.stats()
        self.c = Counter({k: v for k, v in st["counters"].items() if v})
        self.skipped_packets = Counter(dict(st["skipped_packets"]))
        self.other_protocol = Counter({hex(p): n for p, n in st["other_protocol"]})
        self.unknown_types = Counter({hex(t): n for t, n in st["unknown_types"]})
        self.sessions = [{"channel": ch, "session": s, "first_seq": q} for ch, s, q in st["sessions"]]
        self.first_send_ns = st["first_send_ns"]
        self.last_send_ns = st["last_send_ns"]
        self.system_events = dict(st["system_events"])
        for name, b, iid, n, execs, test in st["symbols"]:
            self.ids[b][name] = iid
            self.msgs[name] = n
            if execs:
                self.execs[name] = execs
            if test:
                self.test_issues.add(name)
        self.actions = {name: list(acts) for name, acts in st["trading_status"]}
        self.bucket_msgs = list(st["bucket_messages"])
        self.bucket_segments = list(st["bucket_segments"])
        self.capture = CaptureState(st["capture_format"], st["pending"], Counter(dict(st["blocks"])))

    def close(self) -> None:
        """Stops the writers: records not written yet are dropped (after an error), the running writes end first."""
        for p in self._pools:
            p.shutdown(wait=True, cancel_futures=True)


class _Raised:
    def __init__(self, error: BaseException) -> None:
        self.error = error


def _read_ahead(it: Iterator[bytes], depth: int = 4) -> Iterator[bytes]:
    """The items of `it`, produced by a thread up to `depth` items ahead, so the input is read, hashed and inflated
    while the previous pieces are split and written; an exception of `it` is raised here in its place. Closing this
    generator stops the thread and closes `it`."""
    q: queue.Queue[Any] = queue.Queue(depth)
    stop = threading.Event()
    done = object()

    def put(x: Any) -> bool:
        while not stop.is_set():
            try:
                q.put(x, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def run() -> None:
        try:
            for x in it:
                if not put(x):
                    return
        except BaseException as e:  # handed to the consumer, which raises it
            put(_Raised(e))
        else:
            put(done)
        finally:
            close = getattr(it, "close", None)
            if close is not None:
                close()

    t = threading.Thread(target=run, name="iex-split-read", daemon=True)
    t.start()
    try:
        while True:
            x = q.get()
            if x is done:
                return
            if isinstance(x, _Raised):
                raise x.error
            yield x
    finally:
        stop.set()
        t.join()


def _chunks(a: argparse.Namespace, stats: dict[str, Any]):
    """The capture's bytes, inflated member by member when gzip, with both streams hashed into `stats`."""
    sha_c, sha_u, md5 = hashlib.sha256(), hashlib.sha256(), hashlib.md5()
    n_c = n_u = members = 0
    d: Any = None
    pending = False  # a gzip member was started and has not ended
    # A URL is read with the demo's resumable stream: a dropped connection resumes with a byte range checked against
    # the first response's validator, so a long day file survives resets (it is never restarted or spliced).
    remote = Stream(a.url, retries=10, backoff_s=2.0) if a.url else None
    src = None if remote is not None else open(a.input, "rb")
    raw = remote.chunks() if remote is not None else iter(lambda: src.read(1 << 20), b"")
    try:
        for buf in raw:
            sha_c.update(buf)
            md5.update(buf)
            if n_c == 0 and buf[:2] == b"\x1f\x8b":
                d = zlib.decompressobj(47)
            n_c += len(buf)
            if d is None:
                sha_u.update(buf)
                n_u += len(buf)
                yield buf
                continue
            while buf:
                out = d.decompress(buf)
                pending = True
                if out:
                    sha_u.update(out)
                    n_u += len(out)
                    yield out
                if d.eof:
                    members += 1
                    pending = False
                    buf = d.unused_data
                    d = zlib.decompressobj(47)
                else:
                    buf = b""
    finally:
        if remote is not None:
            remote.close()
            stats["http_resumptions"] = remote.resumptions
        else:
            src.close()
    stats.update(
        compressed_bytes=n_c,
        compressed_md5=md5.hexdigest(),
        compressed_sha256=sha_c.hexdigest(),
        decompressed_bytes=n_u,
        decompressed_sha256=sha_u.hexdigest(),
        gzip_members=members,
        truncated_gzip=pending,
    )


def _date_of(a: argparse.Namespace) -> dt.date:
    if a.date:
        return dt.date.fromisoformat(a.date)
    name = Path(a.input or a.url or "").name
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", name)
    if not m:
        raise SystemExit(f"--date YYYY-MM-DD is needed: no YYYYMMDD in {name!r}")
    return dt.date(int(m[1]), int(m[2]), int(m[3]))


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run_split(a: argparse.Namespace) -> int:
    """Splits one capture; returns the exit code."""
    if bool(a.input) == bool(a.url):
        print("give exactly one of --input FILE and --url URL", file=sys.stderr)
        return EXIT_INPUT
    if a.engine == "fast" and not fast_engine_available():
        print(
            "--engine fast needs queuetruth._core with IexSplit (make develop); or use --engine python", file=sys.stderr
        )
        return EXIT_INPUT
    day = _date_of(a)
    date = day.strftime("%m%d%Y")
    out = Path(a.out) / date
    out.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(out).free / 2**30 < a.min_free_gib:
        print(f"less than {a.min_free_gib} GiB free under {out}", file=sys.stderr)
        return EXIT_DISK
    k = a.buckets
    symbols = {s.strip().upper() for s in a.symbols.split(",") if s.strip()} if a.symbols else None
    started = time.perf_counter()
    files = [out / bucket_name(date, i, k) for i in range(k)]
    tmps = [f.with_suffix(f.suffix + ".tmp") for f in files]
    raws = [open(t, "wb") for t in tmps]
    gzs = [gzip.GzipFile(filename="", mode="wb", fileobj=r, compresslevel=a.level, mtime=0) for r in raws]
    stream: dict[str, Any] = {}
    sp: IexSplitter | FastIexSplitter
    if a.engine == "fast":
        sp = FastIexSplitter(k, list(gzs), midnight_ns(day), symbols, threads=a.threads)
        chunks = _read_ahead(_chunks(a, stream))
    else:
        sp = IexSplitter(k, list(gzs), midnight_ns(day), symbols)
        chunks = _chunks(a, stream)
    try:
        for chunk in chunks:
            sp.feed(chunk)
        sp.finish()
    except (OSError, CaptureError, zlib.error) as e:
        print(f"input error: {e}", file=sys.stderr)
        failed = True
    else:
        failed = False
    finally:
        chunks.close()
        sp.close()
        for g, r in zip(gzs, raws, strict=True):
            g.close()
            r.close()
    if failed:
        for t in tmps:
            t.unlink(missing_ok=True)
        return EXIT_INPUT
    for t, f in zip(tmps, files, strict=True):
        t.replace(f)
    c = sp.c
    checks: dict[str, dict[str, Any]] = {}

    def chk(name: str, got: Any, want: Any) -> None:
        checks[name] = {"ok": got == want, "got": got, "want": want}

    chk("capture read to its end", sp.capture.pending, 0)
    chk("gzip stream complete", bool(stream.get("truncated_gzip")), False)
    chk("no malformed IEX-TP segment", c["bad_segments"], 0)
    chk(
        "every new message classified",
        c["parsed"],
        c["system"] + c["routed"] + c["dropped"] + c["unknown_type"] + c["malformed"],
    )
    chk(
        "every message parsed, prefiltered or a duplicate",
        c["messages"] - c["duplicate_messages"],
        c["parsed"] + c["prefiltered"],
    )
    chk("bucket messages == routed + K x system events", sum(sp.bucket_msgs), c["routed"] + k * c["system"])
    first_day = (
        dt.datetime.fromtimestamp(sp.first_send_ns / NS, ET).date().isoformat()
        if sp.first_send_ns is not None
        else None
    )
    chk("first segment on the session date", first_day, day.isoformat())
    if symbols is not None:
        chk("every requested symbol present", sorted(symbols - set(sp.msgs)), [])
    buckets = []
    for i, f in enumerate(files):
        buckets.append(
            {
                "bucket": i,
                "file": f.name,
                "file_bytes": f.stat().st_size,
                "file_sha256": _sha256_file(f),
                "content_sha256": sp.writers[i].content_sha256.hexdigest(),
                "messages": sp.bucket_msgs[i],
                "segments": sp.bucket_segments[i],
                "symbols": {s: [iid, sp.msgs[s], sp.execs[s]] for s, iid in sorted(sp.ids[i].items())},
            }
        )
    manifest = {
        "schema": "qt.scale.iex_split",
        "schema_version": 1,
        "generator": "scripts/scale/iex_split.py",
        "format": "iex_deep_plus",
        "ingest": INGEST,
        "date": date,
        "session_date": day.isoformat(),
        "source": {"url": a.url or Path(a.input).resolve().as_uri(), "input": Path(a.input).name if a.input else None},
        "stream": {
            **stream,
            "capture_format": sp.capture.kind,
            "packets": c["packets"],
            "segments": c["segments"],
            "heartbeats": c["heartbeats"],
            "messages": c["messages"],
            "duplicate_messages": c["duplicate_messages"],
            "sequence_gaps": c["sequence_gaps"],
            "sequence_gap_messages": c["sequence_gap_messages"],
            "skipped_packets": dict(sp.skipped_packets),
            "other_protocol_segments": dict(sp.other_protocol),
            "unknown_types": dict(sp.unknown_types),
            "sessions": sp.sessions,
            "first_send_ns": sp.first_send_ns,
            "last_send_ns": sp.last_send_ns,
            "system_events_ns_since_midnight": dict(sorted(sp.system_events.items())),
        },
        "session_events": {"open": "R", "close": "M"},
        "routing": {
            "k": k,
            "rule": "bucket = crc32(symbol ljust 8, ASCII) mod k; System Events to every bucket; per bucket, kept "
            "messages renumbered contiguously per (channel, session) with every source gap carried as a jump",
            "symbols": sorted(symbols) if symbols else None,
            "routed_messages": c["routed"],
            "dropped_messages": c["dropped"],
            "prefiltered_messages": c["prefiltered"],
            "system_messages": c["system"],
            "test_issues": sorted(sp.test_issues),
            "gzip": f"python gzip, compresslevel {a.level}, mtime 0, empty name, one member",
        },
        "trading_state": {
            "rule": TRADING_STATE_RULE,
            "h_messages": sum(len(v) for v in sp.actions.values()),
            "symbols_with_h": len(sp.actions),
            "non_t_spans": non_t_spans(sp.actions),
        },
        "buckets": buckets,
        "timings": {"wall_s": round(time.perf_counter() - started, 2)},
        "checks": checks,
        "ok": all(v["ok"] for v in checks.values()),
    }
    (out / "split_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(json.dumps({"ok": manifest["ok"], "buckets": k, "messages": c["messages"], "out": str(out)}, indent=1))
    return EXIT_OK if manifest["ok"] else EXIT_VERIFY


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="IEX DEEP+ capture (pcap or pcapng, gzip or raw)")
    ap.add_argument("--url", help="stream the capture from this URL instead (never stored)")
    ap.add_argument("--date", help="session date YYYY-MM-DD (default: the YYYYMMDD in the file name)")
    ap.add_argument("--buckets", type=int, default=32)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--symbols", help="route only these symbols (comma separated)")
    ap.add_argument("--level", type=int, default=1, help="gzip level of the bucket files (default 1)")
    ap.add_argument("--min-free-gib", type=float, default=2.0)
    ap.add_argument(
        "--engine",
        choices=["fast", "python"],
        default="fast",
        help="fast: Rust (queuetruth._core, the default); python: the pure-Python reference (same output)",
    )
    ap.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"writer threads of the fast engine, which gzip the buckets (default {DEFAULT_THREADS})",
    )
    return run_split(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
