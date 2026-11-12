"""Train and test windows of `queuetruth fit` in plain terms: `--split 70%` (the first 70% of the data in time) or
`--test-after HH:MM` (venue-local time), turned into the `WindowSpec` the calibrations already take.

Both are computed from the orders the fit uses (shadows, or the live orders) and the dataset's sessions:

- `split` with two or more session dates: the first share of the dates train (at least one), the rest test.
- `split` with one date: the cut is that share of the time between the first and the last order. It becomes a block
  window of the continuous session (blocks of a whole number of seconds, so the cut is exact), which is what
  `calibrate` and `calibrate-live` select by. Blocks of `--train blocks:I-J/N` are fifths of the 6.5 h session,
  not of the data, so a short capture had no shadow in the test blocks; these are cut where the data are.
- `test_after`: the same block window on every date, cut at that venue-local time.

Expert windows (`--train` / `--test`: dates or `blocks:I-J/N`) are parsed by `window_arg` and passed through.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from queuetruth.errors import QtError
from queuetruth.schemas.specs import WindowSpec
from queuetruth.score._dataset import DatasetView

NS = 1_000_000_000
#: Block lengths (seconds) tried for a `--split` on one date, longest first.
BLOCK_SECONDS = (60, 30, 10, 5, 1)
#: Fewest blocks the data should span before a coarser block length is used.
MIN_BLOCKS = 20


@dataclass(frozen=True)
class Order:
    """One order the fit uses: instrument and entry time (ns UTC)."""

    instrument_id: int | None
    ts: int


@dataclass(frozen=True)
class Split:
    """Train and test windows with what they hold, in words."""

    train: WindowSpec
    test: WindowSpec
    #: How the split was chosen, e.g. `the first 70% of the data in time`.
    how: str
    #: The windows in words, e.g. `2026-06-15` or `before 14:42:00`.
    train_text: str
    test_text: str
    #: Orders in each window (as this module assigns them).
    n_train: int
    n_test: int


def parse_share(text: str) -> float:
    """`70%`, `70` or `0.7` as a share in (0, 1)."""
    t = str(text).strip()
    try:
        v = float(t.removesuffix("%"))
    except ValueError as e:
        raise QtError("QT-USAGE-001", f"--split: expected a share such as 70%, got {text!r}") from e
    if t.endswith("%") or v > 1:
        v /= 100.0
    if not 0.0 < v < 1.0:
        raise QtError("QT-USAGE-001", f"--split: the train share must be between 0% and 100%, got {text!r}")
    return v


def parse_clock(text: str) -> int:
    """`09:42` or `09:42:30` as seconds after midnight."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59 or int(m.group(3) or 0) > 59:
        raise QtError("QT-USAGE-001", f"--test-after: expected a venue-local time HH:MM or HH:MM:SS, got {text!r}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)


def clock(seconds: int) -> str:
    """Seconds after midnight as `HH:MM:SS`."""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def window_arg(text: str, flag: str) -> WindowSpec:
    """`--train` / `--test`: comma-separated dates `YYYY-MM-DD`, or `blocks:I-J/N` (as `calibrate`)."""
    t = str(text).strip()
    if t.startswith("blocks:"):
        sel, sep, n = t.removeprefix("blocks:").partition("/")
        try:
            n_blocks = int(n)
            index: list[int] = []
            for part in [p for p in sel.split(",") if p.strip()]:
                a, dash, b = part.partition("-")
                index.extend(range(int(a), int(b) + 1) if dash else [int(a)])
        except ValueError as e:
            raise QtError("QT-USAGE-001", f"{flag}: expected blocks:I-J/N, got {text!r}") from e
        if not sep or not index or n_blocks < 1 or any(i < 0 or i >= n_blocks for i in index):
            raise QtError("QT-USAGE-001", f"{flag}: expected blocks:I-J/N with indices in 0..N-1, got {text!r}")
        return WindowSpec.model_validate({"blocks": {"index": sorted(set(index)), "n_blocks": n_blocks}})
    dates = [d.strip() for d in t.split(",") if d.strip()]
    if not dates or not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in dates):
        raise QtError("QT-USAGE-001", f"{flag}: expected dates YYYY-MM-DD[,...] or blocks:I-J/N, got {text!r}")
    return WindowSpec.model_validate({"dates": dates})


def _iso(day: int) -> str:
    return str(np.datetime64(int(day), "D"))


def _sessions(ds: DatasetView, orders: Sequence[Order]) -> dict[tuple[int, int], tuple[int, int, int]]:
    """(instrument_id, session date) -> (midnight ns, open s, close s) of every order's instrument-day."""
    out: dict[tuple[int, int], tuple[int, int, int]] = {}
    for o in orders:
        day, midnight = ds.day(o.instrument_id, o.ts)
        rows = ds._inst.get(int(o.instrument_id)) if o.instrument_id is not None else None
        row = next((r for r in rows or [] if int(r.get("midnight_utc_ns") or 0) == midnight), None)
        if row is None or row.get("open_s") is None or row.get("close_s") is None:
            raise QtError(
                "QT-DATA-001",
                "a time split needs the instruments' trading session (open_s, close_s in instruments.parquet); "
                f"instrument {o.instrument_id} on {_iso(day)} has none",
                "split by dates with --train DATES --test DATES",
            )
        out[(int(o.instrument_id), day)] = (midnight, int(row["open_s"]), int(row["close_s"]))  # type: ignore[arg-type]
    return out


