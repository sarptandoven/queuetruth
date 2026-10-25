"""Public order-level days `queuetruth demo` knows, and the demo window on them.

A source is one public day file: its URL, its format (`itch` = Nasdaq TotalView-ITCH 5.0 framing, `iex` = IEX HIST
DEEP+ pcapng), the session date, the full file's listed size and sha256 (the demo reads only a prefix, so these are
provenance, not a check), the default symbols, the licence notes and an estimate of the compressed bytes needed to
reach a market time (`checkpoints`, measured on 2026-10-24 by streaming each file from its start; the estimate is
only used to tell the user what a download will cost before it starts).

The demo window (`DemoWindow`): shadows are placed during `minutes` minutes starting `open_delay_s` after the
regular-session open (09:30 ET), and the data is cut `h_max_s + margin_s` after the end of that window, so every
shadow has `h_max_s` of exact replay behind it. Both venues start the day with an empty book, so a prefix of the day
file (from its first byte to the cut) is an exact order-level record of everything before the cut.
"""

from __future__ import annotations

import datetime as dt
import functools
import itertools
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from queuetruth.errors import QtError

SEC_NS = 1_000_000_000
OPEN_S = 9 * 3600 + 30 * 60

#: IEX's required citation (IEX Historical Data Terms of Use, clause 01): exact text plus the link.
IEX_ATTRIBUTION = (
    "Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data "
    "Terms of Use."
)
IEX_TERMS_URL = "https://iextrading.com/iex-historical-data-terms/"
NASDAQ_NOTE = (
    "Source: Nasdaq TotalView-ITCH 5.0 public sample file (emi.nasdaq.com). Nasdaq claims copyright in the data; "
    "the demo stores the filtered subset only in your cache and prints aggregate statistics."
)

FORMATS = ("itch", "iex")
# ITCH 5.0 message lengths without the 2-byte prefix (crates/ingest/src/itch/message.rs `message_len`).
ITCH50_LEN = {
    "S": 12, "R": 39, "H": 25, "Y": 20, "L": 26, "V": 35, "W": 12, "K": 28, "J": 35, "h": 21, "A": 36, "F": 40,
    "E": 31, "C": 36, "X": 23, "D": 19, "U": 35, "P": 44, "Q": 40, "B": 19, "I": 50, "N": 20, "O": 48,
}  # fmt: skip
_CAPTURE_MAGICS = (
    b"\x0a\x0d\x0d\x0a",
    b"\xd4\xc3\xb2\xa1",
    b"\xa1\xb2\xc3\xd4",
    b"\x4d\x3c\xb2\xa1",
    b"\xa1\xb2\x3c\x4d",
)


@functools.cache
def eastern() -> ZoneInfo:
    """America/New_York, loaded on first use; `QT-USAGE-001` with a hint when the tz database is missing."""
    try:
        return ZoneInfo("America/New_York")
    except (ZoneInfoNotFoundError, ValueError):
        raise QtError(
            "QT-USAGE-001",
            "demo: the time zone America/New_York is not available on this system",
            "pip install tzdata",
        ) from None


@dataclass(frozen=True)
class Source:
    """One public day file (module docstring)."""

    id: str
    title: str
    format: str
    session_date: str  # YYYY-MM-DD, the US/Eastern trading date
    url: str
    default_symbols: tuple[str, ...]
    listed_bytes: int | None = None
    sha256_full: str | None = None
    terms_url: str | None = None
    attribution: str | None = None
    note: str | None = None
    #: (seconds after local midnight, compressed bytes streamed from the file's start to reach that market time)
    checkpoints: tuple[tuple[int, int], ...] = field(default_factory=tuple)

    @property
    def host(self) -> str:
        m = re.match(r"^[a-z]+://([^/]+)", self.url)
        return m.group(1) if m else self.url

    def estimate_bytes(self, cut_s: float) -> int | None:
        """Compressed bytes needed to reach `cut_s` seconds after local midnight: linear between checkpoints; past
        the last one, the last segment's rate extrapolated and capped at the file's listed size (`extrapolated`
        says which); None without checkpoints."""
        pts = self.checkpoints
        if not pts:
            return None
        if cut_s > pts[-1][0]:
            (t0, b0), (t1, b1) = pts[-2] if len(pts) > 1 else (0, 0), pts[-1]
            est = int(b1 + (b1 - b0) * (cut_s - t1) / (t1 - t0))
            return min(est, self.listed_bytes) if self.listed_bytes else est
        if cut_s <= pts[0][0]:
            return pts[0][1]
        for (t0, b0), (t1, b1) in itertools.pairwise(pts):
            if t0 <= cut_s <= t1:
                return int(b0 + (b1 - b0) * (cut_s - t0) / (t1 - t0))
        return None  # pragma: no cover - the loop covers [pts[0], pts[-1]]

    def extrapolated(self, cut_s: float) -> bool:
        """True when `estimate_bytes(cut_s)` lies past the last measured checkpoint."""
        return bool(self.checkpoints) and cut_s > self.checkpoints[-1][0]


