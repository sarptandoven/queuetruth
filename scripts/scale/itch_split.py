#!/usr/bin/env python3
"""Stream one Nasdaq TotalView-ITCH 5.0 day once and route every message into K symbol buckets (W1 scale runs).

    python3 -I scripts/scale/itch_split.py --date 12302019 --buckets 32 --out .realdata/scale/split
    python3 -I scripts/scale/itch_split.py --date 12302019 --buckets 8 --symbols AAPL,MSFT,...   # a subset
    python3 -I scripts/scale/itch_split.py --url file:///path/12302019.NASDAQ_ITCH50.gz --buckets 4 --out DIR
    python3 -I scripts/scale/itch_split.py --self-test

The raw day is never stored: the same pipeline as `scripts/realdata/itch_subset.py` (curl -> md5 / sha256 of the
compressed bytes -> inflate with gzip CRC / ISIZE checks -> sha256 of the decompressed stream -> ITCH framing walk),
with the subset rule generalised from "keep" to "route":

- a symbol's bucket is `crc32(symbol padded to 8 bytes) mod K`, fixed across days (locates are per day);
- its locate is learned from its `R` (Stock Directory) message; the `R` and every later message with that locate go
  to the symbol's bucket (messages of a locate before its `R` are counted and dropped, as `itch_subset.py` does);
- locate 0 (system / market-wide `S`, `V`, `W`) goes to every bucket;
- with `--symbols`, only those symbols are routed (the rest dropped), for local proofs on a few names.

So each bucket holds exactly the messages a symbol-subset stream of its symbols keeps, in file order, and an ITCH
ingest of a bucket gives the same partition files as an ingest of the symbol subset (tested in
`python/tests/scale/test_itch_split.py`).

Outputs in `<out>/<date>/`: `<date>.bucket-<kk>-of-<KK>.itch50.gz` (gzip, `--level`, mtime 0, one member each),
`split_manifest.json` (stream hashes and checks against the pinned public-day values, per bucket the file sha256,
the sha256 of its framed content, messages and per symbol `[locate, messages, executions]`, where executions count
`E`, `C` and `P` messages, the planner's cost and quota input; `routing.test_issues`, every directory symbol whose
Authenticity is `T`, which `plan.py` excludes; `trading_state`, per symbol of the whole day the spans in which its
`H` Stock Trading Action state was not T (halted, paused, quotation only), which `plan.py` copies into the
universe and `atlas.py reduce-day` turns into the `trading_ok` label) and `<date>.directory.json` (every directory
symbol: `[locate, messages in the full file]`, as `itch_subset.py`). Exit codes as `itch_subset.py`: 0 ok, 1 a check
failed, 2 input error, 4 disk guard.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import sys
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "realdata"))

import itch_subset as S  # noqa: E402

DROP, BROADCAST = -1, -2
T_E, T_C, T_H = ord("E"), ord("C"), ord("H")
TRADING_STATE_RULE = (
    "per symbol (from the Stock Trading Action `H` messages of the whole day, routed or not), every `H` whose state "
    "is not T opens a span [its timestamp, the symbol's next `H` (null: the end of the day)] with that state and "
    "reason; ns since midnight; a symbol whose every `H` says T is not listed"
)


def non_t_spans(actions: dict[str, list[tuple[int, str, str]]]) -> dict[str, list[list[Any]]]:
    """`{symbol: [[start_ns, end_ns or None, state, reason], ...]}` for the symbols with any `H` state other than T
    (`actions`: per symbol its `H` messages `(ts_ns, state, reason)` in file order; `TRADING_STATE_RULE`)."""
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


def bucket_of(symbol: str, k: int) -> int:
    """Bucket of a symbol: `crc32(symbol, ASCII, right-padded with spaces to 8 bytes) mod k`."""
    return zlib.crc32(symbol.encode("ascii").ljust(8)) % k


def bucket_name(date: str, i: int, k: int) -> str:
    """File name of bucket `i` of `k` (starts with MMDDYYYY, so `ingest(format="itch")` reads the session date)."""
    w = max(2, len(str(k - 1)))
    return f"{date}.bucket-{i:0{w}d}-of-{k:0{w}d}.itch50.gz"


class SplitPipeline(S.Pipeline):
    """`itch_subset.Pipeline` with K outputs: `_scan` routes frames by locate, the writer thread gzips K files."""

    def __init__(
        self,
        url: str,
        total: int | None,
        k: int,
        paths: list[Path],
        min_free_gib: float,
        symbols: list[str] | None = None,
        level: int = 1,
    ):
        super().__init__(url, total, symbols or [], paths[0], min_free_gib)
        self.k = k
        self.paths = paths
        self.level = level
        self.only = None if symbols is None else set(self.want)
        self.repeated_dir: list[dict[str, Any]] = []
        self.test_issues: set[str] = set()
        self.actions: dict[str, list[tuple[int, str, str]]] = {}
        self.h_messages = 0
        self.route = [DROP] * 65536
        self.route[0] = BROADCAST
        self.exec_count = [0] * 65536
        self.bucket_msgs = [0] * k
        self.bucket_bytes = [0] * k
        self.bucket_sha = [hashlib.sha256() for _ in range(k)]

    def _scan(self, b: bytes) -> tuple[int, list[list[bytes]]]:  # type: ignore[override]
        """Walks complete frames of `b`; returns (bytes consumed, frames per bucket). The hot loop."""
        n = len(b)
        pos = 0
        route, by_type, kept_by_type, loc_count, ex = (
            self.route,
            self.by_type,
            self.kept_by_type,
            self.loc_count,
            self.exec_count,
        )
        outs: list[list[bytes]] = [[] for _ in range(self.k)]
        while pos + 2 <= n:
            ln = (b[pos] << 8) | b[pos + 1]
            end = pos + 2 + ln
            if end > n:
                break
            if ln < 3:
                if ln == 0:
                    self.zero_frames += 1
                else:
                    self.short_frames += 1
                    by_type[b[pos + 2]] += 1
                pos = end
                continue
            t = b[pos + 2]
            by_type[t] += 1
            loc = (b[pos + 3] << 8) | b[pos + 4]
            loc_count[loc] += 1
            if t == S.T_R:
                self._directory(b, pos, loc)
            elif t == T_E or t == T_C or t == S.T_P:
                ex[loc] += 1
            elif t == T_H and ln >= 25:
                self._trading_action(b, pos)
            r = route[loc]
            if r >= 0:
                kept_by_type[t] += 1
                outs[r].append(b[pos:end])
                if t == S.T_P or t == S.T_Q:
                    self._print(b, pos, t, loc)
            elif r == BROADCAST:
                kept_by_type[t] += 1
                f = b[pos:end]
                for o in outs:
                    o.append(f)
                if t == 83:  # 'S' system event: code -> ns since midnight
                    self.system_events[chr(b[pos + 13])] = int.from_bytes(b[pos + 7 : pos + 13], "big")
            pos = end
        return pos, outs

    def _directory(self, b: bytes, pos: int, loc: int) -> None:
        self.directory_entries += 1
        raw = bytes(b[pos + 13 : pos + 21])
        sym = raw.decode("ascii", "replace").strip()
        self.directory.setdefault(loc, sym)
        if ((b[pos] << 8) | b[pos + 1]) >= 30 and b[pos + 31] == 84:  # Authenticity 'T' (Stock Directory offset 29)
            self.test_issues.add(sym)
        if self.only is not None and raw not in self.only:
            return
        if sym in self.locates:
            entry = {"symbol": sym, "locate": loc, "first_locate": self.locates[sym]}
            # the same symbol re-sent on its own locate (seen in the 2020-01-30 file) changes nothing; only a symbol
            # reappearing on another locate would split its messages
            (self.repeated_dir if loc == self.locates[sym] else self.dup_dir).append(entry)
            return
        self.locates[sym] = loc
        self.before_dir[sym] = self.loc_count[loc] - 1
        self.route[loc] = bucket_of(sym, self.k)

    def _trading_action(self, b: bytes, pos: int) -> None:
        """An `H` (Stock Trading Action) of any symbol, routed or not: kept for `split_manifest.json`
        `trading_state` (stock at offset 11, state at 19, reason at 21..24 of the message)."""
        self.h_messages += 1
        sym = bytes(b[pos + 13 : pos + 21]).decode("ascii", "replace").strip()
        ts = int.from_bytes(b[pos + 7 : pos + 13], "big")
        reason = bytes(b[pos + 23 : pos + 27]).decode("ascii", "replace").strip()
        self.actions.setdefault(sym, []).append((ts, chr(b[pos + 21]), reason))

    def _emit(self, kept: list[list[bytes]]) -> None:  # type: ignore[override]
        chunks = [b"".join(o) for o in kept]
        for i, o in enumerate(kept):
            self.bucket_msgs[i] += len(o)
        if any(chunks):
            self.kept_msgs += sum(len(o) for o in kept)
            self.kept_framed_bytes += sum(len(c) for c in chunks)
            self.q_out.put(chunks)

    def _writer(self) -> None:
        try:
            raws = [open(p, "wb") for p in self.paths]
            gzs = [gzip.GzipFile(filename="", mode="wb", fileobj=r, compresslevel=self.level, mtime=0) for r in raws]
            try:
                while True:
                    chunks = self.q_out.get()
                    if chunks is None:
                        break
                    for i, c in enumerate(chunks):
                        if c:
                            self.bucket_sha[i].update(c)
                            self.bucket_bytes[i] += len(c)
                            gzs[i].write(c)
            finally:
                for g in gzs:
                    g.close()
                for r in raws:
                    r.close()
        except BaseException as e:
            self.err.append(e)
            self.stop.set()


def run_split(a: argparse.Namespace) -> int:
    date = a.date or "00000000"
    out: Path = a.out / date
    out.mkdir(parents=True, exist_ok=True)
    ref = S.public_reference(a.date) if a.date and not a.url else {}
    url = a.url or ref.get("url")
    if not url:
        print("no URL: pass --date or --url", file=sys.stderr)
        return S.EXIT_INPUT
    if S.free_gib() < a.min_free_gib + 0.3:
        print("not enough free space to start", file=sys.stderr)
        return S.EXIT_DISK
    try:
        head = S.http_head(url)
    except OSError as e:
        print(e, file=sys.stderr)
        return S.EXIT_INPUT
    if head["status"] != 200:
        print(f"HTTP {head['status']} for {url}", file=sys.stderr)
        return S.EXIT_INPUT
    total = head["content_length"]
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] if a.symbols else None
    finals = [out / bucket_name(date, i, a.buckets) for i in range(a.buckets)]
    parts = [p.with_name(p.name + ".part") for p in finals]
    pipe = SplitPipeline(url, total, a.buckets, parts, a.min_free_gib, symbols, a.level)
    print(f"splitting {url} ({total:,} bytes) into {a.buckets} buckets", file=sys.stderr)
    started = S.now_iso()

    def drop_parts() -> None:
        for p in parts:
            p.unlink(missing_ok=True)

    try:
        pipe.run()
    except S.DiskGuard as e:
        drop_parts()
        print(f"ABORT (disk guard): {e}; partial buckets deleted", file=sys.stderr)
        return S.EXIT_DISK
    except (OSError, zlib.error) as e:
        drop_parts()
        print(f"ABORT: {type(e).__name__}: {e}; partial buckets deleted", file=sys.stderr)
        return S.EXIT_INPUT
    except KeyboardInterrupt:
        drop_parts()
        raise
    for p, f in zip(parts, finals, strict=True):
        p.replace(f)
    msgs = sum(pipe.by_type)
    by_type = S.type_counts(pipe.by_type)
    checks: dict[str, Any] = {}

    def chk(name: str, got: Any, exp: Any) -> None:
        checks[name] = {"got": got, "expected": exp, "ok": None if exp is None else got == exp}

    chk("compressed_bytes == HTTP Content-Length", pipe.n_comp, total)
    chk("compressed_bytes == pinned size", pipe.n_comp, ref.get("bytes"))
    chk("md5 == pinned md5", pipe.md5.hexdigest(), ref.get("md5"))
    chk("compressed sha256 == pinned sha256", pipe.sha_c.hexdigest(), ref.get("sha256"))
    chk("decompressed sha256 == pinned", pipe.sha_u.hexdigest(), ref.get("decompressed_sha256"))
    chk("gzip members complete (CRC32 + ISIZE verified by zlib)", pipe.gzip_ok, True)
    chk("no partial frame at end of stream", getattr(pipe, "carry_bytes", None), 0)
    chk("messages == pinned messages", msgs, ref.get("messages"))
    chk("by_type == pinned by_type", by_type, ref.get("by_type"))
    chk("directory_entries == pinned", pipe.directory_entries, ref.get("directory_entries"))
    missing = sorted(set(symbols or []) - set(pipe.locates))
    chk("every requested symbol found", missing, None if a.allow_missing else [])
    chk("no symbol message before its R", {s: n for s, n in pipe.before_dir.items() if n}, {})
    chk("no symbol on two locates", pipe.dup_dir, [])
    chk("zero-length frames", pipe.zero_frames, 0)
    chk("short frames (< 3 bytes)", pipe.short_frames, 0)
    loc0 = pipe.loc_count[0]
    routed = sum(pipe.loc_count[loc] - pipe.before_dir.get(s, 0) for s, loc in pipe.locates.items())
    chk("bucket messages == routed + K x locate-0", sum(pipe.bucket_msgs), routed + a.buckets * loc0)
    buckets = []
    for i, f in enumerate(finals):
        syms = {s: loc for s, loc in pipe.locates.items() if pipe.route[loc] == i}
        buckets.append(
            {
                "bucket": i,
                "file": f.name,
                "file_bytes": f.stat().st_size,
                "file_sha256": S.sha256_file(f),
                "framed_bytes": pipe.bucket_bytes[i],
                "framed_sha256": pipe.bucket_sha[i].hexdigest(),
                "messages": pipe.bucket_msgs[i],
                "symbols": {
                    s: [loc, pipe.loc_count[loc] - pipe.before_dir.get(s, 0), pipe.exec_count[loc]]
                    for s, loc in sorted(syms.items())
                },
            }
        )
    dl_s = (pipe.t_last_byte or 0) - (pipe.t_first_byte or 0)
    manifest = {
        "schema": "qt.scale.itch_split",
        "schema_version": 1,
        "generator": "scripts/scale/itch_split.py",
        "started_at": started,
        "finished_at": S.now_iso(),
        "date": date,
        "source": {"url": url, "http": head, **pipe.fetch_stats},
        "stream": {
            "compressed_bytes": pipe.n_comp,
            "compressed_md5": pipe.md5.hexdigest(),
            "compressed_sha256": pipe.sha_c.hexdigest(),
            "decompressed_bytes": pipe.n_uncomp,
            "decompressed_sha256": pipe.sha_u.hexdigest(),
            "gzip_members": pipe.members,
            "messages": msgs,
            "by_type": by_type,
            "directory_entries": pipe.directory_entries,
            "system_events_ns_since_midnight": dict(sorted(pipe.system_events.items())),
            "locate0_messages": loc0,
        },
        "routing": {
            "k": a.buckets,
            "rule": "bucket = crc32(symbol ljust 8, ASCII) mod k; locate 0 to every bucket; a locate's messages from "
            "its R on to its symbol's bucket; messages before a locate's R dropped",
            "symbols": symbols,
            "missing_symbols": missing,
            "repeated_directory_entries": pipe.repeated_dir,
            "test_issues": sorted(pipe.test_issues),
            "gzip": f"python gzip, compresslevel {a.level}, mtime 0, empty name, one member",
        },
        "trading_state": {
            "rule": TRADING_STATE_RULE,
            "h_messages": pipe.h_messages,
            "symbols_with_h": len(pipe.actions),
            "non_t_spans": non_t_spans(pipe.actions),
        },
        "buckets": buckets,
        "timings": {
            "wall_s": round(pipe.wall_s, 2),
            "download_s": round(dl_s, 2),
            "scan_cpu_s": round(pipe.filter_s, 2),
            "scan_msgs_per_s": round(msgs / pipe.filter_s) if pipe.filter_s else None,
        },
        "checks": checks,
        "ok": all(c["ok"] is not False for c in checks.values()),
        "not_checked": sorted(k for k, c in checks.items() if c["ok"] is None),
    }
    (out / "split_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    directory = {
        "schema": "qt.realdata.itch_directory",
        "date": date,
        "source_sha256": pipe.sha_c.hexdigest(),
        "note": "every Stock Directory symbol: [locate, messages with that locate in the full file]; test_issues: "
        "the symbols whose Stock Directory Authenticity is T",
        "symbols": {sym: [loc, pipe.loc_count[loc]] for loc, sym in sorted(pipe.directory.items())},
        "test_issues": sorted(pipe.test_issues),
    }
    (out / f"{date}.directory.json").write_text(json.dumps(directory, separators=(",", ":")) + "\n")
    bad = [k for k, c in checks.items() if c["ok"] is False]
    print(json.dumps({"ok": manifest["ok"], "bad": bad, "timings": manifest["timings"]}, indent=1))
    return S.EXIT_OK if manifest["ok"] else S.EXIT_VERIFY


def self_test() -> int:
    """Routing on the synthetic stream of `itch_subset.py`: every bucket equals the reference filter of its symbols."""
    tmp = Path(tempfile.mkdtemp(prefix="itch-split-selftest-"))
    raw, locs = S._synth_stream(200_000)
    raw = raw[:-2]  # drop the trailing zero-length frame (reported, not routed)
    src = tmp / "11112011.NASDAQ_ITCH50.gz"
    src.write_bytes(gzip.compress(raw, mtime=0))
    a = argparse.Namespace(
        date=None, url=f"file://{src}", buckets=4, out=tmp, symbols=None, allow_missing=False, level=1, min_free_gib=0
    )
    rc = run_split(a)
    m = json.loads((tmp / "00000000" / "split_manifest.json").read_text())
    for b in m["buckets"]:
        got = gzip.decompress((tmp / "00000000" / b["file"]).read_bytes())
        assert got == S._reference_filter(raw, set(b["symbols"])), b["bucket"]
    assert sum(len(b["symbols"]) for b in m["buckets"]) == len(locs)
    assert rc == S.EXIT_OK, [k for k, c in m["checks"].items() if c["ok"] is False]
    rng = random.Random(0)
    sample = rng.sample(sorted(locs), 5)
    assert all(bucket_of(s, 4) == next(b["bucket"] for b in m["buckets"] if s in b["symbols"]) for s in sample)
    t0 = time.perf_counter()
    p = SplitPipeline("file:///dev/null", None, 4, [tmp / "x"] * 4, 0.0)
    p._scan(raw)
    print(f"itch_split self-test: ok ({sum(p.by_type) / (time.perf_counter() - t0) / 1e6:.2f}M msgs/s routing)")
    return S.EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="public EMI day MMDDYYYY (URL and pinned reference values from itch_subset.py)")
    ap.add_argument("--url", help="any http(s):// or file:// ITCH 5.0 gzip day instead of --date")
    ap.add_argument("--buckets", type=int, default=32)
    ap.add_argument("--symbols", help="route only these symbols (comma separated); default every directory symbol")
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--level", type=int, default=1, help="gzip level of the bucket files (default 1: fast)")
    ap.add_argument("--out", type=Path, default=S.REPO / ".realdata" / "scale" / "split")
    ap.add_argument("--min-free-gib", type=float, default=2.5)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not 1 <= a.buckets <= 4096:
        ap.error("--buckets: 1..4096")
    return run_split(a)


if __name__ == "__main__":
    raise SystemExit(main())
