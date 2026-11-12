"""What `compare` was given: the backtest's backtester and the live run's kind, read from the files.

The backtest is read through the Backtester registry (`queuetruth.backtesters.detect`), with one refinement the
registry leaves to its callers: an SQLite file is a Freqtrade trades database when it has the tables `trades` and
`orders`, and a Hummingbot database when it has `Order` and `TradeFill`. A Hummingbot trades CSV is told by its
header.

The live run is one of three kinds:

- `canonical`: a canonical live-log directory (`manifest.json`, written by `queuetruth live import`);
- `bot`: a trading bot's own records (Freqtrade trades database and log, Hummingbot database or trades CSV);
- `venue`: a venue or broker export (`queuetruth live import --venue V`), told by the record shapes below.

Every guess can be overridden (`--backtester`, `--venue`). When a file says nothing, the error names what to pass.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from queuetruth import backtesters
from queuetruth.errors import QtError
from queuetruth.live import importers as _importers

#: The built-in venue importers and trading bots of the importer registry (`queuetruth.live.importers`), in its list
#: order: the `--venue` choices (fixed, so the generated grammar does not change with the plugins installed).
#: `detect_live` also accepts a plugin's venue or bot.
VENUES: tuple[str, ...] = _importers.names("venue", builtin=True)
BOTS: tuple[str, ...] = _importers.names("bot", builtin=True)
_HEAD = 64 * 1024
_FT_LOG = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]\d+ - freqtrade\.", re.M)
_HB_CSV = ("exchange_trade_id", "config_file_path")
#: (venue, patterns any of which marks its records), tried in order; the first match wins.
_VENUE_MARKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fix44", ("8=FIX.4", "8=FIXT.1")),
    ("ibkr_flex", ("<FlexQueryResponse", "<FlexStatement", "IBExecID", "ibExecID")),
    ("binance", ('"executionReport"', '"order.place"', '"isBuyer"')),
    ("kraken", ('"channel":"executions"', '"channel": "executions"', '"exec_type"')),
    ("hyperliquid", ('"userFills"', '"orderUpdates"', '"historicalOrders"', '"cloid"')),
    ("deribit", ("user.orders.", "user.trades.", '"instrument_name"')),
    ("okx", ('"instId"', '"ordId"')),
    ("bybit", ('"orderLinkId"', '"execType"', '"orderStatus"')),
    ("coinbase", ('"cumulative_quantity"', '"product_id"')),
)


@dataclass(frozen=True)
class LiveKind:
    """The live run: `kind` is `canonical`, `bot` or `venue`; `name` the bot or venue (None for a canonical log);
    `detected` is false when a flag said so."""

    kind: str
    name: str | None
    detected: bool = True

    def describe(self) -> str:
        if self.kind == "canonical":
            return "a canonical live log"
        if self.kind == "bot":
            return f"a {self.name} bot's own records"
        return f"a {self.name} export"


def _sqlite_tables(path: Path) -> set[str]:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
        finally:
            con.close()
    except sqlite3.Error:
        return set()


def _head(path: Path) -> bytes:
    try:
        with path.open("rb") as f:
            return f.read(_HEAD)
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e}") from e


def bot_of(path: Path) -> str | None:
    """`freqtrade` or `hummingbot` when `path` is that bot's database, log or trades CSV, else None."""
    if path.is_dir():
        return None
    head = _head(path)
    if head.startswith(b"SQLite format 3\x00"):
        tables = _sqlite_tables(path)
        if {"trades", "orders"} <= tables:
            return "freqtrade"
        if {"Order", "TradeFill"} <= tables:
            return "hummingbot"
        return None
    text = head.decode("utf-8", errors="replace")
    if _FT_LOG.search(text):
        return "freqtrade"
    first = text.split("\n", 1)[0]
    if all(c in first for c in _HB_CSV):
        return "hummingbot"
    return None


def venue_of(path: Path) -> str | None:
    """The venue whose export `path` is, read from its records, or None."""
    if path.is_dir():
        return "fix44" if any(path.glob("*.messages*.log")) or any(path.glob("*.body")) else None
    text = _head(path).decode("utf-8", errors="replace")
    for venue, marks in _VENUE_MARKS:
        if any(m in text for m in marks):
            return venue
    return None


def detect_backtest(paths: Sequence[str | Path], backtester: str | None = None) -> str:
    """The backtester that wrote `paths` (`backtester` when given; `QT-CONFIG-001` when it is not registered).
    `QT-INPUT-002` when no file says, naming the choices."""
    if backtester is not None:
        backtesters.get(backtester)
        return backtester
    for p in paths:
        path = Path(p)
        if not path.is_file():
            continue
        bot = bot_of(path)
        if bot is not None:
            return bot
        name = backtesters.detect(path)
        if name is not None:
            return name
    raise QtError(
        "QT-INPUT-002",
        f"compare: cannot tell which backtester wrote {', '.join(str(p) for p in paths)}",
        f"pass --backtester NAME (one of {', '.join(backtesters.names())})",
    )


def detect_live(paths: Sequence[str | Path], venue: str | None = None) -> LiveKind:
    """The kind of live run `paths` are (`venue` when given: a bot name or a venue)."""
    ps = [Path(p) for p in paths]
    for p in ps:
        if not p.exists():
            raise QtError("QT-INPUT-001", f"compare: {p}: no such file or directory")
    if venue is not None:
        v = venue.replace("-", "_").lower()
        for kind in ("bot", "venue"):
            if v in _importers.names(kind):
                return LiveKind(kind, v, detected=False)
        raise QtError(
            "QT-USAGE-001",
            f"compare: --venue {venue}: not a venue or bot queuetruth reads",
            f"one of {', '.join((*_importers.names('venue'), *_importers.names('bot')))}",
        )
    if len(ps) == 1 and ps[0].is_dir() and (ps[0] / "manifest.json").is_file():
        return LiveKind("canonical", None)
    if any(p.is_dir() and (p / "manifest.json").is_file() for p in ps):
        raise QtError("QT-USAGE-001", "compare: a canonical live log is compared on its own; pass one --live DIR")
    kinds = set()
    for p in ps:  # each file is a bot's record, a venue's record or neither
        bot = bot_of(p)
        venue_name = None if bot is not None else venue_of(p)
        if bot is not None:
            kinds.add(("bot", bot))
        elif venue_name is not None:
            kinds.add(("venue", venue_name))
    if len(kinds) == 1:
        kind, name = kinds.pop()
        return LiveKind(kind, name)
    bots = {n for k, n in kinds if k == "bot"}
    venues = {n for k, n in kinds if k == "venue"}
    what = ", ".join(str(p) for p in ps)
    if len(venues | bots) > 1:
        raise QtError(
            "QT-INPUT-002",
            f"compare: the live files {what} look like records of different sources "
            f"({', '.join(sorted(venues | bots))})",
            "compare one live run at a time, or pass --venue V to read them all as one venue's export",
        )
    raise QtError(
        "QT-INPUT-002",
        f"compare: cannot tell what kind of live run {what} is",
        "pass a canonical live-log directory (queuetruth live import), a bot's database or log, or --venue V "
        f"(one of {', '.join((*VENUES, *BOTS))})",
    )


__all__ = ["BOTS", "VENUES", "LiveKind", "bot_of", "detect_backtest", "detect_live", "venue_of"]
