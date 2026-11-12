"""Which backtester wrote a file: read from the file's own columns or keys, never from its name alone.

Built-in backtesters and what marks their output (the importers in `queuetruth.live.importers` hold the full
formats; the rules live in `queuetruth.backtesters._builtin`, and a plugin brings its own `detect`):

- `hftbacktest`: order snapshots with `price_tick`, `tick_size` and `leaves_qty`.
- `nautilus`: report or event rows with `client_order_id`, `ts_init`, `last_qty` or `ts_event`; or an order event
  file named after the event (`OrderFilled.csv`).
- `backtrader`: `QtOrderLog` lines (`"event": "order"` or `"trade"`), or a `Transactions` analyzer dump (an object
  of bar times to lists).
- `vectorbt`: `records_readable` columns (`Order Id` and `Column`, or a trade id column).
- `lean`: a result JSON with an `orders` member, or order events (`orderEventId`, `algorithmId`, `OrderId`).
- `freqtrade`: a backtest result (`.zip`, or JSON with `strategy` and its trades) or a trades database.
"""

from __future__ import annotations

from pathlib import Path

from queuetruth import backtesters as _bt
from queuetruth.errors import QtError

#: Backtesters `check` reads (the `queuetruth.backtesters` registry entries with a `detect`). Lists the built-ins;
#: `name in BACKTESTERS` also holds for a plugin registered through the `queuetruth.backtesters` entry points.
BACKTESTERS: tuple[str, ...] = _bt.Names(check=True)


def detect(path: str | Path) -> str | None:
    """The backtester that wrote `path`, or None when the file does not say (`queuetruth.backtesters.detect`)."""
    return _bt.detect(path)


def detect_all(paths: list[str]) -> str:
    """The one backtester of all `paths` (`QT-USAGE-001` when a file does not say or two files disagree)."""
    kinds: dict[str, list[str]] = {}
    unknown: list[str] = []
    for p in paths:
        k = detect(p)
        if k is None:
            unknown.append(p)
        else:
            kinds.setdefault(k, []).append(p)
    if len(kinds) > 1:
        seen = "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(kinds.items()))
        raise QtError(
            "QT-USAGE-001",
            f"check: the files look like output of different backtesters ({seen})",
            "check one backtest at a time, or name it with --backtester",
        )
    if unknown and not kinds:
        raise QtError(
            "QT-USAGE-001",
            f"check: cannot tell which backtester wrote {', '.join(unknown)} (no column or key of a known format)",
            f"name it with --backtester ({', '.join(_bt.names(check=True))})",
        )
    return next(iter(kinds))
