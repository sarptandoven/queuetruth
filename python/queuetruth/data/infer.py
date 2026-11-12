"""What an order-level file is: its format, venue and session date, inferred so `queuetruth data add` needs no flags.

Format: from the file's first bytes (inflated when it is gzip or zstd): a pcap or pcapng capture is IEX DEEP+, `DBN`
and a version byte is Databento DBN, ITCH 5.0 framing is Nasdaq TotalView-ITCH; text files are told apart by their
first lines: a Tardis CSV header, the JSON lines of the crypto captures (Bitstamp, Bitfinex, Coinbase `full` and
`level3`, Kraken `level3`, Hyperliquid node), and NYSE TAQ Integrated Feed files by their product name
(`EQY_US_<market>_IBF_<channel>_<YYYYMMDD>`). A directory is a capture directory: its first data file decides.

Venue: from the format (ITCH: nasdaq, IEX: iex, NYSE: nyse, the crypto L3 captures: generic, which the engine
reads as crypto_l3; Coinbase `full`: coinbase; Tardis: generic, with the source venue from its `exchange` column),
and for DBN from the dataset code in the file's metadata (`XNAS.ITCH`: nasdaq, `GLBX.MDP3`: cme).

Session date: only ITCH needs one from outside the records (its timestamps count from midnight). It comes from the
file name (`MMDDYYYY.` or `YYYYMMDD_`), else from a `manifest.json` next to the file that names it (the demo's
cached subsets, the IEX split's buckets). Every other format dates each record itself.

The names are the `ingest` spec names (`iex_deep_plus`); `FORMATS` maps the CLI spellings (`iex-deep-plus`). This is
a local list until the source-format registry (`_core.formats()`) lands.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from queuetruth.demo.sources import _CAPTURE_MAGICS, infer_date, sniff_format
from queuetruth.errors import QtError

#: CLI spelling -> spec name of every format `ingest` reads.
FORMATS: dict[str, str] = {
    "itch": "itch",
    "dbn": "dbn",
    "etcsv": "etcsv",
    "parquet": "parquet",
    "bitstamp": "bitstamp",
    "bitfinex": "bitfinex",
    "coinbase": "coinbase",
    "coinbase-level3": "coinbase_level3",
    "kraken-l3": "kraken_l3",
    "hyperliquid-node": "hyperliquid_node",
    "tardis-l2": "tardis_l2",
    "iex-deep-plus": "iex_deep_plus",
    "nyse-ibf": "nyse_ibf",
}
#: Plain words for each format (the summary's "how inferred" lines).
TITLES: dict[str, str] = {
    "itch": "Nasdaq TotalView-ITCH 5.0",
    "dbn": "Databento DBN",
    "etcsv": "et CSV",
    "parquet": "queuetruth Parquet",
    "bitstamp": "Bitstamp capture",
    "bitfinex": "Bitfinex capture",
    "coinbase": "Coinbase full channel capture",
    "coinbase_level3": "Coinbase level3 capture",
    "kraken_l3": "Kraken level3 capture",
    "hyperliquid_node": "Hyperliquid node output",
    "tardis_l2": "Tardis incremental_book_L2 CSV",
    "iex_deep_plus": "IEX DEEP+",
    "nyse_ibf": "NYSE TAQ Integrated Feed",
}
#: The venue (rule set) of each format when the format alone decides it.
VENUE_OF: dict[str, str] = {
    "itch": "nasdaq",
    "iex_deep_plus": "iex",
    "nyse_ibf": "nyse",
    "bitstamp": "generic",
    "bitfinex": "generic",
    "coinbase_level3": "generic",
    "kraken_l3": "generic",
    "hyperliquid_node": "generic",
    "coinbase": "coinbase",
    "tardis_l2": "generic",
}
#: Databento dataset code -> venue rule set.
DBN_VENUES: dict[str, str] = {"XNAS.ITCH": "nasdaq", "GLBX.MDP3": "cme"}
#: Formats whose records carry no date (the session date must come from outside the file).
NEEDS_DATE = frozenset({"itch"})

HEAD_BYTES = 1 << 16
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
NYSE_NAME = re.compile(r"^EQY_US_[A-Z]+_IBF_\d+_(\d{8})", re.IGNORECASE)
TARDIS_HEADER = "exchange,symbol,timestamp,local_timestamp"
BITSTAMP_CHANNELS = ("live_orders_", "live_trades_", "detail_order_book_", "order_book_", "diff_order_book_")
COINBASE_TYPES = frozenset(
    {"received", "open", "done", "match", "change", "activate", "subscriptions", "heartbeat", "l2update", "snapshot"}
)
COINBASE_L3_FRAMES = frozenset({"open", "change", "match", "done", "noop", "heartbeat"})


@dataclass
class Detected:
    """One input: what it is and how that was decided (`how`: format, venue and date, each in plain words)."""

    path: Path
    format: str
    venue: str | None
    source_venue: str | None = None
    date: str | None = None
    how: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": self.format,
            "venue": self.venue,
            "source_venue": self.source_venue,
            "date": self.date,
            "how": dict(self.how),
        }


def spec_format(name: str) -> str:
    """The spec name of a format given in either spelling (`iex-deep-plus` or `iex_deep_plus`); `QT-USAGE-001`
    for an unknown one."""
    key = name.strip().lower().replace("_", "-")
    if key in FORMATS:
        return FORMATS[key]
    raise QtError("QT-USAGE-001", f"data add: unknown format {name!r}", f"choose one of: {', '.join(FORMATS)}")


def head(path: Path, n: int = HEAD_BYTES) -> bytes:
    """The first `n` bytes of a file's content: inflated when it is gzip or zstd (b'' when it cannot be read)."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            f.seek(0)
            if magic[:2] == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=f) as g:
                    return _read_upto(g, n)
            if magic == ZSTD_MAGIC:
                import pyarrow as pa

                with pa.CompressedInputStream(pa.OSFile(str(path)), "zstd") as z:
                    return _read_upto(z, n)
            return f.read(n)
    except (OSError, EOFError, ValueError):
        return b""