def _hm(h: int, m: int) -> int:
    return h * 3600 + m * 60


#: Measured with a prefix stream on 2026-10-24 (bytes at the first chunk whose last message reached the time).
_NASDAQ_20191230_CHECKPOINTS = tuple(
    (_hm(9, 30 + 5 * i), int(mb * 1e6)) for i, mb in enumerate((88.1, 166.7, 240.1, 307.2, 380.6, 453.0, 514.9, 580.9))
)
_IEX_20261009_CHECKPOINTS = tuple(
    (_hm(9, 30 + 5 * i), int(mb * 1e6))
    for i, mb in enumerate((24.1, 233.8, 547.4, 849.3, 1146.1, 1397.8, 1587.5, 1800.4))
)

SOURCES: dict[str, Source] = {
    s.id: s
    for s in (
        Source(
            id="nasdaq-2019-12-30",
            title="Nasdaq TotalView-ITCH 5.0",
            format="itch",
            session_date="2019-12-30",
            url="https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/12302019.NASDAQ_ITCH50.gz",
            default_symbols=("AAPL", "MSFT", "SPY", "TSLA"),
            listed_bytes=3_524_013_057,
            sha256_full="ef03df46a27e6bda4dead017f84c2e3979df7211f02c7868b51d53fceb99c689",
            terms_url="https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/",
            note=NASDAQ_NOTE,
            checkpoints=_NASDAQ_20191230_CHECKPOINTS,
        ),
        Source(
            id="iex-2026-10-09",
            title="IEX DEEP+ (IEX HIST)",
            format="iex",
            session_date="2026-10-09",
            url=(
                "https://www.googleapis.com/download/storage/v1/b/iex/o/data%2Ffeeds%2F20261009%2F"
                "20261009_IEXTP1_DPLS1.0.pcap.gz?generation=1791608815689771&alt=media"
            ),
            default_symbols=("SPY", "AAPL", "NVDA", "TSLA"),
            listed_bytes=9_383_598_955,
            sha256_full="0fc06989540e36de6dfb8455e6ccda20fda6161ea2800631cb6128bef16a93af",
            terms_url=IEX_TERMS_URL,
            attribution=IEX_ATTRIBUTION,
            checkpoints=_IEX_20261009_CHECKPOINTS,
        ),
    )
}
ALIASES = {"nasdaq": "nasdaq-2019-12-30", "iex": "iex-2026-10-09"}
DEFAULT_SOURCE = "nasdaq-2019-12-30"


def get_source(name: str) -> Source:
    """A source by id or alias (`nasdaq`, `iex`); `QT-USAGE-001` for an unknown name."""
    key = ALIASES.get(name, name)
    if key not in SOURCES:
        known = ", ".join([*SOURCES, *ALIASES])
        raise QtError("QT-USAGE-001", f"demo: unknown source {name!r}", f"choose one of: {known}")
    return SOURCES[key]


def custom_source(fmt: str, session_date: str, location: str, symbols: tuple[str, ...]) -> Source:
    """A source for `--url` / `--from-file` (no pinned values). IEX inputs carry IEX's attribution."""
    if fmt not in FORMATS:
        raise QtError("QT-USAGE-001", f"demo: unknown format {fmt!r}", "use --input-format itch or iex")
    parse_date(session_date)
    is_iex = fmt == "iex"
    return Source(
        id="custom",
        title="IEX DEEP+ (your input)" if is_iex else "TotalView-ITCH 5.0 (your input)",
        format=fmt,
        session_date=session_date,
        url=location,
        default_symbols=symbols,
        terms_url=IEX_TERMS_URL if is_iex else None,
        attribution=IEX_ATTRIBUTION if is_iex else None,
    )


def parse_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        raise QtError("QT-USAGE-001", f"demo: invalid date {s!r}", "use YYYY-MM-DD") from None


def file_name(location: str) -> str:
    """The last path segment of a URL or path, percent-decoded (IEX HIST URLs encode `/` as `%2F`); the query and
    the directories are never part of it."""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", location):
        path = urllib.parse.unquote(urllib.parse.urlsplit(location).path)
    else:
        path = location
    return re.split(r"[/\\]", path)[-1]


