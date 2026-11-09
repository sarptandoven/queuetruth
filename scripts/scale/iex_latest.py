#!/usr/bin/env python3
"""The newest IEX DEEP+ day IEX has published, for the scheduled atlas (`.github/workflows/atlas.yml`).

    uv run --frozen --no-sync python scripts/scale/iex_latest.py [--date YYYY-MM-DD] [--today YYYY-MM-DD]

IEX publishes each session's DEEP+ capture the next morning (UTC) at a fixed public address,
`https://storage.googleapis.com/iex/data/feeds/<YYYYMMDD>/<YYYYMMDD>_IEXTP1_DPLS1.0.pcap.gz`; the bucket cannot be
listed anonymously, so the newest day is found by asking for each weekday's file, newest first, from the day before
`--today` (default: the current UTC date) back `--lookback` days (holidays have no file). With `--date`, that day
only. Prints `date=`, `url=` and `bytes=` lines (the `$GITHUB_OUTPUT` format); exit 1 when no file is found.

With `--count N` (the backfill, `.github/workflows/atlas-backfill.yml`) it finds the N newest published sessions
instead and prints `days=` (a JSON list of `{"date", "url", "bytes"}`, newest first, for a job matrix) and `n=`.
`--kept FILE` (asset names, one per line, as `release_assets.sh names` prints them) leaves out the sessions whose
`day-<date>.tar.gz` is already kept, unless `--rebuild`; they still count towards N. Using the data means accepting
IEX's Historical Data Terms of Use (`docs/data-and-licensing.md`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable

BASE = "https://storage.googleapis.com/iex/data/feeds"


def url_of(day: dt.date) -> str:
    d = day.strftime("%Y%m%d")
    return f"{BASE}/{d}/{d}_IEXTP1_DPLS1.0.pcap.gz"


def head(url: str, timeout: float = 30.0) -> int | None:
    """Content length of `url`, or None when it does not exist (HTTP 404 or 403)."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "queuetruth-atlas"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


def candidates(today: dt.date, lookback: int) -> list[dt.date]:
    """Weekdays before `today`, newest first, within `lookback` days."""
    days = (today - dt.timedelta(days=i) for i in range(1, lookback + 1))
    return [d for d in days if d.weekday() < 5]


def find(
    today: dt.date, lookback: int, date: dt.date | None = None, probe: Callable[[str], int | None] | None = None
) -> tuple[dt.date, str, int] | None:
    """`(date, url, bytes)` of the newest published day (or of `date`), else None; `probe` defaults to `head`."""
    probe = probe or head
    for d in [date] if date else candidates(today, lookback):
        size = probe(url_of(d))
        if size:
            return d, url_of(d), size
    return None


def newest(
    today: dt.date, count: int, lookback: int, probe: Callable[[str], int | None] | None = None
) -> list[tuple[dt.date, str, int]]:
    """The `count` newest published days before `today` (newest first, at most `lookback` days back)."""
    probe = probe or head
    out: list[tuple[dt.date, str, int]] = []
    for d in candidates(today, lookback):
        if len(out) == count:
            break
        size = probe(url_of(d))
        if size:
            out.append((d, url_of(d), size))
    return out


def backfill(found: list[tuple[dt.date, str, int]], kept: set[str], rebuild: bool) -> list[dict[str, object]]:
    """The matrix rows of the days to run: every found day, less those already kept (unless `rebuild`)."""
    return [
        {"date": d.isoformat(), "url": url, "bytes": size}
        for d, url, size in found
        if rebuild or f"day-{d.isoformat()}.tar.gz" not in kept
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", type=dt.date.fromisoformat, help="this session date only (YYYY-MM-DD)")
    ap.add_argument("--today", type=dt.date.fromisoformat, help="search back from this date (default: UTC today)")
    ap.add_argument("--lookback", type=int, help="calendar days to search back (default 10; with --count, 2N + 14)")
    ap.add_argument("--count", type=int, help="the N newest published sessions, as a job matrix (the backfill)")
    ap.add_argument("--kept", type=argparse.FileType("r"), help="with --count: kept asset names, one per line")
    ap.add_argument("--rebuild", action="store_true", help="with --count: run kept days again")
    a = ap.parse_args(argv)
    today = a.today or dt.datetime.now(dt.UTC).date()
    if a.count is not None:
        if a.date is not None or not 1 <= a.count <= 60:
            ap.error("--count takes 1 to 60 sessions and no --date")
        lookback = a.lookback or 2 * a.count + 14
        found = newest(today, a.count, lookback)
        if len(found) < a.count:
            print(f"iex_latest: {len(found)} of {a.count} sessions found in {lookback} days", file=sys.stderr)
        kept = {line.strip() for line in a.kept} if a.kept else set()
        rows = backfill(found, kept, a.rebuild)
        print(f"days={json.dumps(rows, separators=(',', ':'))}\nn={len(rows)}")
        return 0 if found else 1
    if a.kept is not None or a.rebuild:
        ap.error("--kept and --rebuild go with --count")
    a.lookback = a.lookback or 10
    found = find(today, a.lookback, a.date)
    if found is None:
        what = a.date.isoformat() if a.date else f"the {a.lookback} days before {today}"
        print(f"iex_latest: no IEX DEEP+ file for {what}", file=sys.stderr)
        return 1
    d, url, size = found
    print(f"date={d.isoformat()}\nurl={url}\nbytes={size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