def _read_upto(f: Any, n: int) -> bytes:
    out = b""
    try:
        while len(out) < n:
            b = f.read(n - len(out))
            if not b:
                break
            out += b
    except (OSError, EOFError, ValueError):  # a truncated stream: what was read so far
        pass
    return out


def _lines(data: bytes, limit: int = 50) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if len(data) >= HEAD_BYTES and len(lines) > 1:
        lines = lines[:-1]  # the last line may be cut by the head
    return [ln.strip() for ln in lines[:limit] if ln.strip()]


def json_format(obj: Any) -> str | None:
    """The crypto capture format of one parsed JSON line, or None (module docstring)."""
    if isinstance(obj, dict):
        if "chan" in obj and "msg" in obj:
            return "bitfinex"
        if obj.keys() & {"block_number", "block_time", "raw_book_diff"} or {"coin", "oid"} <= obj.keys():
            return "hyperliquid_node"
        if "book" in obj and "product_id" in obj:
            return "coinbase_level3"
        inner = obj.get("msg", obj)
        if isinstance(inner, list):
            return "coinbase_level3" if inner and inner[0] in COINBASE_L3_FRAMES else None
        if not isinstance(inner, dict):
            return None
        if inner.get("type") == "level3":
            return "coinbase_level3"
        result = inner.get("result")
        if inner.get("channel") == "level3" or (isinstance(result, dict) and result.get("channel") == "level3"):
            return "kraken_l3"
        channel = inner.get("channel")
        if "event" in inner and isinstance(channel, str) and channel.startswith(BITSTAMP_CHANNELS):
            return "bitstamp"
        if inner.get("type") in COINBASE_TYPES and ("product_id" in inner or inner.get("type") == "subscriptions"):
            return "coinbase"
    if isinstance(obj, list) and len(obj) == 2 and isinstance(obj[1], dict) and "oid" in obj[1]:
        return "hyperliquid_node"
    return None


def sniff(data: bytes, name: str) -> tuple[str, str] | None:
    """(format, how) from a file's first bytes and its name, or None."""
    if data[:4] in _CAPTURE_MAGICS:
        return "iex_deep_plus", "a pcap / pcapng capture"
    if data[:3] == b"DBN" and len(data) > 3 and 0 < data[3] < 16:
        return "dbn", f"DBN version {data[3]} header"
    if sniff_format(data) == "itch":
        return "itch", "ITCH 5.0 framing"
    if NYSE_NAME.match(name):
        return "nyse_ibf", "TAQ Integrated Feed file name"
    lines = _lines(data)
    if lines and lines[0].lower().replace(" ", "").startswith(TARDIS_HEADER):
        return "tardis_l2", "Tardis CSV header"
    for ln in lines:
        if not ln.startswith(("{", "[")):
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        fmt = json_format(obj)
        if fmt is not None:
            return fmt, "its JSON lines"
    return None


def _data_files(d: Path) -> list[Path]:
    return [
        p
        for p in sorted(d.rglob("*"))
        if p.is_file() and not p.name.startswith(".") and p.name not in ("capture_meta.json", "manifest.json")
    ]


