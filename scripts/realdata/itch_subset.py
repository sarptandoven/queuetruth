#!/usr/bin/env python3
"""Stream one public Nasdaq TotalView-ITCH 5.0 day file and keep only the messages of a few symbols.

    python3 -I scripts/realdata/itch_subset.py --date 12302019 --symbols AAPL,MSFT,SPY,TSLA
    python3 -I scripts/realdata/itch_subset.py --self-test

The raw file is never stored. One pass:

    curl -fsSL <url>  ->  md5 + sha256 of the compressed bytes  ->  zlib inflate (gzip CRC32 / ISIZE checked per
    member)  ->  sha256 of the decompressed stream  ->  ITCH framing walk ([u16 BE length][message])  ->  subset

A message is kept when its stock locate (the 2 bytes after the type byte) is 0 (system / market-wide: `S`, `V`, `W`)
or belongs to a requested symbol. Locates are learned from `R` (Stock Directory) messages; the `R` of a kept symbol
is kept, every other `R` is dropped. queuetruth's ITCH mapper (crates/ingest/src/itch/mapper.rs) selects by the
same header locate after the same `R`, needs no other message type for session handling (system events only feed
its statistics), and gives every record-producing message a sequence number only when its locate is selected, so
the subset maps to exactly the records of the full file. All message types of a kept locate are kept (also the
ones the mapper ignores: H, Y, L, I, N, J, K, h, B, O).

Outputs (gitignored), in `--out` (default `.realdata/itch`):
- `<date>.subset.<SYM-SYM...>.itch50.gz`: the subset in the same framing, gzip (mtime 0, level 6, one member).
- `<date>.subset.<SYM-SYM...>.manifest.json`: source URL and HTTP headers, bytes streamed, md5 / sha256 of the
  compressed stream as it passed through, sha256 of the decompressed stream, messages by type (all and kept),
  per-symbol locates and counts, P / Q prints of the kept symbols, the sha256 of the subset file and of its framed
  content, timings (download MB/s, filter msgs/s), and every check against the reference values of the public file.
- `<date>.directory.json`: every symbol of the day's Stock Directory with its locate and its message count in the
  full file (aggregate counts only; used to pick liquidity tiers).

Reference values: by default the values pinned in `PUBLIC_DAYS` below (size, md5 and sha256 of the compressed file,
sha256 of the decompressed stream, message counts by type, system-event times, directory entries), recorded by this
script when it first streamed the two days on 2026-10-18 and identical to what the earlier prototype (et) recorded
for the same files. `--et-root DIR` (optional; also `QT_ET_ROOT`) also reads et's fetch log and converter stats.json
and checks those.
`--tag NAME` names the outputs instead of the joined symbol list (for long lists); `--allow-missing` keeps going when a
requested symbol is not in the day's directory (it is reported).

Guards: the free space on `/` is checked every few seconds; below `--min-free-gib` (default 2.5) the stream is
stopped, the partial subset deleted and the exit code is 4. Exit codes: 0 ok, 1 a verification failed (subset kept
for inspection, manifest says what), 2 network / input error, 4 disk guard.
Stdlib only; run with `python3 -I` (or `uv run --frozen --no-sync python -I`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import platform
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT_DEFAULT = REPO / ".realdata" / "itch"
ITCH_BASE_FALLBACK = "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/"
# The public Nasdaq EMI sample days as streamed by this script on 2026-10-18 (and recorded identically by
# the earlier prototype's (et) fetch log / converter for 2019-12-30; its log has no md5 for 2020-01-30).
PUBLIC_DAYS: dict[str, dict[str, Any]] = {
    "12302019": {
        "bytes": 3524013057,
        "md5": "23514ef98821448a3f8f4c5b4095f3f5",
        "sha256": "ef03df46a27e6bda4dead017f84c2e3979df7211f02c7868b51d53fceb99c689",
        "decompressed_bytes": 8251407909,
        "decompressed_sha256": "5d81c2e14a0f748b29c674b6a342796932702034b4dd341e39e9a9ec5bac610f",
        "messages": 268744780,
        "directory_entries": 8906,
        "system_events": {"C": 72300000040070, "E": 72000000019686, "M": 57600000071785, "O": 11072057543747,
                          "Q": 34200000075566, "S": 14400000198145},
        "by_type": {"A": 117145568, "C": 99917, "D": 114360997, "E": 5722824, "F": 1485888, "H": 8966, "I": 4024315,
                    "J": 34, "K": 3, "L": 215161, "P": 1218602, "Q": 17836, "R": 8906, "S": 6, "U": 21639067, "V": 1,
                    "X": 2787676, "Y": 9013},
    },
    "01302020": {
        "bytes": 5597158940,
        "md5": "baa0a7dfbf4384841a01594cd931e5c0",
        "sha256": "8f5399155242f8d84338c36fdd78a47c0b0aa3df5707829b20c5158c258364c6",
        "decompressed_bytes": 12952050754,
        "decompressed_sha256": "a324b2cc6daa0992e411d021b1f714cbc614dc5d1d3f2828b19b79d3f9fbe75b",
        "messages": 423285709,
        "directory_entries": 8916,
        "system_events": {"C": 72300000039817, "E": 72000000021649, "M": 57600000078520, "O": 10953404452051,
                          "Q": 34200000073183, "S": 14400000245942},
        "by_type": {"A": 184735355, "C": 139474, "D": 180285101, "E": 8415610, "F": 1875350, "H": 8921, "I": 4025192,
                    "J": 5, "K": 2, "L": 216802, "P": 1779727, "Q": 17835, "R": 8916, "S": 6, "U": 36777372, "V": 1,
                    "X": 4990972, "Y": 9068},
    },
}  # fmt: skip
GIB = 1 << 30
READ_CHUNK = 1 << 20
EXIT_OK, EXIT_VERIFY, EXIT_INPUT, EXIT_DISK = 0, 1, 2, 4

# ITCH 5.0 message lengths without the 2-byte prefix (same table as crates/ingest/src/itch/message.rs message_len).
ITCH50_LEN = {
    "S": 12, "R": 39, "H": 25, "Y": 20, "L": 26, "V": 35, "W": 12, "K": 28, "J": 35, "h": 21, "A": 36, "F": 40,
    "E": 31, "C": 36, "X": 23, "D": 19, "U": 35, "P": 44, "Q": 40, "B": 19, "I": 50, "N": 20, "O": 48,
}  # fmt: skip
T_R, T_P, T_Q = ord("R"), ord("P"), ord("Q")


class DiskGuard(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def free_gib(path: str = "/") -> float:
    return shutil.disk_usage(path).free / GIB


# ---------------------------------------------------------------------------------------------- reference values


def public_reference(date: str) -> dict[str, Any]:
    """The pinned values of a public day (`PUBLIC_DAYS`); only the URL for a day not pinned here."""
    name = f"{date}.NASDAQ_ITCH50.gz"
    ref: dict[str, Any] = {"name": name, "url": ITCH_BASE_FALLBACK + name, "provenance": {}}
    pin = PUBLIC_DAYS.get(date)
    if pin:
        ref.update(pin)
        ref["provenance"]["all"] = "PUBLIC_DAYS in scripts/realdata/itch_subset.py (first stream, 2026-10-18)"
    return ref


def et_reference(et_root: Path, date: str) -> dict[str, Any]:
    """What the earlier prototype (et) recorded about this day file (read-only): URL base from fetch_public_data.py,
    size and md5 from its fetch logs / summaries, message counts by type and system events from its converter
    stats.json."""
    ref: dict[str, Any] = {"provenance": {}}
    fp = et_root / "scripts" / "fetch_public_data.py"
    base = ITCH_BASE_FALLBACK
    if fp.exists():
        m = re.search(r'^ITCH_BASE = "([^"]+)"', fp.read_text(), re.M)
        if m:
            base = m.group(1)
            ref["provenance"]["url_base"] = f"{fp} (ITCH_BASE)"
    name = f"{date}.NASDAQ_ITCH50.gz"
    ref["name"], ref["url"] = name, base + name
    pat = re.compile(rf"verified data/itch/{re.escape(name)} \(([\d,]+) bytes, md5 ([0-9a-f]{{32}})\)")
    for log in sorted((et_root / "out" / "logs").glob("*.log")):
        m = pat.search(log.read_text(errors="replace"))
        if m:
            ref["bytes"], ref["md5"] = int(m.group(1).replace(",", "")), m.group(2)
            ref["provenance"]["bytes_md5"] = str(log)
            break
    for summ in sorted((et_root / "out").glob("real-day*-summary.json")):
        d = json.loads(summ.read_text())
        if d.get("itch_file") == name and "itch_bytes" in d:
            if "bytes" in ref and ref["bytes"] != d["itch_bytes"]:
                ref["bytes_conflict"] = {"log": ref["bytes"], str(summ): d["itch_bytes"]}
            ref.setdefault("bytes", d["itch_bytes"])
            ref["provenance"].setdefault("bytes_md5", str(summ) + " (itch_bytes; no md5 recorded)")
    st = et_root / "out" / f"itch-{date}" / "stats.json"
    if st.exists():
        s = json.loads(st.read_text())
        ref["messages"] = s.get("messages")
        ref["by_type"] = s.get("by_type")
        ref["system_events"] = s.get("system_events")
        ref["directory_entries"] = s.get("directory_entries")
        ref["first_ts_ns"], ref["last_ts_ns"] = s.get("first_ts_ns"), s.get("last_ts_ns")
        ref["provenance"]["messages_by_type"] = str(st)
    return ref


def http_head(url: str) -> dict[str, Any]:
    if url.startswith("file://"):
        p = Path(url[7:])
        return {"status": 200, "content_length": p.stat().st_size, "headers": {}}
    for attempt in range(6):  # EMI drops connections now and then: retry with backoff before giving up
        r = subprocess.run(["curl", "-sSIL", "--max-time", "60", url], capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(min(60, 5 * 2**attempt))
    if r.returncode != 0:
        raise OSError(f"HEAD {url}: curl exit {r.returncode}: {r.stderr.strip()}")
    blocks = [b for b in r.stdout.replace("\r", "").split("\n\n") if b.strip()]
    last = blocks[-1].splitlines()
    status = int(last[0].split()[1])
    hdr = {}
    for ln in last[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdr[k.strip().lower()] = v.strip()
    cl = hdr.get("content-length")
    return {
        "status": status,
        "content_length": int(cl) if cl else None,
        "headers": {
            k: hdr[k] for k in ("content-type", "last-modified", "etag", "accept-ranges", "server") if k in hdr
        },
    }


# ---------------------------------------------------------------------------------------------- byte source


def curl_stream(url: str, total: int | None, stats: dict[str, Any], max_restarts: int = 8) -> Iterator[bytes]:
    """`curl -fsSL <url>` piped in READ_CHUNK pieces. If curl dies before `total` bytes, the stream resumes from
    the byte reached with a range request (`-r <offset>-`, HTTP 206 required), so the decoder state carries on."""
    off, restarts = 0, 0
    while True:
        hdr_file = None
        if off == 0:
            args = ["curl", "-fsSL", url]
        else:
            fd, hdr_file = tempfile.mkstemp(prefix="itch_subset_hdr_")
            os.close(fd)
            args = ["curl", "-fsSL", "-D", hdr_file, "-r", f"{off}-", url]
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        checked = hdr_file is None
        try:
            assert p.stdout is not None
            while True:
                b = p.stdout.read(READ_CHUNK)
                if not b:
                    break
                if not checked:
                    txt = Path(hdr_file).read_text(errors="replace")
                    codes = re.findall(r"^HTTP/\S+ (\d{3})", txt, re.M)
                    if not codes or codes[-1] != "206":
                        p.kill()
                        raise OSError(f"resume at byte {off}: server answered {codes[-1] if codes else '?'}, not 206")
                    checked = True
                off += len(b)
                yield b
        finally:
            if p.poll() is None:
                p.kill()
            rc = p.wait()
            err = p.stderr.read().decode(errors="replace").strip() if p.stderr else ""
            if hdr_file:
                Path(hdr_file).unlink(missing_ok=True)
        if rc == 0 and (total is None or off == total):
            return
        if total is not None and off > total:
            raise OSError(f"received {off} bytes, more than Content-Length {total}")
        restarts += 1
        stats.setdefault("restarts", []).append({"at_byte": off, "curl_exit": rc, "stderr": err[-300:]})
        if restarts > max_restarts:
            raise OSError(f"curl failed {restarts} times; last exit {rc}: {err[-300:]}")
        time.sleep(min(30, 2**restarts))


# ---------------------------------------------------------------------------------------------- pipeline


class Pipeline:
    """fetch+hash+inflate thread -> framing filter (caller's thread) -> gzip writer thread."""

    def __init__(self, url: str, total: int | None, symbols: list[str], out_part: Path, min_free_gib: float):
        self.url, self.total = url, total
        self.symbols = [s.upper() for s in symbols]
        self.want = {s.encode().ljust(8): s for s in self.symbols}
        self.out_part = out_part
        self.min_free = min_free_gib
        self.fetch_stats: dict[str, Any] = {}
        self.md5, self.sha_c, self.sha_u = hashlib.md5(), hashlib.sha256(), hashlib.sha256()
        self.n_comp = self.n_uncomp = 0
        self.members = 0
        self.gzip_ok: bool | None = None
        self.q_in: queue.Queue = queue.Queue(maxsize=64)
        self.q_out: queue.Queue = queue.Queue(maxsize=64)
        self.err: list[BaseException] = []
        self.stop = threading.Event()
        self.t_first_byte: float | None = None
        self.t_last_byte: float | None = None
        # filter state
        self.keep = bytearray(65536)
        self.keep[0] = 1
        self.by_type = [0] * 256
        self.kept_by_type = [0] * 256
        self.loc_count = [0] * 65536
        self.len_by_type: dict[int, set[int]] = {}
        self.locates: dict[str, int] = {}
        self.before_dir: dict[str, int] = {}
        self.dup_dir: list[dict] = []
        self.directory_entries = 0
        self.directory: dict[int, str] = {}
        self.zero_frames = self.short_frames = 0
        self.prints: dict[int, dict[str, Any]] = {}
        self.system_events: dict[str, int] = {}
        self.first_ts = self.last_ts = None
        self.kept_msgs = self.kept_framed_bytes = 0
        self.sha_subset_framed = hashlib.sha256()
        self.filter_s = 0.0

    # -- thread 1: bytes in, hashes, inflate
    def _inflate(self) -> None:
        try:
            d = zlib.decompressobj(31)
            fed = 0
            for comp in curl_stream(self.url, self.total, self.fetch_stats):
                if self.stop.is_set():
                    return
                now = time.perf_counter()
                if self.t_first_byte is None:
                    self.t_first_byte = now
                self.t_last_byte = now
                self.md5.update(comp)
                self.sha_c.update(comp)
                self.n_comp += len(comp)
                data = d.decompress(comp)
                fed += len(comp)
                while d.eof:  # gzip member done: CRC32 and ISIZE were checked by zlib
                    self.members += 1
                    rest = d.unused_data
                    d = zlib.decompressobj(31)
                    fed = 0
                    if not rest:
                        break
                    data += d.decompress(rest)
                    fed = len(rest)
                if data:
                    self.sha_u.update(data)
                    self.n_uncomp += len(data)
                    self.q_in.put(data)
            tail = d.flush()
            if tail:
                self.sha_u.update(tail)
                self.n_uncomp += len(tail)
                self.q_in.put(tail)
            # clean end: the last member completed and no bytes were fed to an unfinished one
            self.gzip_ok = self.members > 0 and fed == 0 and not d.eof
            if not self.gzip_ok:
                raise OSError(f"gzip stream ends inside a member ({fed} bytes fed since the last complete member)")
        except BaseException as e:
            self.err.append(e)
            self.gzip_ok = False
        finally:
            self.q_in.put(None)

    # -- thread 3: gzip writer
    def _writer(self) -> None:
        try:
            with open(self.out_part, "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as gz:
                    while True:
                        b = self.q_out.get()
                        if b is None:
                            break
                        self.sha_subset_framed.update(b)
                        gz.write(b)
        except BaseException as e:
            self.err.append(e)
            self.stop.set()

    # -- caller's thread: framing walk
    def _scan(self, b: bytes) -> tuple[int, list[bytes]]:
        """Walks complete frames of `b`; returns (bytes consumed, kept frames). The hot loop of this script."""
        n = len(b)
        pos = 0
        keep, by_type, kept_by_type, loc_count = self.keep, self.by_type, self.kept_by_type, self.loc_count
        out: list[bytes] = []
        app = out.append
        while pos + 2 <= n:
            ln = (b[pos] << 8) | b[pos + 1]
            end = pos + 2 + ln
            if end > n:
                break
            if ln < 3:
                if ln == 0:
                    self.zero_frames += 1  # skipped, as queuetruth's Reader does
                else:
                    self.short_frames += 1
                    by_type[b[pos + 2]] += 1
                    app(b[pos:end])
                pos = end
                continue
            t = b[pos + 2]
            by_type[t] += 1
            loc = (b[pos + 3] << 8) | b[pos + 4]
            loc_count[loc] += 1
            if t == T_R:
                self._directory(b, pos, loc)
            if keep[loc]:
                kept_by_type[t] += 1
                app(b[pos:end])
                if t == T_P or t == T_Q:
                    self._print(b, pos, t, loc)
                elif loc == 0 and t == 83:  # 'S' system event: code -> ns since midnight (et stats.json)
                    self.system_events[chr(b[pos + 13])] = int.from_bytes(b[pos + 7 : pos + 13], "big")
            pos = end
        return pos, out

    def _emit(self, kept: list[bytes]) -> None:
        """Hands the kept frames of one scanned chunk to the writer (`scripts/scale/itch_split.py` overrides it)."""
        if kept:
            chunk = b"".join(kept)
            self.kept_msgs += len(kept)
            self.kept_framed_bytes += len(chunk)
            self.q_out.put(chunk)

    def _directory(self, b: bytes, pos: int, loc: int) -> None:
        self.directory_entries += 1
        raw = bytes(b[pos + 13 : pos + 21])
        self.directory.setdefault(loc, raw.decode("ascii", "replace").strip())
        sym = self.want.get(raw)
        if sym is None:
            return
        if sym in self.locates:
            self.dup_dir.append({"symbol": sym, "locate": loc, "first_locate": self.locates[sym]})
        else:
            self.locates[sym] = loc
            self.before_dir[sym] = self.loc_count[loc] - 1  # messages with this locate before its R
        self.keep[loc] = 1

    def _print(self, b: bytes, pos: int, t: int, loc: int) -> None:
        p = self.prints.get(loc)
        if p is None:
            p = self.prints[loc] = {
                "P": 0,
                "P_shares": 0,
                "Q": 0,
                "Q_zero_shares": 0,
                "Q_shares": 0,
                "Q_cross_type": {},
            }
        m = pos + 2
        if t == T_P:  # type locate track ts(6) ref(8) side(1) shares(4) stock(8) price(4) match(8)
            p["P"] += 1
            p["P_shares"] += int.from_bytes(b[m + 20 : m + 24], "big")
        else:  # type locate track ts(6) shares(8) stock(8) price(4) match(8) cross_type(1)
            sh = int.from_bytes(b[m + 11 : m + 19], "big")
            p["Q"] += 1
            p["Q_shares"] += sh
            p["Q_zero_shares"] += sh == 0
            ct = chr(b[m + 39])
            p["Q_cross_type"][ct] = p["Q_cross_type"].get(ct, 0) + 1

    def run(self, progress_every_s: float = 10.0) -> None:
        t_in = threading.Thread(target=self._inflate, name="inflate", daemon=True)
        t_out = threading.Thread(target=self._writer, name="writer", daemon=True)
        t_in.start()
        t_out.start()
        self.t0 = time.perf_counter()
        carry = b""
        last_report = last_disk = self.t0
        msgs_prev = 0
        try:
            while True:
                data = self.q_in.get()
                if data is None:
                    break
                buf = carry + data if carry else data
                f0 = time.perf_counter()
                used, kept = self._scan(buf)
                self.filter_s += time.perf_counter() - f0
                carry = buf[used:]
                self._emit(kept)
                now = time.perf_counter()
                if now - last_disk >= 2.0:
                    last_disk = now
                    fg = free_gib()
                    if fg < self.min_free:
                        raise DiskGuard(f"free space {fg:.2f} GiB < {self.min_free} GiB")
                if now - last_report >= progress_every_s:
                    msgs = sum(self.by_type)
                    el = now - self.t0
                    pct = f"{100 * self.n_comp / self.total:5.1f}%" if self.total else ""
                    sys.stderr.write(
                        f"[{el:6.0f}s] {pct} {self.n_comp / 1e9:.3f} GB in ({self.n_comp / el / 1e6:.1f} MB/s), "
                        f"{self.n_uncomp / 1e9:.2f} GB out, {msgs / 1e6:.1f}M msgs "
                        f"({(msgs - msgs_prev) / (now - last_report) / 1e6:.2f}M/s now), kept {self.kept_msgs:,}, "
                        f"free {free_gib():.2f} GiB\n"
                    )
                    sys.stderr.flush()
                    last_report, msgs_prev = now, msgs
                if self.err:
                    break
            self.carry_bytes = len(carry)
        except BaseException:
            self.stop.set()
            raise
        finally:
            self.q_out.put(None)
            t_out.join()
            if t_in.is_alive():
                self.stop.set()
                while t_in.is_alive():  # drain so the producer can see stop / finish
                    try:
                        self.q_in.get(timeout=0.5)
                    except queue.Empty:
                        pass
            t_in.join()
        self.wall_s = time.perf_counter() - self.t0
        if self.err:
            raise self.err[0]


# ---------------------------------------------------------------------------------------------- main


def type_counts(arr: list[int]) -> dict[str, int]:
    return {chr(k): v for k, v in sorted(enumerate(arr), key=lambda kv: chr(kv[0])) if v}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def subset_paths(out: Path, date: str, symbols: list[str], tag: str | None = None) -> tuple[Path, Path]:
    tag = tag or "-".join(symbols)
    return out / f"{date}.subset.{tag}.itch50.gz", out / f"{date}.subset.{tag}.manifest.json"


def run_subset(args: argparse.Namespace) -> int:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    sub, man_path = subset_paths(out, args.date, symbols, getattr(args, "tag", None))
    et_root = getattr(args, "et_root", None)
    if args.date and not args.url:
        ref = public_reference(args.date)
        if et_root:  # optional: the earlier prototype's (et) records of the same file
            ref["et"] = et_reference(Path(et_root), args.date)
    else:
        ref = {}
    et_ref = ref.get("et") or {}
    url = args.url or ref.get("url")
    if not url:
        print("no URL: pass --date or --url", file=sys.stderr)
        return EXIT_INPUT
    fg = free_gib()
    print(f"free space on /: {fg:.2f} GiB (guard {args.min_free_gib} GiB)", file=sys.stderr)
    if fg < args.min_free_gib + 0.3:
        print("not enough free space to start", file=sys.stderr)
        return EXIT_DISK
    try:
        head = http_head(url)
    except OSError as e:
        print(e, file=sys.stderr)
        return EXIT_INPUT
    if head["status"] != 200:
        print(f"HTTP {head['status']} for {url}", file=sys.stderr)
        return EXIT_INPUT
    total = head["content_length"]
    print(f"streaming {url}: {total:,} bytes (pinned {ref.get('bytes')})", file=sys.stderr)
    part = sub.with_name(sub.name + ".part")
    pipe = Pipeline(url, total, symbols, part, args.min_free_gib)
    started = now_iso()
    try:
        pipe.run()
    except DiskGuard as e:
        part.unlink(missing_ok=True)
        print(f"ABORT (disk guard): {e}; partial subset deleted", file=sys.stderr)
        return EXIT_DISK
    except (OSError, zlib.error) as e:
        part.unlink(missing_ok=True)
        print(f"ABORT: {type(e).__name__}: {e}; partial subset deleted", file=sys.stderr)
        return EXIT_INPUT
    except KeyboardInterrupt:
        part.unlink(missing_ok=True)
        raise
    part.replace(sub)
    msgs = sum(pipe.by_type)
    by_type = type_counts(pipe.by_type)
    kept_by_type = type_counts(pipe.kept_by_type)
    md5 = pipe.md5.hexdigest()
    checks: dict[str, Any] = {}

    def chk(name: str, got: Any, exp: Any) -> None:
        checks[name] = {"got": got, "expected": exp, "ok": None if exp is None else got == exp}

    chk("compressed_bytes == HTTP Content-Length", pipe.n_comp, total)
    sysev = dict(sorted(pipe.system_events.items()))
    chk("compressed_bytes == pinned size", pipe.n_comp, ref.get("bytes"))
    chk("md5 == pinned md5", md5, ref.get("md5"))
    chk("compressed sha256 == pinned sha256", pipe.sha_c.hexdigest(), ref.get("sha256"))
    chk("decompressed sha256 == pinned", pipe.sha_u.hexdigest(), ref.get("decompressed_sha256"))
    chk("gzip members complete (CRC32 + ISIZE verified by zlib)", pipe.gzip_ok, True)
    chk("no partial frame at end of stream", getattr(pipe, "carry_bytes", None), 0)
    chk("messages == pinned messages", msgs, ref.get("messages"))
    chk("by_type == pinned by_type", by_type, ref.get("by_type"))
    chk("system_events == pinned system_events", sysev, ref.get("system_events"))
    chk("directory_entries == pinned", pipe.directory_entries, ref.get("directory_entries"))
    if et_ref:
        chk("compressed_bytes == et recorded size", pipe.n_comp, et_ref.get("bytes"))
        chk("md5 == et recorded md5", md5, et_ref.get("md5"))
        chk("messages == et stats.json messages", msgs, et_ref.get("messages"))
        chk("by_type == et stats.json by_type", by_type, et_ref.get("by_type"))
        chk("system_events == et stats.json system_events", sysev, et_ref.get("system_events"))
        chk("directory_entries == et stats.json", pipe.directory_entries, et_ref.get("directory_entries"))
    missing = sorted(set(symbols) - set(pipe.locates))
    chk("every requested symbol found", missing, None if getattr(args, "allow_missing", False) else [])
    chk("no symbol message before its R", {s: n for s, n in pipe.before_dir.items() if n}, {})
    chk("no duplicate directory entry for a requested symbol", pipe.dup_dir, [])
    chk("zero-length frames", pipe.zero_frames, 0)
    chk("short frames (< 3 bytes)", pipe.short_frames, 0)
    # The subset re-read: framing walk, per-type lengths against the ITCH 5.0 table, digest of framed content.
    t_v = time.perf_counter()
    lens: dict[str, set[int]] = {}
    n_sub = 0
    h_framed = hashlib.sha256()
    with gzip.open(sub, "rb") as g:
        carry = b""
        while True:
            d = g.read(1 << 22)
            if not d:
                break
            h_framed.update(d)
            b = carry + d
            pos, n = 0, len(b)
            while pos + 2 <= n:
                ln = (b[pos] << 8) | b[pos + 1]
                if pos + 2 + ln > n:
                    break
                lens.setdefault(chr(b[pos + 2]), set()).add(ln)
                n_sub += 1
                pos += 2 + ln
            carry = b[pos:]
    bad_len = {t: sorted(v) for t, v in lens.items() if v != {ITCH50_LEN.get(t)}}
    chk("subset re-read: messages == kept", n_sub, pipe.kept_msgs)
    chk("subset re-read: framed sha256 == written", h_framed.hexdigest(), pipe.sha_subset_framed.hexdigest())
    chk("subset re-read: every frame length == ITCH 5.0 length of its type", bad_len, {})
    verify_s = time.perf_counter() - t_v
    dl_s = (pipe.t_last_byte or 0) - (pipe.t_first_byte or 0)
    loc_to_sym = {v: k for k, v in pipe.locates.items()}
    manifest = {
        "schema": "qt.realdata.itch_subset",
        "schema_version": 1,
        "generator": "queuetruth itch_subset.py",
        "started_at": started,
        "finished_at": now_iso(),
        "source": {"url": url, "http": head, "fetch": "curl -fsSL <url> (range resume on failure)", **pipe.fetch_stats},
        "reference": ref,
        "stream": {
            "compressed_bytes": pipe.n_comp,
            "compressed_md5": md5,
            "compressed_sha256": pipe.sha_c.hexdigest(),
            "decompressed_bytes": pipe.n_uncomp,
            "decompressed_sha256": pipe.sha_u.hexdigest(),
            "gzip_members": pipe.members,
            "messages": msgs,
            "by_type": by_type,
            "directory_entries": pipe.directory_entries,
            "system_events_ns_since_midnight": dict(sorted(pipe.system_events.items())),
            "locate0_messages": pipe.loc_count[0],
        },
        "selection": {
            "symbols": symbols,
            "missing_symbols": missing,
            "rule": "keep a message iff its header stock locate is 0 or the locate of a requested symbol (learned "
            "from R); R kept only for requested symbols; zero-length frames skipped",
            "locates": dict(sorted(pipe.locates.items())),
            "messages_by_symbol": {s: pipe.loc_count[loc] for s, loc in sorted(pipe.locates.items())},
            "messages_before_directory": pipe.before_dir,
            "prints_by_symbol": {loc_to_sym.get(k, str(k)): v for k, v in sorted(pipe.prints.items())},
        },
        "subset": {
            "path": str(sub),
            "file_bytes": sub.stat().st_size,
            "file_sha256": sha256_file(sub),
            "framed_bytes": pipe.kept_framed_bytes,
            "framed_sha256": pipe.sha_subset_framed.hexdigest(),
            "messages": pipe.kept_msgs,
            "by_type": kept_by_type,
            "gzip": "python gzip, compresslevel 6, mtime 0, empty name, one member",
        },
        "timings": {
            "wall_s": round(pipe.wall_s, 2),
            "download_s": round(dl_s, 2),
            "download_MBps": round(pipe.n_comp / dl_s / 1e6, 2) if dl_s > 0 else None,
            "filter_cpu_s": round(pipe.filter_s, 2),
            "filter_msgs_per_s": round(msgs / pipe.filter_s) if pipe.filter_s else None,
            "end_to_end_msgs_per_s": round(msgs / pipe.wall_s) if pipe.wall_s else None,
            "subset_verify_s": round(verify_s, 2),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "curl": subprocess.run(["curl", "--version"], capture_output=True, text=True).stdout.split("\n")[0],
            "free_gib_at_end": round(free_gib(), 2),
        },
        "checks": checks,
        "ok": all(c["ok"] is not False for c in checks.values()),
        "not_checked": sorted(k for k, c in checks.items() if c["ok"] is None),
    }
    man_path.write_text(json.dumps(manifest, indent=1) + "\n")
    dir_path = out / f"{args.date}.directory.json"
    if args.date and not args.url:
        dir_doc = {
            "schema": "qt.realdata.itch_directory",
            "date": args.date,
            "source_sha256": pipe.sha_c.hexdigest(),
            "note": "every Stock Directory symbol: [locate, messages with that locate in the full file]",
            "symbols": {sym: [loc, pipe.loc_count[loc]] for loc, sym in sorted(pipe.directory.items())},
        }
        dir_path.write_text(json.dumps(dir_doc, separators=(",", ":")) + "\n")
    print(json.dumps({k: manifest[k] for k in ("ok", "timings")}, indent=1))
    for k, c in checks.items():
        tag = {True: "ok ", False: "BAD", None: "n/a"}[c["ok"]]
        print(f"  {tag} {k}: {c['got'] if not isinstance(c['got'], dict) or len(c['got']) < 6 else '...'}")
    print(f"subset {sub} ({sub.stat().st_size:,} bytes, {pipe.kept_msgs:,} messages); manifest {man_path}")
    return EXIT_OK if manifest["ok"] else EXIT_VERIFY


# ---------------------------------------------------------------------------------------------- self-test


def _synth_stream(n_msgs: int, seed: int = 1) -> tuple[bytes, dict[str, int]]:
    """A framed synthetic day: S, R for 50 symbols (two requested), then random messages of every type."""
    rng = random.Random(seed)
    syms = ["AAPL", "MSFT"] + [f"S{i:03d}" for i in range(48)]
    locs = {s: i + 1 for i, s in enumerate(syms)}
    buf = io.BytesIO()

    def frame(t: str, loc: int, body: bytes = b"") -> None:
        ln = ITCH50_LEN[t]
        m = t.encode() + loc.to_bytes(2, "big") + b"\x00\x00" + rng.randrange(1 << 40).to_bytes(6, "big") + body
        m = m.ljust(ln, b"\x01")[:ln]
        buf.write(ln.to_bytes(2, "big") + m)

    frame("S", 0, b"O")
    for s in syms:
        frame("R", locs[s], s.encode().ljust(8))
    types = [t for t in ITCH50_LEN if t not in "RS"]
    for _ in range(n_msgs):
        t = rng.choice(types)
        loc = 0 if t in "VW" else locs[rng.choice(syms)]
        frame(t, loc)
    frame("S", 0, b"C")
    buf.write(b"\x00\x00")  # one zero-length frame
    return buf.getvalue(), locs


def _reference_filter(raw: bytes, want: set[str]) -> bytes:
    keep = {0}
    out = io.BytesIO()
    pos = 0
    while pos < len(raw):
        ln = int.from_bytes(raw[pos : pos + 2], "big")
        m = raw[pos + 2 : pos + 2 + ln]
        if ln:
            loc = int.from_bytes(m[1:3], "big")
            if m[:1] == b"R" and m[11:19].decode().strip() in want:
                keep.add(loc)
            if loc in keep:
                out.write(raw[pos : pos + 2 + ln])
        pos += 2 + ln
    return out.getvalue()


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="itch-subset-selftest-"))
    try:
        raw, _ = _synth_stream(300_000)
        # two gzip members, like a concatenated file, to exercise the multi-member path
        cut = len(raw) // 3
        gz = gzip.compress(raw[:cut], mtime=0) + gzip.compress(raw[cut:], mtime=0)
        src = tmp / "11112011.NASDAQ_ITCH50.gz"
        src.write_bytes(gz)
        a = argparse.Namespace(
            date="11112011", symbols="AAPL,MSFT", out=tmp / "out", url=f"file://{src}", et_root=tmp, min_free_gib=0.0
        )
        rc = run_subset(a)
        sub, man = subset_paths(a.out, "11112011", ["AAPL", "MSFT"])
        m = json.loads(man.read_text())
        exp = _reference_filter(raw, {"AAPL", "MSFT"})
        got = gzip.decompress(sub.read_bytes())
        assert got == exp, (len(got), len(exp))
        assert m["stream"]["compressed_md5"] == hashlib.md5(gz).hexdigest()
        assert m["stream"]["decompressed_sha256"] == hashlib.sha256(raw).hexdigest()
        assert m["stream"]["gzip_members"] == 2
        assert m["subset"]["framed_sha256"] == hashlib.sha256(exp).hexdigest()
        assert m["selection"]["locates"] == {"AAPL": 1, "MSFT": 2}
        # the zero-length frame is reported, so rc is 1 (verification), everything else ok
        bad = [k for k, c in m["checks"].items() if c["ok"] is False]
        assert bad == ["zero-length frames"], bad
        assert rc == EXIT_VERIFY
        # truncated gzip -> input error, no subset left
        src.write_bytes(gz[:-100])
        sub.unlink()
        assert run_subset(a) == EXIT_INPUT and not sub.exists() and not sub.with_name(sub.name + ".part").exists()
        # speed of the framing walk on 5M synthetic messages (the real file averages ~29 bytes / message)
        raw, _ = _synth_stream(5_000_000, seed=2)
        p = Pipeline("file:///dev/null", None, ["AAPL", "MSFT"], tmp / "x.gz", 0.0)
        t0 = time.perf_counter()
        step = 1 << 21
        carry = b""
        for i in range(0, len(raw), step):
            b = carry + raw[i : i + step]
            used, _k = p._scan(b)
            carry = b[used:]
        dt_s = time.perf_counter() - t0
        n = sum(p.by_type)
        print(f"self-test: framing walk {n / dt_s / 1e6:.2f}M msgs/s ({len(raw) / n:.1f} B/msg synthetic)")
        print("itch_subset self-test: all ok")
        return EXIT_OK
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="MMDDYYYY of the emi.nasdaq.com file, e.g. 12302019")
    ap.add_argument("--symbols", default="AAPL,MSFT,SPY,TSLA")
    ap.add_argument(
        "--url", help="override the URL (file:// works, e.g. a larger local subset; skips the pinned checks)"
    )
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--tag", help="name outputs <date>.subset.<tag>.* instead of the joined symbol list")
    ap.add_argument(
        "--allow-missing", action="store_true", help="a requested symbol missing from the day is not a failure"
    )
    ap.add_argument(
        "--et-root",
        type=Path,
        default=Path(os.environ["QT_ET_ROOT"]) if os.environ.get("QT_ET_ROOT") else None,
        help="path to the author's earlier private prototype (et) outputs, optional; also QT_ET_ROOT",
    )
    ap.add_argument("--min-free-gib", type=float, default=2.5)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.date and not a.url:
        ap.error("--date or --url is required")
    return run_subset(a)


if __name__ == "__main__":
    sys.exit(main())
