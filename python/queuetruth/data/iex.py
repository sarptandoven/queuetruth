"""`queuetruth data fetch iex`: one free IEX DEEP+ day, only the symbols asked for, as a dataset.

    qt.data.fetch_iex("2026-10-09", ["AAPL", "MSFT"], out="iex-aapl", yes=True)

The day's file (`<YYYYMMDD>_IEXTP1_DPLS1.0.pcap.gz` in IEX's public bucket, several GB) is streamed once and never
stored: the demo's resumable HTTP stream (`queuetruth.demo.fetch.Stream`, byte-range resumption checked against the
first response's validator) feeds the gzip inflater and the Rust IEX split engine (`queuetruth._core.IexSplit`, the
engine of `scripts/scale/iex_split.py`, here with one bucket), which keeps only the requested symbols' messages,
renumbered so the source's sequence gaps are kept. That subset, a few MB to a few hundred MB, is written to a
staging directory next to `out`, ingested with `queuetruth.data.add` (which runs the data-quality checks) and
deleted. IEX's required citation and its terms link go into the dataset manifest (`attribution`).

Consent: nothing is fetched (not even a HEAD request) unless `yes` is set or `confirm(plan)` returns True, as for
`queuetruth demo`. Checks after the pass: the capture was read to its end, its first segment is on the requested
US/Eastern date, and every requested symbol has messages; otherwise `QT-INPUT-002` and nothing is written.
"""

from __future__ import annotations

import datetime as dt
import gzip
import secrets
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from queuetruth.data.add import DataResult, add
from queuetruth.errors import QtError

IEX_BASE = "https://storage.googleapis.com/iex/data/feeds"
SOURCE_TITLE = "IEX DEEP+ (IEX HIST)"
#: Where IEX lists the published days (the hint when a day has no file).
IEX_LISTING = "https://iextrading.com/trading/market-data/"
MIN_FREE_BYTES = 2 << 30

Progress = Callable[[dict[str, Any]], None]


def iex_url(day: dt.date, base: str = IEX_BASE) -> str:
    """The public DEEP+ file of a day: `<base>/<YYYYMMDD>/<YYYYMMDD>_IEXTP1_DPLS1.0.pcap.gz`."""
    d = day.strftime("%Y%m%d")
    return f"{base.rstrip('/')}/{d}/{d}_IEXTP1_DPLS1.0.pcap.gz"


@dataclass(frozen=True)
class FetchPlan:
    """What `fetch_iex` would do (no I/O): the day, symbols, URL and output dataset."""

    date: str
    symbols: tuple[str, ...]
    url: str
    out: str
    attribution: str
    terms_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "qt.data_fetch_plan",
            "schema_version": 1,
            "source": SOURCE_TITLE,
            "date": self.date,
            "symbols": list(self.symbols),
            "url": self.url,
            "out": self.out,
            "attribution": self.attribution,
            "terms_url": self.terms_url,
        }


def _symbols(raw: Sequence[str] | str | None) -> tuple[str, ...]:
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    syms = [s.strip().upper() for s in items if s.strip()]
    if not syms:
        raise QtError("QT-USAGE-001", "data fetch iex: needs --symbols", "for example --symbols AAPL,MSFT")
    bad = [s for s in syms if not (1 <= len(s) <= 8 and s.isascii() and s.replace(".", "").replace("+", "").isalnum())]
    if bad:
        raise QtError("QT-USAGE-001", f"data fetch iex: invalid symbol(s) {', '.join(bad)}", "use tickers like AAPL")
    return tuple(dict.fromkeys(syms))


def plan_fetch(
    date: str | dt.date, symbols: Sequence[str] | str, out: str | Path | None = None, *, url: str | None = None
) -> FetchPlan:
    """The plan of a fetch (module docstring); `QT-USAGE-001` for a bad date or symbol, or a day in the future."""
    from queuetruth.demo.sources import IEX_ATTRIBUTION, IEX_TERMS_URL

    try:
        day = date if isinstance(date, dt.date) else dt.date.fromisoformat(str(date))
    except ValueError:
        raise QtError("QT-USAGE-001", f"data fetch iex: invalid date {date!r}", "use YYYY-MM-DD") from None
    if day > dt.date.today():
        raise QtError(
            "QT-USAGE-001",
            f"data fetch iex: {day} is in the future",
            "choose a past trading day; IEX publishes each day's file after the close",
        )
    syms = _symbols(symbols)
    return FetchPlan(
        date=day.isoformat(),
        symbols=syms,
        url=url or iex_url(day),
        out=str(out) if out is not None else f"iex-{day.isoformat()}",
        attribution=IEX_ATTRIBUTION,
        terms_url=IEX_TERMS_URL,
    )