def dbn_dataset(data: bytes) -> str | None:
    """The Databento dataset code in a DBN header (16 bytes after the 8-byte prelude), or None."""
    if data[:3] != b"DBN" or len(data) < 24:
        return None
    code = data[8:24].split(b"\0", 1)[0].decode("ascii", errors="replace").strip()
    return code or None


def tardis_exchange(data: bytes) -> str | None:
    """The `exchange` of the first data row of a Tardis CSV, or None."""
    lines = _lines(data, limit=3)
    if len(lines) < 2:
        return None
    ex = lines[1].split(",", 1)[0].strip()
    return ex or None


def sidecar_date(path: Path) -> str | None:
    """The session date a `manifest.json` next to `path` gives for it: a demo subset manifest naming this file, an
    IEX split manifest, or any manifest with a top-level or `source` `session_date`; None otherwise."""
    try:
        man = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(man, dict):
        return None
    sub = man.get("subset")
    if isinstance(sub, dict) and sub.get("file") not in (None, path.name):
        return None  # a demo manifest about another file
    for d in (man, man.get("source"), man.get("window")):
        if isinstance(d, dict) and isinstance(d.get("session_date"), str):
            v = d["session_date"]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                return v
    return None


def detect(
    path: str | Path, *, format: str | None = None, venue: str | None = None, date: str | None = None
) -> Detected:
    """What `path` is (module docstring). Given values win; `QT-USAGE-001` names what could not be inferred and why;
    `QT-INPUT-001` for a missing path."""
    p = Path(path)
    if not p.exists():
        raise QtError("QT-INPUT-001", f"data add: {p}: no such file or directory", "check the path")
    how: dict[str, str] = {}
    probe = p
    if p.is_dir():
        files = _data_files(p)
        if not files:
            raise QtError("QT-INPUT-002", f"data add: {p} is an empty directory", "pass the capture's files")
        probe = files[0]
    data = head(probe)
    if format:
        fmt = spec_format(format)
        how["format"] = "given"
    else:
        found = sniff(data, probe.name)
        if found is None:
            raise QtError(
                "QT-USAGE-001",
                f"data add: cannot tell the format of {p.name}: its first bytes are none of the formats queuetruth "
                "reads (ITCH 5.0, IEX DEEP+ pcap, DBN, NYSE TAQ Integrated Feed, Tardis CSV, crypto JSON captures)",
                f"pass --input-format ({', '.join(FORMATS)})",
            )
        fmt, why = found
        how["format"] = why
    source_venue = None
    if venue:
        how["venue"] = "given"
    elif fmt == "dbn":
        code = dbn_dataset(data)
        venue = DBN_VENUES.get(code or "")
        if venue is None:
            raise QtError(
                "QT-USAGE-001",
                f"data add: cannot tell the venue of {p.name}: its DBN dataset is "
                f"{code or 'not readable from the header'}, which has no rule set of its own",
                "pass --venue (nasdaq, cme or generic)",
            )
        how["venue"] = f"DBN dataset {code}"
    elif fmt in VENUE_OF:
        venue = VENUE_OF[fmt]
        how["venue"] = "the format"
    else:
        raise QtError(
            "QT-USAGE-001",
            f"data add: format {fmt} does not say which venue's rules apply to {p.name}",
            "pass --venue (nasdaq, cme, coinbase, crypto_l3, iex, nyse or generic)",
        )
    if fmt == "tardis_l2":
        source_venue = tardis_exchange(data)
    if date:
        how["date"] = "given"
    elif fmt in NEEDS_DATE:
        date = infer_date(p.name)
        if date:
            how["date"] = "file name"
        else:
            date = sidecar_date(p)
            if date:
                how["date"] = "manifest.json next to it"
        if date is None:
            raise QtError(
                "QT-USAGE-001",
                f"data add: cannot tell the session date of {p.name}: ITCH timestamps count from midnight, the "
                "name has no MMDDYYYY date and no manifest.json next to it names one",
                "pass --date YYYY-MM-DD (the US/Eastern trading date of the file)",
            )
    else:
        m = NYSE_NAME.match(p.name) if fmt == "nyse_ibf" else None
        how["date"] = "the file name" if m else "the records"
    return Detected(p, fmt, venue, source_venue, date, how)


__all__ = [
    "DBN_VENUES",
    "FORMATS",
    "NEEDS_DATE",
    "TITLES",
    "VENUE_OF",
    "Detected",
    "detect",
    "head",
    "json_format",
    "sidecar_date",
    "sniff",
    "spec_format",
]