def infer_date(location: str) -> str | None:
    """The session date in a public file name: `MMDDYYYY.NASDAQ_ITCH50` or `MMDDYYYY.subset...` (Nasdaq) or
    `YYYYMMDD_IEXTP1_...` / `YYYYMMDD_DPLS...` (IEX); None when the name has neither. Only the file name counts."""
    name = file_name(location)
    m = re.match(r"^(\d{2})(\d{2})(\d{4})\.", name)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def infer_format(location: str) -> str | None:
    """`itch` or `iex` from a file name (`ITCH50`, `.itch50`, `pcap`, `DPLS`, `DEEP`), else None. Only the file
    name counts: a directory called `switch` says nothing about the file in it."""
    name = file_name(location).lower()
    if "itch" in name:
        return "itch"
    if "pcap" in name or "dpls" in name or "deep" in name:
        return "iex"
    return None


def sniff_format(head: bytes) -> str | None:
    """`iex` for a pcapng / pcap capture, `itch` for ITCH 5.0 framing (a zero high length byte, the length of a
    known message type, that type), None when the first decompressed bytes are neither (or fewer than 4)."""
    if len(head) < 4:
        return None
    if head[:4] in _CAPTURE_MAGICS:
        return "iex"
    if head[0] == 0 and ITCH50_LEN.get(chr(head[2])) == head[1]:
        return "itch"
    return None


def read_sidecar(path: Path) -> dict[str, Any] | None:
    """The `queuetruth.demo_subset` manifest next to a cached demo subset (`<dir>/manifest.json` naming this file),
    or None: it tells `--from-file` the format, session date and symbols of a subset the demo wrote itself."""
    man_p = path.parent / "manifest.json"
    try:
        man = json.loads(man_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(man, dict) or man.get("schema") != "queuetruth.demo_subset":
        return None
    if (man.get("subset") or {}).get("file") != path.name:
        return None
    return man


@dataclass(frozen=True)
class DemoWindow:
    """Where the shadows go and where the data is cut (module docstring)."""

    session_date: str
    minutes: int
    open_delay_s: int = 300
    h_max_s: int = 300
    margin_s: int = 10

    def __post_init__(self) -> None:
        if not 1 <= self.minutes <= 360:
            raise QtError("QT-USAGE-001", f"demo: --minutes {self.minutes} out of range", "use 1 to 360")

    @property
    def midnight_utc_ns(self) -> int:
        d = parse_date(self.session_date)
        return int(dt.datetime(d.year, d.month, d.day, tzinfo=eastern()).timestamp()) * SEC_NS

    def local_date(self, utc_ns: int) -> str:
        """The US/Eastern date of an epoch timestamp in nanoseconds (YYYY-MM-DD)."""
        return dt.datetime.fromtimestamp(utc_ns // SEC_NS, tz=eastern()).date().isoformat()

    @property
    def complete_s(self) -> int:
        """End of the last label: an input whose last message is at or after this time covers every shadow's
        `h_max` even when it ends before the cut (a subset the demo cut itself ends just before the cut)."""
        return self.end_s + self.h_max_s

    @property
    def start_s(self) -> int:
        """First placement time, seconds after local midnight."""
        return OPEN_S + self.open_delay_s

    @property
    def end_s(self) -> int:
        """End of the placement window (exclusive), seconds after local midnight."""
        return self.start_s + 60 * self.minutes

    @property
    def cut_s(self) -> int:
        """Data cut: the first message stamped at or after this time ends the subset."""
        return self.end_s + self.h_max_s + self.margin_s

    @property
    def cut_ns_local(self) -> int:
        """The cut in nanoseconds after local midnight (ITCH timestamps)."""
        return self.cut_s * SEC_NS

    @property
    def cut_ns_utc(self) -> int:
        """The cut in nanoseconds since the epoch (IEX send times)."""
        return self.midnight_utc_ns + self.cut_ns_local

    def label(self, seconds: int) -> str:
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"

    def tag(self) -> str:
        """Cache-key part: `0935-10m` (placement start and length)."""
        return f"{self.start_s // 3600:02d}{self.start_s % 3600 // 60:02d}-{self.minutes}m"


#: Published numbers on the same day for comparison (docs/RESULTS.md section 4, faithful mode, raw-bytes run):
#: NautilusTrader's default fill model on 3,000 shadows placed from 09:35 to 15:50 ET on 2019-12-30, and the exact
#: fill rate of those shadows.
REFERENCES: dict[str, dict[str, object]] = {
    "nasdaq-2019-12-30": {
        "label": "full session, 3,000 shadows, 30-minute block bootstrap (docs/RESULTS.md, section 4)",
        "bias": {"1000000000": 0.0313, "10000000000": 0.0330, "60000000000": 0.0143},
        "ci_lo": {"1000000000": 0.0263, "10000000000": 0.0247, "60000000000": 0.0101},
        "ci_hi": {"1000000000": 0.0364, "10000000000": 0.0414, "60000000000": 0.0192},
        # 331 / 1,453 / 2,250 of 3,000 shadows filled in the exact replay (docs/RESULTS.md, section 4)
        "exact_fill_rate": {"1000000000": 0.1103, "10000000000": 0.4843, "60000000000": 0.75},
    }
}