def plan_text(pl: FetchPlan) -> str:
    """The plan in plain words (what the consent prompt shows)."""
    return "\n".join(
        [
            f"source: {SOURCE_TITLE}, {pl.date} ({pl.url})",
            "download: the whole day's file, usually several GB, streamed once and never stored",
            f"kept: the messages of {', '.join(pl.symbols)}, ingested into {pl.out}",
            f"{pl.attribution} {pl.terms_url}",
        ]
    )


def _not_published(pl: FetchPlan, e: QtError) -> QtError:
    if "HTTP 404" in e.message or "HTTP 403" in e.message:
        return QtError(
            "QT-INPUT-001",
            f"data fetch iex: IEX has no DEEP+ file for {pl.date} ({e.message.split(': ', 1)[-1]})",
            f"choose a trading day IEX has published (listed at {IEX_LISTING}); weekends and holidays have none",
        )
    return e


def _stream_subset(pl: FetchPlan, dest: Path, progress: Progress | None, every_s: float = 1.0) -> dict[str, Any]:
    """Streams the day, keeps `pl.symbols` and writes the classic-pcap subset to `dest` (gzip); returns the stream's
    and the engine's numbers (module docstring)."""
    from queuetruth import _core
    from queuetruth.demo.fetch import Inflater, Stream
    from queuetruth.demo.iex import SYSTEM_EVENT_PATTERN, PcapWriter
    from queuetruth.demo.sources import DemoWindow

    day = dt.date.fromisoformat(pl.date)
    midnight = DemoWindow(pl.date, 1).midnight_utc_ns
    syms = sorted(pl.symbols)
    patterns = [*(s.ljust(8).encode() for s in syms), SYSTEM_EVENT_PATTERN]
    engine = _core.IexSplit(1, midnight, syms, patterns)
    stream = Stream(pl.url, retries=10, backoff_s=2.0)
    inflater = Inflater()
    t0 = time.perf_counter()
    last = t0
    kept = 0

    def report() -> None:
        if progress is None:
            return
        st = engine.stats()
        ns = st.get("last_send_ns")
        progress(
            {
                "bytes": stream.bytes_read,
                "total": stream.total_bytes,
                "elapsed_s": time.perf_counter() - t0,
                "market_s": None if ns is None else (int(ns) - midnight) / 1e9,
                "kept": kept,
            }
        )

    def take(piece: bytes) -> None:
        nonlocal kept
        try:
            parts = engine.feed(piece)
        except _core.QtCoreError as e:
            raise QtError("QT-INPUT-002", f"{pl.url}: {e}", "is this an IEX DEEP+ pcap file?") from None
        for _bucket, records in parts:
            writer._write(records)

    try:
        with (
            open(dest, "wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as gz,
        ):
            writer = PcapWriter(gz)
            for chunk in stream.chunks():
                for piece in inflater.pieces(chunk):
                    take(piece)
                now = time.perf_counter()
                if now - last >= every_s:
                    last = now
                    kept = int(engine.stats()["bucket_messages"][0])
                    report()
            tail = inflater.flush()
            if tail:
                take(tail)
    except QtError as e:
        raise _not_published(pl, e) from None
    finally:
        stream.close()
    st = engine.stats()
    kept = int(st["bucket_messages"][0])
    report()
    first = st.get("first_send_ns")
    first_day = None
    if first is not None:
        from queuetruth.demo.sources import eastern

        first_day = dt.datetime.fromtimestamp(int(first) / 1e9, tz=eastern()).date()
    if st.get("pending"):
        raise QtError(
            "QT-INPUT-002",
            f"{pl.url}: the capture ends inside a packet ({st['pending']} bytes left over)",
            "the file is cut short; rerun the fetch",
        )
    if first_day is None:
        raise QtError("QT-INPUT-002", f"{pl.url}: no IEX-TP segment in the capture", "is this an IEX DEEP+ file?")
    if first_day != day:
        raise QtError(
            "QT-INPUT-002",
            f"{pl.url}: the capture starts on {first_day} (US/Eastern), not on {day}",
            "pass the URL of that day's file",
        )
    seen = {row[0] for row in st["symbols"]}
    missing = [s for s in syms if s not in seen]
    if missing:
        raise QtError(
            "QT-INPUT-002",
            f"data fetch iex: no messages for {', '.join(missing)} on {pl.date}",
            "check --symbols against the symbols IEX listed that day",
        )
    return {
        "url": pl.url,
        "bytes_read": stream.bytes_read,
        "total_bytes": stream.total_bytes,
        "sha256_bytes_read": stream.sha256.hexdigest(),
        "resumptions": stream.resumptions,
        "decompressed_bytes": inflater.bytes_out,
        "messages": int(st["counters"].get("messages", 0)),
        "kept_messages": kept,
        "sequence_gaps": int(st["counters"].get("sequence_gaps", 0)),
        "subset_bytes": dest.stat().st_size,
        "wall_s": round(time.perf_counter() - t0, 2),
    }


def fetch_iex(
    date: str | dt.date,
    symbols: Sequence[str] | str,
    out: str | Path | None = None,
    *,
    yes: bool = False,
    confirm: Callable[[FetchPlan], bool] | None = None,
    progress: Progress | None = None,
    say: Callable[[str], None] | None = None,
    url: str | None = None,
    min_free_bytes: int = MIN_FREE_BYTES,
) -> DataResult:
    """Fetches one IEX DEEP+ day's `symbols` into the dataset `out` (default `iex-<date>`; an existing dataset gains
    the day). Downloads only with `yes` or when `confirm(plan)` returns True, else `QT-USAGE-001`. `url` streams
    another location (a mirror, or a local test server) instead of IEX's bucket."""
    from queuetruth.demo.sources import IEX_ATTRIBUTION, IEX_TERMS_URL

    note = say or (lambda _s: None)
    pl = plan_fetch(date, symbols, out, url=url)
    dest_ds = Path(pl.out)
    if (
        dest_ds.exists()
        and not (dest_ds / "manifest.json").is_file()
        and (not dest_ds.is_dir() or any(dest_ds.iterdir()))
    ):
        raise QtError(
            "QT-USAGE-001",
            f"data fetch iex: --out {dest_ds} exists and is not a dataset",
            "choose a new directory, or an existing dataset to add the day to",
        )
    if not (yes or (confirm is not None and confirm(pl))):
        raise QtError(
            "QT-USAGE-001", "data fetch iex: download not confirmed", "pass --yes to download without a prompt"
        )
    dest_ds.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(dest_ds.parent).free < min_free_bytes:
        raise QtError(
            "QT-INPUT-001",
            f"data fetch iex: less than {min_free_bytes / 2**30:.1f} GiB free under {dest_ds.parent}",
            "free some disk space or choose another --out",
        )
    stage = dest_ds.parent / f".{dest_ds.name}.qt-fetch-{secrets.token_hex(4)}"
    stage.mkdir()
    try:
        d = pl.date.replace("-", "")
        subset = stage / f"{d}_IEXTP1_DPLS1.0.{'-'.join(sorted(pl.symbols))}.pcap.gz"
        note(f"fetch {pl.url} (streamed; keeping {', '.join(pl.symbols)})")
        fetched = _stream_subset(pl, subset, progress)
        res = add(
            [subset],
            dest_ds,
            format="iex_deep_plus",
            venue="iex",
            attribution=[{"source": SOURCE_TITLE, "text": IEX_ATTRIBUTION, "terms_url": IEX_TERMS_URL, "url": pl.url}],
            say=say,
        )
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    res.doc["fetch"] = fetched
    return res


__all__ = ["IEX_BASE", "FetchPlan", "fetch_iex", "iex_url", "plan_fetch", "plan_text"]