def _one_session(sessions: dict[tuple[int, int], tuple[int, int, int]]) -> tuple[int, int]:
    """The common (open s, close s) of every instrument-day, or `QT-CONFIG-001`."""
    kinds = {(o, c) for (_m, o, c) in sessions.values()}
    if len(kinds) != 1:
        texts = ", ".join(f"{clock(o)} to {clock(c)}" for o, c in sorted(kinds))
        raise QtError(
            "QT-CONFIG-001",
            f"a time split needs one trading session for every instrument and day; this data has {texts}",
            "fit one session at a time, or split by dates with --train DATES --test DATES",
        )
    return next(iter(kinds))


def _block_windows(
    ds: DatasetView, orders: Sequence[Order], n_blocks: int, k: int
) -> tuple[WindowSpec, WindowSpec, int, int]:
    blocks = [ds.block_of(o.instrument_id, o.ts, n_blocks) for o in orders]
    train = sorted({b for b in blocks if b < k})
    test = sorted({b for b in blocks if b >= k})
    n_train = sum(1 for b in blocks if b < k)
    return (
        WindowSpec.model_validate({"blocks": {"index": train, "n_blocks": n_blocks}}),
        WindowSpec.model_validate({"blocks": {"index": test, "n_blocks": n_blocks}}),
        n_train,
        len(blocks) - n_train,
    )


def plan(
    ds: DatasetView,
    orders: Sequence[Order],
    *,
    split: str | float | None = None,
    test_after: str | None = None,
    what: str = "orders",
) -> Split:
    """The train and test windows of `split` (a share, default 70%) or `test_after` (venue-local `HH:MM`)."""
    if split is not None and test_after is not None:
        raise QtError("QT-USAGE-001", "give --split or --test-after, not both")
    if not orders:
        raise QtError("QT-DATA-001", f"no {what} to fit on")
    if test_after is not None:
        return _after(ds, orders, parse_clock(test_after))
    share = parse_share(f"{split}") if split is not None else 0.7
    pct = f"{share * 100:.0f}%"
    days = sorted({ds.day(o.instrument_id, o.ts)[0] for o in orders})
    if len(days) >= 2:
        k = min(max(round(share * len(days)), 1), len(days) - 1)
        tr, te = days[:k], days[k:]
        n_train = sum(1 for o in orders if ds.day(o.instrument_id, o.ts)[0] in set(tr))
        return Split(
            WindowSpec(dates=[_iso(d) for d in tr]),
            WindowSpec(dates=[_iso(d) for d in te]),
            f"the first {pct} of the {len(days)} days",
            ", ".join(_iso(d) for d in tr),
            ", ".join(_iso(d) for d in te),
            n_train,
            len(orders) - n_train,
        )
    sessions = _sessions(ds, orders)
    open_s, close_s = _one_session(sessions)
    midnight = next(iter(sessions.values()))[0]
    length = max(close_s - open_s, 1)
    t0, t1 = min(o.ts for o in orders), max(o.ts for o in orders)
    span = max((t1 - t0) / NS, 1.0)
    sec = next((b for b in BLOCK_SECONDS if length % b == 0 and span / b >= MIN_BLOCKS), 1)
    n_blocks = length // sec
    cut_ns = t0 + share * (t1 - t0)
    k = min(max(round((cut_ns - midnight - open_s * NS) / (sec * NS)), 1), n_blocks - 1)
    tr, te, n_tr, n_te = _block_windows(ds, orders, n_blocks, k)
    at = clock(open_s + k * sec)
    return Split(tr, te, f"the first {pct} of the data in time", f"before {at}", f"from {at}", n_tr, n_te)


def _after(ds: DatasetView, orders: Sequence[Order], cut_s: int) -> Split:
    open_s, close_s = _one_session(_sessions(ds, orders))
    if not open_s < cut_s < close_s:
        raise QtError(
            "QT-USAGE-001",
            f"--test-after {clock(cut_s)} is outside the trading session {clock(open_s)} to {clock(close_s)} "
            "(venue-local time)",
        )
    sec = math.gcd(cut_s - open_s, close_s - open_s)
    n_blocks = (close_s - open_s) // sec
    tr, te, n_tr, n_te = _block_windows(ds, orders, n_blocks, (cut_s - open_s) // sec)
    at = clock(cut_s)
    return Split(tr, te, f"held out after {at} venue time", f"before {at}", f"from {at}", n_tr, n_te)


def inside(ds: DatasetView, o: Order, w: WindowSpec) -> bool:
    """The order belongs to the window (its session date; its block of the continuous session, as `qt_calib`)."""
    if w.dates is not None and _iso(ds.day(o.instrument_id, o.ts)[0]) not in set(w.dates):
        return False
    if w.blocks is not None:
        return ds.block_of(o.instrument_id, o.ts, w.blocks.n_blocks) in set(w.blocks.index)
    return True


def given(ds: DatasetView, orders: Sequence[Order], train: WindowSpec, test: WindowSpec) -> Split:
    """Expert windows (`--train` / `--test`) as given, with what they hold."""

    def text(w: WindowSpec) -> str:
        from queuetruth.export._text import window_text
        from queuetruth.schemas.models import WindowEcho

        return window_text(WindowEcho.model_validate(w.model_dump(mode="json")))

    n_tr = sum(1 for o in orders if inside(ds, o, train))
    n_te = sum(1 for o in orders if inside(ds, o, test) and not inside(ds, o, train))
    return Split(train, test, "the windows given", text(train), text(test), n_tr, n_te)


__all__ = ["Order", "Split", "clock", "given", "inside", "parse_clock", "parse_share", "plan", "window_arg"]
