"""Build (or reuse) the demo's real-data subset: one pass over a day file's prefix.

    Stream (HTTP with ranged resumption, or a file)  ->  sha256 + byte count of the raw bytes  ->  gzip inflate
    (thread 1)  ->  symbol filter with a market-time cut (caller's thread)  ->  gzip subset (level 1, mtime 0)

The download stops as soon as the filter reaches the cut, so only a prefix of the day file is transferred.
`fetch_subset` builds into a fresh sibling directory and swaps it into the cache entry only on success, so a failed
or interrupted fetch (or `--refresh`) never leaves a broken entry or removes a good one, and two runs on the same key
never touch each other's files. A later run with the same key reuses the entry after checking the manifest's schema,
filter version and the subset's sha256 (no download). For the pinned default configurations (`PINS`) the subset
content (the framed ITCH messages, or the pcap bytes before gzip) must hash to the pinned sha256, on a build and on a
cache hit; a mismatch on a build is `QT-INPUT-002` and nothing is cached. Any input whose content matches a pin
(e.g. a subset the demo cut, fed back through `--from-file` or `--url`) is reported as that pinned source.

Completeness: the subset must cover every shadow's label, i.e. reach `window.complete_s` (the end of the placement
window plus `h_max`). The input either reaches the cut (`cut`), or ends after its last message at or past
`complete_s` (`end_of_input`: a subset the demo cut itself ends just before the cut, so the demo can read its own
subsets back); anything shorter is `QT-INPUT-002`.

Guards: the first decompressed bytes must look like the declared format (`QT-INPUT-002` otherwise); an IEX input
whose first Send Time is on another US/Eastern date than the session is `QT-INPUT-002` at once; `max_download_bytes`
stops a stream that has not reached the cut (`QT-INPUT-002`: wrong file, or a cut past the file's end); free disk
space below `min_free_bytes` stops it too (`QT-INPUT-001`).
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import queue
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from queuetruth._version import __version__
from queuetruth.demo.fetch import Inflater, Stream, is_url
from queuetruth.demo.iex import CaptureError, IexCutFilter
from queuetruth.demo.itch import ItchCutFilter
from queuetruth.demo.sources import SOURCES, DemoWindow, Source, sniff_format
from queuetruth.errors import QtError

MANIFEST_SCHEMA = "queuetruth.demo_subset"
MANIFEST_VERSION = 1
#: Version of the selection rules of `ItchCutFilter` / `IexCutFilter`; a cache entry written by another version is
#: not reused.
FILTER_VERSION = 1
#: Pinned subset content hashes of the default configurations: (source id, symbols, placement tag) -> (sha256 of
#: the content, kept messages). Recorded from the first real runs on 2026-10-24 (docs/getting-started.md).
PINS: dict[tuple[str, tuple[str, ...], str], tuple[str, int]] = {
    ("nasdaq-2019-12-30", ("AAPL", "MSFT", "SPY", "TSLA"), "0935-5m"): (
        "1f04ab8ec17adf0229151932012b4921269e93ed2e011630f6c62734982803b6",
        576_953,
    ),
    ("iex-2026-10-09", ("SPY", "AAPL", "NVDA", "TSLA"), "0935-5m"): (
        "9b9eac384d557d00f4e8cb52fd445958d3124b67dcd740499d8912179bca8dce",
        430_905,
    ),
}

Progress = Callable[[dict[str, Any]], None]


@dataclass
class SubsetResult:
    """Where the subset is and what it took to make it."""

    path: Path
    manifest_path: Path
    manifest: dict[str, Any]
    cache_hit: bool
    bytes_downloaded: int
    wall_s: float


def cache_key(source: Source, symbols: list[str], window: DemoWindow, extra: str = "") -> str:
    """Directory name of a subset in the cache: source, placement window and symbols (custom URLs and files add a
    hash of their location and `extra`, e.g. a file's size and modification time)."""
    base = source.id
    if source.id == "custom":
        h = hashlib.sha256(f"{source.url}\n{extra}".encode()).hexdigest()[:12]
        base = f"{source.format}-{source.session_date}-{h}"
    return f"{base}_{window.tag()}_{'-'.join(symbols)}"


def subset_name(source: Source) -> str:
    return "subset.itch50.gz" if source.format == "itch" else "subset.pcap.gz"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def pinned_source(source: Source, symbols: list[str], window: DemoWindow, content_sha256: str, kept: int) -> str | None:
    """The id of the pinned configuration this subset content belongs to (same format, session date, symbols and
    window, same content sha256 and kept messages), or None."""
    for (sid, syms, tag), pin in PINS.items():
        src = SOURCES.get(sid)
        if src is None:
            continue
        if (src.format, src.session_date, syms, tag) != (
            source.format,
            source.session_date,
            tuple(symbols),
            window.tag(),
        ):
            continue
        if (content_sha256, kept) == pin:
            return sid
    return None


def cached(
    directory: Path, source: Source, symbols: list[str] | None = None, window: DemoWindow | None = None
) -> SubsetResult | None:
    """The cached subset in `directory` when its manifest (schema, schema version, filter version) is current, the
    file's size and sha256 match it, and, for a pinned configuration (`symbols` and `window` given), its content
    matches the pin."""
    man_p = directory / "manifest.json"
    sub_p = directory / subset_name(source)
    if not (man_p.is_file() and sub_p.is_file()):
        return None
    try:
        man = json.loads(man_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(man, dict):
        return None
    sub = man.get("subset") or {}
    flt = man.get("filter") or {}
    if (man.get("schema"), man.get("schema_version"), man.get("filter_version")) != (
        MANIFEST_SCHEMA,
        MANIFEST_VERSION,
        FILTER_VERSION,
    ):
        return None
    if sub.get("bytes") != sub_p.stat().st_size or _sha256_file(sub_p) != sub.get("sha256"):
        return None
    if symbols is not None and window is not None:
        pin = PINS.get((source.id, tuple(symbols), window.tag()))
        if pin is not None and (flt.get("content_sha256"), flt.get("kept_messages")) != pin:
            return None
    return SubsetResult(sub_p, man_p, man, True, 0, 0.0)


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _report(progress: Progress, stream: Stream, flt: Any, to_local: int, window: DemoWindow, t0: float) -> None:
    m = flt.market_ns
    progress(
        {
            "bytes": stream.bytes_read,
            "elapsed_s": time.perf_counter() - t0,
            "market_s": None if m is None else (m - to_local) / 1e9,
            "cut_s": window.cut_s,
            "kept": flt.kept,
        }
    )


def _check_disk(directory: Path, min_free_bytes: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < min_free_bytes:
        raise QtError(
            "QT-INPUT-001",
            f"free disk space {free / 2**30:.1f} GiB below {min_free_bytes / 2**30:.1f} GiB",
            "free disk space or pass --cache-dir on another volume",
        )


class _Producer(threading.Thread):
    """Reads the stream and inflates it into a bounded queue (zlib and socket reads release the GIL)."""

    def __init__(self, stream: Stream, inflater: Inflater, q: queue.Queue, stop: threading.Event) -> None:
        super().__init__(name="qt-demo-inflate", daemon=True)
        self.stream, self.inflater, self.q, self.stop_evt = stream, inflater, q, stop
        self.error: BaseException | None = None

    def _put(self, item: Any) -> bool:
        while not self.stop_evt.is_set():
            try:
                self.q.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def run(self) -> None:
        try:
            for raw in self.stream.chunks():
                if self.stop_evt.is_set():
                    break
                if not all(self._put(piece) for piece in self.inflater.pieces(raw)):
                    break
            else:
                tail = self.inflater.flush()
                if tail:
                    self._put(tail)
        except BaseException as e:
            if not self.stop_evt.is_set():  # an error after the consumer stopped is the early close itself
                self.error = e
        finally:
            self.stream.close()
            self._put(None)


def build_subset(
    source: Source,
    symbols: list[str],
    window: DemoWindow,
    directory: Path,
    *,
    max_download_bytes: int | None = None,
    min_free_bytes: int = 2 << 30,
    progress: Progress | None = None,
    progress_every_s: float = 1.0,
    stream: Stream | None = None,
) -> SubsetResult:
    """Streams the source's prefix, filters it and writes `<directory>/subset.*.gz` and `manifest.json`."""
    directory.mkdir(parents=True, exist_ok=True)
    _check_disk(directory, min_free_bytes)
    sub_p = directory / subset_name(source)
    man_p = directory / "manifest.json"
    part = sub_p.with_name(sub_p.name + ".part")
    stream = stream or Stream(source.url)
    inflater = Inflater()
    q: queue.Queue = queue.Queue(maxsize=4)  # little read-ahead: the stream stops soon after the cut
    stop = threading.Event()
    producer = _Producer(stream, inflater, q, stop)
    t0 = time.perf_counter()
    last_report = t0
    last_disk = t0
    try:
        with (
            open(part, "wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as gz,
        ):
            flt: ItchCutFilter | IexCutFilter
            if source.format == "itch":
                flt = ItchCutFilter(symbols, window.cut_ns_local, gz)
                to_local = 0
            else:
                flt = IexCutFilter(symbols, window.cut_ns_utc, gz)
                to_local = window.midnight_utc_ns
            producer.start()
            head = b""
            date_checked = source.format != "iex"
            while True:
                data = q.get()
                if data is None:
                    break
                if len(head) < 4:
                    head += data[: 4 - len(head)]
                    _check_format(source, head)
                try:
                    done = flt.feed(data)
                except CaptureError as e:
                    raise QtError("QT-INPUT-002", f"{source.url}: {e}", "is this an IEX HIST pcap file?") from None
                first_seen = getattr(flt, "first_seen_ns", None)
                if not date_checked and first_seen is not None:
                    date_checked = True
                    first = window.local_date(int(first_seen))
                    if first != source.session_date:
                        raise QtError(
                            "QT-INPUT-002",
                            f"{source.url}: the capture starts on {first} (US/Eastern), not on the session date "
                            f"{source.session_date}",
                            "pass the capture's own --date, or the file of that day",
                        )
                if done:
                    break
                now = time.perf_counter()
                if max_download_bytes is not None and stream.bytes_read > max_download_bytes:
                    raise QtError(
                        "QT-INPUT-002",
                        f"{source.url}: {stream.bytes_read:,} bytes read without reaching the cut "
                        f"{window.label(window.cut_s)} ET",
                        "check the file and --minutes, or raise --max-download-mb",
                    )
                if now - last_disk >= 2.0:
                    last_disk = now
                    _check_disk(directory, min_free_bytes)
                if progress is not None and now - last_report >= progress_every_s:
                    last_report = now
                    _report(progress, stream, flt, to_local, window, t0)
            stop.set()
            stream.close()
            producer.join(timeout=5)
            if progress is not None:
                _report(progress, stream, flt, to_local, window, t0)
            if producer.error is not None:
                err = producer.error
                if isinstance(err, QtError):
                    raise err
                raise QtError("QT-INPUT-001", f"{source.url}: {type(err).__name__}: {err}") from None
            stats = flt.finish()
        last = stats.get("last_ts_ns") if source.format == "itch" else stats.get("last_send_ns")
        if stats["cut_reached"]:
            stats["complete"] = "cut"
        elif last is not None and last - to_local >= window.complete_s * 1_000_000_000:
            stats["complete"] = "end_of_input"
        else:
            if head and sniff_format(head) is None:
                raise QtError(
                    "QT-INPUT-002",
                    f"{source.url}: the input is neither ITCH 5.0 framing nor a pcap / pcapng capture",
                    "check --input-format and the file",
                )
            at = "no message" if last is None else window.label(int((last - to_local) // 1_000_000_000))
            raise QtError(
                "QT-INPUT-002",
                f"{source.url}: the input ends at {at} ET, before the end of the last label "
                f"{window.label(window.complete_s)} ET (data cut {window.label(window.cut_s)} ET)",
                "use a full day file, a subset that covers the window, or a smaller --minutes",
            )
        if stats["missing_symbols"]:
            raise QtError(
                "QT-INPUT-002",
                f"{source.url}: no messages for {', '.join(stats['missing_symbols'])} before the cut",
                "check --symbols against the day's listed symbols",
            )
        pin = PINS.get((source.id, tuple(symbols), window.tag()))
        matched = pinned_source(source, symbols, window, stats["content_sha256"], stats["kept_messages"])
        if pin is not None and (stats["content_sha256"], stats["kept_messages"]) != pin:
            raise QtError(
                "QT-INPUT-002",
                f"{source.url}: subset content sha256 {stats['content_sha256'][:16]}... with "
                f"{stats['kept_messages']:,} "
                f"messages differs from the pinned {pin[0][:16]}... with {pin[1]:,} messages",
                "the public file may have changed; nothing was cached",
            )
        wall = time.perf_counter() - t0
        part.replace(sub_p)
    except BaseException:
        stop.set()
        stream.close()  # unblocks a producer waiting in a socket read before joining it (Ctrl-C stays prompt)
        part.unlink(missing_ok=True)
        if producer.is_alive():
            producer.join(timeout=5)
        raise
    finally:
        stream.close()
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_VERSION,
        "filter_version": FILTER_VERSION,
        "created": _now_iso(),
        "queuetruth_version": __version__,
        "source": {
            "id": source.id,
            "title": source.title,
            "format": source.format,
            "session_date": source.session_date,
            "location": source.url,
            "remote": is_url(source.url),
            "listed_bytes": source.listed_bytes,
            "sha256_full_file": source.sha256_full,
            "http_status": stream.http_status,
            "headers": stream.headers,
            "total_bytes": stream.total_bytes,
            "bytes_read": stream.bytes_read,
            "sha256_bytes_read": stream.sha256.hexdigest(),
            "resumptions": stream.resumptions,
            "decompressed_bytes": inflater.bytes_out,
            "input_encoding": inflater.mode,
            "attribution": source.attribution,
            "terms_url": source.terms_url,
        },
        "symbols": symbols,
        "window": {
            "session_date": window.session_date,
            "placement_start": window.label(window.start_s),
            "placement_end": window.label(window.end_s),
            "cut": window.label(window.cut_s),
            "cut_ns_after_midnight": window.cut_ns_local,
            "timezone": "America/New_York",
        },
        "filter": stats,
        "pinned": pin is not None or matched is not None,
        "pinned_source": source.id if pin is not None else matched,
        "subset": {"file": sub_p.name, "bytes": sub_p.stat().st_size, "sha256": _sha256_file(sub_p)},
        "timing": {
            "wall_s": round(wall, 2),
            "download_mb_per_s": round(stream.bytes_read / 1e6 / wall, 2) if wall > 0 else None,
        },
    }
    tmp = man_p.with_name(man_p.name + ".part")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, man_p)
    return SubsetResult(sub_p, man_p, manifest, False, stream.bytes_read, wall)


def _check_format(source: Source, head: bytes) -> None:
    got = sniff_format(head)
    if got is not None and got != source.format:
        what = {"itch": "ITCH 5.0 framing", "iex": "a pcap / pcapng capture"}
        raise QtError(
            "QT-INPUT-002",
            f"{source.url}: the input is {what[got]}, not {what[source.format]}",
            f"pass --input-format {got}",
        )


def fetch_subset(source: Source, symbols: list[str], window: DemoWindow, entry: Path, **kw: Any) -> SubsetResult:
    """`build_subset` into a fresh sibling of the cache entry, then swapped into `entry` (module docstring)."""
    entry.parent.mkdir(parents=True, exist_ok=True)
    tag = f"{os.getpid()}-{secrets.token_hex(4)}"
    tmp = entry.with_name(f".{entry.name}.build-{tag}")
    try:
        res = build_subset(source, symbols, window, tmp, **kw)
        old = entry.with_name(f".{entry.name}.old-{tag}")
        if entry.exists():
            entry.rename(old)
        try:
            tmp.rename(entry)
        except OSError:  # another run swapped its own build in first: use that one if it is good
            other = cached(entry, source, symbols, window)
            if other is None:
                if old.exists() and not entry.exists():  # put the previous entry back
                    old.rename(entry)
                raise
            return SubsetResult(
                other.path, other.manifest_path, other.manifest, False, res.bytes_downloaded, res.wall_s
            )
        finally:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return SubsetResult(
        entry / res.path.name, entry / res.manifest_path.name, res.manifest, False, res.bytes_downloaded, res.wall_s
    )


__all__ = [
    "FILTER_VERSION",
    "MANIFEST_SCHEMA",
    "PINS",
    "SubsetResult",
    "build_subset",
    "cache_key",
    "cached",
    "fetch_subset",
    "pinned_source",
    "subset_name",
]
