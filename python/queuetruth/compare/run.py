"""`compare`: a backtest against the live run of the same strategy, order by order.

Steps (each one is existing machinery; this module only picks and joins them):

1. Read what was given (`queuetruth.compare.detect`): the backtester through the Backtester registry, and the live
   run's kind (a canonical live log, a bot's own records, or a venue export).
2. Pick the path:
   - `reconcile` for the trading bots and the bar backtesters (Freqtrade, Hummingbot, Backtrader, VectorBT, LEAN
     and plugins with a reconciler): the backtester's reconciler pairs every order with its live twin and says why
     each pair differs, then scores the two logs (`reconcile --kind`). A Backtrader, VectorBT or LEAN backtest
     needs a canonical live log; a venue export or a bot's records are imported first (`live import`).
   - `score` for the event-driven simulators (hftbacktest, NautilusTrader and plugins without a reconciler): the
     backtest's output becomes the scorer's simulator arm (`live import --venue sim-*`, `score --sim-log`). Its
     orders are linked to the live orders by their keys (the backtest ran on the live schedule).
3. With `data` (an order-level dataset of the same days), the scorer also marks every fill to the dataset's mid
   (markouts, the P&L gap per order) and replays the live orders in the exact queue (the `exact` arm).
4. Restate everything in the user's words, answer first (`CompareResult`).

Instrument maps: none is needed to pair and score two logs of the same venue. With `data` the live instruments are
mapped to the dataset's symbols (`queuetruth.check.symbols`); only a name that maps to none needs `instrument_map`,
and the error names it.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import shutil
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar

from queuetruth import backtesters, units
from queuetruth.compare.detect import BOTS, LiveKind, detect_backtest, detect_live
from queuetruth.errors import QtError
from queuetruth.results import Result
from queuetruth.score.lines import pct as _pct

SCHEMA = "qt.compare"
#: The markout horizons of the answer (100 ms, 1 s, 10 s), as `score` prints them.
HORIZONS_NS = (100_000_000, 1_000_000_000, 10_000_000_000)
#: Subdirectories `compare` writes (cleared when `out` holds an earlier comparison).
_OUTPUTS = ("live", "sim", "score", "exact", "backtest", "live-import", "score-data")
_OUT_FILES = ("compare.json", "orders.csv", "compare.html", "instrument_map.csv", "reconcile.json")
_OUT_FILES += ("reconcile_orders.csv", "backtest_instrument_map.csv")

#: Plain-word reasons of the reconcile categories for a fill only one side made.
REASONS = {
    "missed_fill": "the live order rested and did not fill",
    "no_live_twin": "no live order to pair it with (none was placed live)",
    "extra_live_fill": "the backtest order did not fill",
    "no_sim_twin": "no backtest order to pair it with (the backtest did not place it)",
    "partial_fill": "filled in both, for different quantities",
    "sim_only": "the live order did not fill within the window",
    "live_only": "the backtest order did not fill within the window",
    "missing_in_backtest": "no backtest order carries this live order's key",
    "missing_in_live": "no live order carries this backtest order's key",
}


@dataclass(repr=False)
class CompareResult(Result):
    """The answer of `compare`: `summary` is the first answer line, `doc` the full `qt.compare` document, `orders`
    one row per order in human units, `out` the output directory. The shared result shape
    (`queuetruth.results.Result`): `table()`, `interval`, `to_html()`, `save(path)` and a compact notebook view."""

    title: ClassVar[str] = "queuetruth compare"

    doc: dict[str, Any]
    orders: list[dict[str, Any]] = field(default_factory=list)
    out: Path | None = None

    @property
    def summary(self) -> str:
        """The answer in one plain sentence."""
        return str(self.doc["answer"])

    @property
    def interval(self) -> str | None:
        """The share of the backtest's fills that also filled live, with its 95% interval, in words."""
        f = self.doc.get("fills") or {}
        if f.get("share") is None or f.get("ci_lo") is None:
            return None
        return (
            f"Backtest fills that also filled live: {_pct(f['share'])} (95% CI {_pct(f['ci_lo'])} to "
            f"{_pct(f['ci_hi'])})."
        )

    def _rows(self) -> list[dict[str, Any]]:
        return self.orders

    def text(self, *, max_rows: int = 10) -> str:
        """The terminal report: the answer lines first, then the first orders that differ."""
        from queuetruth.compare.render import text

        return text(self.doc, self.orders, max_rows=max_rows)

    def to_html(self) -> str:
        """A self-contained HTML report."""
        from queuetruth.compare.render import html

        return html(self.doc, self.orders)

    def __str__(self) -> str:
        return self.summary


# ---- small helpers ----------------------------------------------------------------------------------------------


def _paths(x: str | Path | Sequence[str | Path] | None, what: str) -> list[str]:
    items = [] if x is None else [x] if isinstance(x, str | Path) else list(x)
    if not items:
        raise QtError("QT-USAGE-001", f"compare: no {what} given", f"pass --{what} PATH")
    for p in items:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"compare: {p}: no such file or directory")
    return [str(p) for p in items]


def _ns(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise QtError("QT-USAGE-001", f"compare: window must be a duration such as 30s or 1h, got {v!r}")
    try:
        n = units.parse_duration(v, what="window", bare_ns=isinstance(v, int))
    except units.DurationError as e:
        raise QtError("QT-USAGE-001", f"compare: {e}", "use a duration such as 30s, 1min or 1h") from None
    if n <= 0:
        raise QtError("QT-USAGE-001", f"compare: window must be positive, got {v!r}")
    return n


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _fixed(v: int | None, exp: int | None) -> str | None:
    """A fixed-point size or quantity as a decimal (`queuetruth.units.format_scaled`); None stays None."""
    return None if v is None else units.format_scaled(int(v), exp)


def _price(v: int | None, exp: int | None) -> str | None:
    """A fixed-point price as a decimal (`queuetruth.units.format_price`); None stays None."""
    return None if v is None else units.format_price(int(v), exp)


def _epoch_ns(v: Any) -> int | None:
    """Epoch nanoseconds of a time given as an integer, a timestamp, a datetime or RFC 3339 text."""
    if v is None or v == "":
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if hasattr(v, "value") and isinstance(v.value, int):  # pandas.Timestamp keeps nanoseconds
        return int(v.value)
    if isinstance(v, dt.datetime):
        t = v if v.tzinfo else v.replace(tzinfo=dt.UTC)
    else:
        try:
            t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
        t = t if t.tzinfo else t.replace(tzinfo=dt.UTC)
    return int(t.timestamp()) * 1_000_000_000 + t.microsecond * 1000


def _local(v: Any, tz: str) -> str | None:
    """A time as venue-local wall time (`queuetruth.units.local_time`; the zone is named in the `tz` column)."""
    ns = _epoch_ns(v)
    if ns is None:
        return None if v in (None, "") else str(v)
    return units.local_time(ns, tz, named=False)


def _wilson(k: int, n: int) -> tuple[float | None, float | None]:
    """The 95% Wilson interval (`queuetruth.stats.wilson`, exact 0 and 1 edges); no interval without orders."""
    if n <= 0:
        return None, None
    from queuetruth.stats import wilson

    ci = wilson(k, n, edges="exact")
    assert ci is not None
    return float(ci[0]), float(ci[1])


def _ms(v: Any) -> str:
    """A measured latency, three significant digits (`queuetruth.units.format_duration`)."""
    return "n/a" if v is None else units.format_duration(float(v), sig=3)


def _prepare_out(out: str | Path | None) -> Path:
    if out is None:
        return Path(tempfile.mkdtemp(prefix="qt-compare-"))
    p = Path(out)
    if p.exists() and not p.is_dir():
        raise QtError("QT-USAGE-001", f"compare: --out {p} is a file")
    if (p / "compare.json").is_file():  # an earlier comparison: replace what it wrote, nothing else
        for d in _OUTPUTS:
            shutil.rmtree(p / d, ignore_errors=True)
        for f in _OUT_FILES:
            (p / f).unlink(missing_ok=True)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- instrument maps --------------------------------------------------------------------------------------------


def _auto_map(path: Path, rows: Sequence[tuple[str, str]]) -> Path:
    """An instrument map giving each (venue, instrument) an id 1..n with the default scales (price_exp -9, size_exp
    -8): enough to schedule and score two logs of the same venue against each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["venue", "instrument", "instrument_id", "session_date", "price_exp", "size_exp", "tick_size",
                    "lot_size"])  # fmt: skip
        for i, (venue, inst) in enumerate(sorted(set(rows)), start=1):
            w.writerow([venue, inst, i, "", -9, -8, "0.000000001", "0.00000001"])
    return path


def _data_map(path: Path, rows: Sequence[tuple[str, str]], data: str) -> tuple[Path, list[str]]:
    """The instrument map of the live instruments onto the dataset's symbols (`QT-INPUT-002` naming every name that
    maps to none)."""
    from queuetruth.check.symbols import dataset_symbols, match, write_map

    syms = dataset_symbols(data)
    by_venue: dict[str, list[str]] = {}
    for venue, inst in rows:
        by_venue.setdefault(venue, []).append(inst)
    notes: list[str] = []
    unmapped: dict[str, str] = {}
    lines: list[list[str]] = []
    for venue, names in sorted(by_venue.items()):
        m = match(names, syms)
        notes += m.notes
        unmapped.update({f"{venue}/{k}": v for k, v in m.unmapped.items()})
        tmp = path.with_suffix(f".{venue}.csv")
        write_map(tmp, venue, m.mapped, syms)
        with tmp.open() as f:
            lines += list(csv.reader(f))[1:]
        tmp.unlink()
    if unmapped:
        raise QtError(
            "QT-INPUT-002",
            "compare: live instrument(s) with no symbol in the dataset: "
            + "; ".join(f"{k} {v}" for k, v in sorted(unmapped.items())),
            "pass --instrument-map FILE with one row per instrument (columns venue,instrument,instrument_id,"
            "session_date,price_exp,size_exp,tick_size,lot_size), the instrument_id being the dataset's",
        )
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["venue", "instrument", "instrument_id", "session_date", "price_exp", "size_exp", "tick_size",
                    "lot_size"])  # fmt: skip
        w.writerows(lines)
    return path, notes


def _keys(log_dir: Path) -> set[str]:
    from queuetruth.live import read_log, table_rows

    orders, _ = read_log(str(log_dir))
    return {str(o["order_key"]) for o in table_rows(orders)}


def _live_map(path: Path, live_dir: Path) -> Path:
    """The instrument map the live log was imported with (its ids and units per venue and instrument), so the
    backtest's prices and quantities are read in the same units."""
    from queuetruth.live import read_log, table_rows

    orders, _ = read_log(str(live_dir))
    seen: dict[tuple[str, str], tuple[int, int, int]] = {}
    for o in table_rows(orders):
        if o.get("instrument_id") is not None:
            seen.setdefault(
                (str(o["venue"]), str(o["instrument"])),
                (int(o["instrument_id"]), int(o.get("price_exp") or -9), int(o.get("size_exp") or 0)),
            )
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["venue", "instrument", "instrument_id", "session_date", "price_exp", "size_exp", "tick_size",
                    "lot_size"])  # fmt: skip
        for (venue, inst), (iid, pe, se) in sorted(seen.items()):
            w.writerow([venue, inst, iid, "", pe, se, _fixed(1, pe), _fixed(1, se)])
    return path


def _log_instruments(live_dir: Path) -> list[tuple[str, str]]:
    from queuetruth.live import read_log, table_rows

    orders, _ = read_log(str(live_dir))
    return sorted({(str(o["venue"]), str(o["instrument"])) for o in table_rows(orders)})


# ---- the live arm as a canonical log ----------------------------------------------------------------------------


def _import(lk: LiveKind, paths: list[str], out: Path, imap: Path | None, account: str | None) -> Any:
    from queuetruth import live as qlive

    if out.exists():
        shutil.rmtree(out)
    # one call for every live arm: `live import` picks the venue's or the bot's importer from the importer registry
    spec: dict[str, Any] = {"venue": lk.name, "inputs": [{"path": p} for p in paths], "out": str(out)}
    if account or lk.kind == "bot":
        spec["account"] = account or "main"  # the reconcilers' default account for a bot's run
    if imap is not None:
        spec["instrument_map"] = {"path": str(imap)}
    return qlive.import_venue(spec)


def canonical_live(
    lk: LiveKind,
    paths: list[str],
    out: Path,
    *,
    instrument_map: str | None,
    data: str | None,
    account: str | None,
    need_ids: bool,
) -> tuple[Path, list[str]]:
    """The live run as a canonical live-log directory: as given, or imported into `<out>/live-import` with the
    user's instrument map, a map onto the dataset's symbols (with `data`) or, when `need_ids`, ids 1..n."""
    if lk.kind == "canonical":
        live_dir = Path(paths[0])
        if need_ids:
            _require_ids(live_dir)
        return live_dir, []
    dest = out / "live-import"
    notes: list[str] = []
    if instrument_map is not None:
        s = _import(lk, paths, dest, Path(instrument_map), account)
        return dest, [f"live: {w}" for w in s.warnings]
    if data is None and not need_ids:  # the reconcilers take instrument ids and scales from the log itself
        s = _quiet_import(lk, paths, dest, account)
        return dest, [f"live: {w}" for w in s.warnings if not w.startswith("unmapped_instruments:")]
    _quiet_import(lk, paths, dest, account)  # the first pass only learns the instrument names
    names = _log_instruments(dest)
    if data is not None:
        imap, notes = _data_map(out / "instrument_map.csv", names, data)
    else:
        imap = _auto_map(out / "instrument_map.csv", names)
        notes.append("instrument_map: none given; live instruments numbered 1..n with default scales")
    s = _import(lk, paths, dest, imap, account)
    return dest, notes + [f"live: {w}" for w in s.warnings]


def _quiet_import(lk: LiveKind, paths: list[str], out: Path, account: str | None) -> Any:
    """An import without an instrument map, without the unmapped-instrument warning (expected here)."""
    import warnings

    from queuetruth.live import LiveImportWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LiveImportWarning)
        return _import(lk, paths, out, None, account)


def _require_ids(live_dir: Path) -> None:
    from queuetruth.live import read_log, table_rows

    orders, _ = read_log(str(live_dir))
    missing = Counter(str(o["instrument"]) for o in table_rows(orders) if o.get("instrument_id") is None)
    if missing:
        names = ", ".join(f"{k} ({v} orders)" for k, v in sorted(missing.items()))
        raise QtError(
            "QT-INPUT-002",
            f"compare: the live log {live_dir} has orders without an instrument id: {names}",
            "it was imported without an instrument map: pass the venue export itself with --live (compare imports "
            "it), or reimport it with queuetruth live import --instrument-map FILE",
        )


# ---- the two paths ----------------------------------------------------------------------------------------------


def _score(live_dir: Path, sim: dict[str, Any], out: Path, window_ns: int | None, data: str | None) -> Any:
    from queuetruth import api

    spec: dict[str, Any] = {"live": {"path": str(live_dir)}, "sim": sim, "out": str(out)}
    if window_ns is not None:
        spec["window_ns"] = int(window_ns)
    if data is not None:
        spec["dataset"] = {"path": str(data)}
    return api.score(spec).model_dump(mode="json", by_alias=True)


def _summary(path: Path) -> dict[str, Any] | None:
    p = path / "summary.json"
    return json.loads(p.read_text()) if p.is_file() else None


@dataclass
class _Run:
    path: str
    live_dir: Path
    sim_dir: Path
    score: dict[str, Any] | None
    score_dir: Path | None
    data_score: dict[str, Any] | None = None
    data_score_dir: Path | None = None
    exact: dict[str, Any] | None = None
    report: Any = None
    notes: list[str] = field(default_factory=list)


def _reconcile_path(
    b: Any,
    bpaths: list[str],
    lk: LiveKind,
    lpaths: list[str],
    out: Path,
    *,
    data: str | None,
    instrument_map: str | None,
    window_ns: int | None,
    account: str | None,
    options: dict[str, Any],
) -> _Run:
    notes: list[str] = []
    if b.name in BOTS:
        if lk.kind != "bot" or lk.name != b.name:
            raise QtError(
                "QT-INPUT-002",
                f"compare: a {b.label} backtest is compared with the {b.label} bot's own live records, and the live "
                f"run here is {lk.describe()}",
                "pass the bot's trades database and log with --live (Freqtrade: tradesv3.sqlite and freqtrade.log; "
                "Hummingbot: the database or the trades CSV)",
            )
        live_arg: list[str] = lpaths
    else:
        live_dir, notes = canonical_live(
            lk, lpaths, out, instrument_map=instrument_map, data=data, account=account, need_ids=False
        )
        live_arg = [str(live_dir)]
    kw: dict[str, Any] = {"account": account or "main", **options}
    imap = instrument_map
    if imap is None and data is not None and b.name not in BOTS and lk.kind != "canonical":
        imap = str(out / "instrument_map.csv")  # written by canonical_live onto the dataset's symbols
    if imap is not None:
        kw["instrument_map"] = imap
    if window_ns is not None:
        kw["window_ns"] = window_ns
    report = b.reconcile(bpaths if len(bpaths) > 1 else bpaths[0], live_arg, str(out), **kw)
    if data is not None and b.name in BOTS and instrument_map is None:
        # the bot's instruments onto the dataset's symbols, then the same reconciliation again in those units
        imap_p, n2 = _data_map(out / "instrument_map.csv", _log_instruments(out / "live"), data)
        notes += n2
        for d in ("live", "sim", "score"):
            shutil.rmtree(out / d, ignore_errors=True)
        report = b.reconcile(
            bpaths if len(bpaths) > 1 else bpaths[0], live_arg, str(out), **{**kw, "instrument_map": str(imap_p)}
        )
    run = _Run("reconcile", out / "live", out / "sim", _summary(out / "score"), out / "score", report=report)
    run.notes = notes + [str(w) for w in report.warnings]
    if data is not None:
        _with_data(run, out, window_ns, data)
    return run


def _score_path(
    b: Any,
    bpaths: list[str],
    lk: LiveKind,
    lpaths: list[str],
    out: Path,
    *,
    data: str | None,
    instrument_map: str | None,
    window_ns: int | None,
    account: str | None,
    options: dict[str, Any],
) -> _Run:
    from queuetruth import live as qlive

    live_dir, notes = canonical_live(
        lk, lpaths, out, instrument_map=instrument_map, data=data, account=account, need_ids=True
    )
    venues = sorted({v for v, _ in _log_instruments(live_dir)})
    imap = instrument_map or str(_live_map(out / "backtest_instrument_map.csv", live_dir))
    sim_dir = out / "backtest"
    import warnings

    with warnings.catch_warnings():  # the scorer takes each order's instrument from its live twin
        warnings.simplefilter("ignore", qlive.LiveImportWarning)
        s = qlive.import_sim(
            b.name,
            bpaths,
            sim_dir,
            account=account,
            instrument_map=imap,
            venue=venues[0] if len(venues) == 1 else None,
            **options,
        )
    notes += [f"backtest: {w}" for w in s.warnings if not w.startswith("unmapped_instruments:")]
    live_keys, sim_keys = _keys(live_dir), _keys(sim_dir)
    if not live_keys & sim_keys:
        raise QtError(
            "QT-INPUT-002",
            f"compare: none of the backtest's {len(sim_keys)} orders carries the key of one of the live log's "
            f"{len(live_keys)} orders, so no order can be compared",
            f"{b.label} orders are linked to live orders by key: run the backtest on the live schedule (queuetruth "
            "export replay-schedule) so each order keeps its live order key (hftbacktest: an order_key column; "
            "NautilusTrader: the live key as client_order_id)",
        )
    doc = _score(live_dir, {"kind": "external", "log": {"path": str(sim_dir)}}, out / "score", window_ns, data)
    run = _Run("score", live_dir, sim_dir, doc, out / "score", data_score=doc if data else None, notes=notes)
    run.data_score_dir = out / "score" if data else None
    if data is not None:
        run.exact = _score(live_dir, {"kind": "exact"}, out / "exact", window_ns, data)
    return run


def _with_data(run: _Run, out: Path, window_ns: int | None, data: str) -> None:
    """The reconciled logs scored again with the dataset (markouts, P&L per order) and the exact arm."""
    w = window_ns or (run.score or {}).get("spec", {}).get("window_ns")
    sim = {"kind": "external", "log": {"path": str(run.sim_dir)}}
    try:
        run.data_score = _score(run.live_dir, sim, out / "score-data", w, data)
        run.data_score_dir = out / "score-data"
        run.exact = _score(run.live_dir, {"kind": "exact"}, out / "exact", w, data)
    except QtError as e:
        run.notes.append(f"data: the logs could not be scored with {data} ({e.message})")


# ---- rows and answer --------------------------------------------------------------------------------------------


def _shortfall(score_dir: Path | None, summary: dict[str, Any] | None) -> tuple[dict[str, float], str | None, int]:
    """(order key -> total P&L gap in currency, currency, horizon) from `score_shortfall.parquet`."""
    if score_dir is None or summary is None:
        return {}, None, 0
    p = score_dir / "score_shortfall.parquet"
    gaps = summary.get("pnl_gap") or []
    if not p.is_file() or not gaps:
        return {}, None, 0
    import pyarrow.parquet as pq

    h = int(gaps[0].get("horizon_ns") or 0)
    out: dict[str, float] = {}
    cur: str | None = None
    for r in pq.read_table(p).to_pylist():
        if r["scope"] == "order" and int(r["horizon_ns"]) == h and r.get("valued") and r.get("total_ccy") is not None:
            out[str(r["order_key"])] = float(r["total_ccy"])
            cur = cur or r.get("currency")
    return out, cur, h


def _reconcile_rows(report: Any, tz: str, gaps: dict[str, float]) -> list[dict[str, Any]]:
    rows = []
    for r in report.orders:
        bq, lq = _dec(r.sim_filled_qty) or Decimal(0), _dec(r.live_filled_qty) or Decimal(0)
        outcome = "both" if bq > 0 and lq > 0 else "backtest_only" if bq > 0 else "live_only" if lq > 0 else "neither"
        key = r.live_key or r.sim_key
        gap = gaps.get(str(r.live_key)) if r.live_key else None
        rows.append(
            {
                "order": key,
                "symbol": r.instrument,
                "side": units.side_word(r.side),
                "qty": r.qty,
                "backtest_price": r.sim_limit_px,
                "live_price": r.live_limit_px,
                "placed": _local(r.live_placed, tz),
                "backtest_filled_qty": r.sim_filled_qty,
                "live_filled_qty": r.live_filled_qty,
                "backtest_fill_price": r.sim_avg_px,
                "live_fill_price": r.live_avg_px,
                "backtest_first_fill": _local(r.sim_first_fill, tz),
                "live_first_fill": _local(r.live_first_fill, tz),
                "outcome": outcome,
                "category": r.category,
                "why": r.explanation,
                "price_gap": r.sim_price_advantage_quote,
                "pnl_gap": gap,
                "tz": tz,
            }
        )
    return rows


def _score_rows(run: _Run, tz: str, gaps: dict[str, float]) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    from queuetruth.live import read_log, table_rows

    lo, _ = read_log(str(run.live_dir))
    names = {str(o["order_key"]): str(o["instrument"]) for o in table_rows(lo)}
    so, _ = read_log(str(run.sim_dir))
    sim_keys = {str(o["order_key"]) for o in table_rows(so)}
    rows = []
    assert run.score_dir is not None
    scored: set[str] = set()
    for r in pq.read_table(run.score_dir / "score_orders.parquet").to_pylist():
        k = str(r["order_key"])
        scored.add(k)
        cls = str(r["class"])
        pe, se = r.get("price_exp"), r.get("size_exp")
        outcome = {"sim_only": "backtest_only", "live_only": "live_only"}.get(cls, cls)
        rows.append(
            {
                "order": k,
                "symbol": names.get(k),
                "side": units.side_word(r["side"]),
                "qty": _fixed(r["qty"], se),
                "backtest_price": _price(r["limit_px"], pe),
                "live_price": _price(r["limit_px"], pe),
                "placed": _local(r["ts_entry"], tz),
                "backtest_filled_qty": _fixed(r["sim_filled_qty"], se),
                "live_filled_qty": _fixed(r["live_filled_qty"], se),
                "backtest_fill_price": None,
                "live_fill_price": None,
                "backtest_first_fill": _local(r.get("sim_ts_first_fill"), tz),
                "live_first_fill": _local(r.get("live_ts_first_fill"), tz),
                "outcome": outcome,
                "category": cls,
                "why": REASONS.get(cls, "filled in both" if cls == "both" else "filled in neither"),
                "price_gap": None,
                "pnl_gap": gaps.get(k),
                "tz": tz,
            }
        )
    for k in sorted(sim_keys - scored - set(names)):
        rows.append(
            {
                "order": k,
                "symbol": None,
                "side": None,
                "outcome": "backtest_unpaired",
                "category": "missing_in_live",
                "why": REASONS["missing_in_live"],
                "tz": tz,
            }
        )
    return rows


def _tz(data: str | None, symbols: set[str]) -> str:
    """The venue-local time zone: the dataset's session zone of these symbols when it has one, else UTC (crypto
    venues and the canonical log keep UTC)."""
    if data is None:
        return "UTC"
    try:
        from queuetruth.check.symbols import dataset_symbols, norm

        syms = dataset_symbols(data)
    except QtError:
        return "UTC"
    by = {norm(s): i.tz for s, i in syms.items()}
    zones = {by[norm(s)] for s in symbols if s and norm(s) in by}
    return zones.pop() if len(zones) == 1 else "UTC"


def _fills_line(rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    bt = [r for r in rows if r["outcome"] in ("both", "backtest_only")]
    both = sum(1 for r in bt if r["outcome"] == "both")
    lo, hi = _wilson(both, len(bt))
    n_live = sum(1 for r in rows if r["outcome"] in ("both", "live_only"))
    fills = {"backtest": len(bt), "both": both, "live": n_live, "share": both / len(bt) if bt else None}
    fills.update({"ci_lo": lo, "ci_hi": hi})
    if not bt:
        return f"The backtest made no fills to compare; live filled {n_live} order(s).", fills
    return (
        f"{both} of the backtest's {len(bt)} fills also filled live ({_pct(fills['share'])}, 95% CI {_pct(lo)} to "
        f"{_pct(hi)}, orders counted as independent); live filled {n_live} order(s) in all.",
        fills,
    )


def _one_sided(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    out: dict[str, Any] = {}
    lines = []
    for side, label in (("backtest_only", "only the backtest"), ("live_only", "only live")):
        sel = [r for r in rows if r["outcome"] == side]
        cats = Counter(str(r["category"]) for r in sel)
        out[side] = dict(cats)
        if sel:
            why = "; ".join(f"{n} {REASONS.get(c, c)}" for c, n in cats.most_common())
            lines.append(f"Fills {label} made: {len(sel)} ({why}).")
    unpaired = [r for r in rows if r["outcome"] in ("backtest_unpaired",)]
    if unpaired:
        out["backtest_unpaired"] = len(unpaired)
        lines.append(f"Backtest orders with no live order to compare: {len(unpaired)} ({REASONS['missing_in_live']}).")
    return lines, out


def _pnl_lines(run: _Run, cur: str | None) -> tuple[list[str], dict[str, Any]]:
    lines: list[str] = []
    pnl: dict[str, Any] = {"currency": cur}
    rep = run.report
    if rep is not None:
        adv = dict(rep.sim_price_advantage_quote)
        missed = dict(rep.missed_fill_notional)
        pnl.update({"price_advantage": adv, "missed_fill_notional": missed})
        for inst in sorted(set(adv) | set(missed)):
            parts = []
            if inst in adv and _dec(adv[inst]) != 0:
                parts.append(f"on fills both made the backtest's prices were better by {adv[inst]} (quote)")
            if inst in missed:
                parts.append(f"fills only the backtest made are {missed[inst]} of notional (quote)")
            if parts:
                lines.append(f"P&L gap {inst}: " + "; ".join(parts) + ".")
    ds = run.data_score
    if ds is not None:
        for g in ds.get("pnl_gap") or []:
            if isinstance(g, dict) and g.get("headline"):
                lines.append(f"P&L gap per order (marked to the dataset's mid): {g['headline']}")
                pnl["marked"] = g
    elif rep is None:
        lines.append("P&L gap: needs --data (each fill is marked to the dataset's mid).")
    return lines, pnl


def _practitioner(doc: dict[str, Any] | None, data: bool) -> list[str]:
    from queuetruth.score.lines import practitioner_lines

    lines = practitioner_lines(doc) if doc else []
    if not data and not any(x.startswith("markouts:") for x in lines):
        lines.append("markouts: need --data (fills are marked to the dataset's mid at 100 ms, 1 s and 10 s).")
    return lines


def _latency(live_dir: Path) -> tuple[str, dict[str, Any] | None]:
    from queuetruth import live as qlive

    try:
        fit = qlive.latency_stats(str(live_dir))
    except QtError as e:
        return f"latency: not measured ({e.message})", None
    e = fit.get("entry_ns") or {}
    if e.get("p50") is None:
        return (
            "latency: not measured: no live order has both a local send time and an exchange ack time "
            "(log ts_local_send on submit_sent and ts_exchange on ack to measure it)",
            None,
        )
    return (
        f"latency: median entry latency {_ms(e['p50'])} (p10 {_ms(e.get('p10'))}, p90 {_ms(e.get('p90'))}, "
        f"{e.get('n')} orders), exchange ack minus local send, fitted from the live log",
        {k: fit.get(k) for k in ("entry_ns", "response_ns", "cancel_ns", "amend_ns")},
    )


def _exact_line(doc: dict[str, Any] | None) -> str | None:
    if not doc:
        return None
    b = doc.get("bias") or {}
    v, lo, hi = b.get("fill_rate_bias"), b.get("ci_lo"), b.get("ci_hi")
    if v is None:
        return "exact queue: not computable (" + str((doc.get("verdict") or {}).get("headline")) + ")"
    pp = abs(v) * 100
    what = "as often as" if pp < 0.05 else f"{pp:.1f} pp {'more' if v > 0 else 'less'} often than"
    ci = f" (95% CI {lo * 100:+.1f} to {hi * 100:+.1f} pp)" if lo is not None and hi is not None else ""
    return f"exact queue: the live orders replayed in the dataset's real queue fill {what} live{ci}."


def compare(
    backtest: str | Path | Sequence[str | Path],
    live: str | Path | Sequence[str | Path],
    *,
    data: str | Path | None = None,
    backtester: str | None = None,
    venue: str | None = None,
    out: str | Path | None = None,
    instrument_map: str | Path | None = None,
    window: str | int | None = None,
    account: str | None = None,
    options: dict[str, Any] | None = None,
) -> CompareResult:
    """A backtest against the live run of the same strategy, order by order (see the module docstring).

    `backtest`: the backtester's output file(s) of one run; `live`: a canonical live-log directory, a bot's own
    records (database, log) or a venue export; `data`: an order-level dataset of the same days (markouts, the P&L
    gap per order and the exact arm). `backtester` and `venue` override what is read from the files. `window`
    (e.g. `"1h"`) is how long after entry a fill counts (default: the reconciler's, 60 s for `score`). `options`
    go to the backtest's importer (e.g. `timeframe="1h"`). `out` (default a new temporary directory) receives
    `compare.json`, `orders.csv` and the logs and scorecards behind them."""
    bpaths = _paths(backtest, "backtest")
    lpaths = _paths(live, "live")
    kind = detect_backtest(bpaths, backtester)
    b = backtesters.get(kind)
    lk = detect_live(lpaths, venue)
    window_ns = _ns(window)
    opts = dict(options or {})
    imap = None if instrument_map is None else str(instrument_map)
    data_s = None if data is None else str(data)
    if data_s is not None and not (Path(data_s) / "manifest.json").is_file():
        raise QtError(
            "QT-INPUT-001",
            f"compare: {data_s} is not a queuetruth dataset (no manifest.json)",
            "write one with `queuetruth ingest` (order-level data) or try `queuetruth fixtures synth --out synth`",
        )
    outp = _prepare_out(out)
    kw: dict[str, Any] = {
        "data": data_s,
        "instrument_map": imap,
        "window_ns": window_ns,
        "account": account,
        "options": opts,
    }
    if getattr(b, "reconcile", None) is not None:
        run = _reconcile_path(b, bpaths, lk, lpaths, outp, **kw)
    else:
        run = _score_path(b, bpaths, lk, lpaths, outp, **kw)
    gaps, cur, _h = _shortfall(run.data_score_dir, run.data_score)
    if run.report is not None:
        tz = _tz(data_s, {r.instrument for r in run.report.orders})
        rows = _reconcile_rows(run.report, tz, gaps)
    else:
        tz = _tz(data_s, set(n for _, n in _log_instruments(run.live_dir)))
        rows = _score_rows(run, tz, gaps)
    first, fills = _fills_line(rows)
    one, one_doc = _one_sided(rows)
    pnl_lines, pnl = _pnl_lines(run, cur)
    stats_doc = run.data_score or run.score
    prac = _practitioner(stats_doc, data_s is not None)
    lat_line, lat = _latency(run.live_dir)
    exact = _exact_line(run.exact)
    lines = [first, *one, *pnl_lines, *prac, lat_line, *([exact] if exact else [])]
    s = stats_doc or {}
    window_eff = int(window_ns or (s.get("spec") or {}).get("window_ns") or 0) or None
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "answer": first,
        "lines": lines,
        "backtester": b.name,
        "backtester_label": b.label,
        "backtest": bpaths,
        "live": {"kind": lk.kind, "name": lk.name, "inputs": lpaths, "log": str(run.live_dir)},
        "detected": {"backtester": backtester is None, "live": lk.detected},
        "path": run.path,
        "data": data_s,
        "window_ns": window_eff,
        "window": None if window_eff is None else units.format_duration(window_eff),
        "tz": tz,
        "fills": fills,
        "one_sided": one_doc,
        "pnl": pnl,
        "ttf_survival": s.get("ttf_survival"),
        "markout_diff": [r for r in s.get("markout_diff") or [] if int(r.get("horizon_ns") or 0) in HORIZONS_NS]
        if run.data_score
        else None,
        "latency": lat,
        "exact": None
        if run.exact is None
        else {k: run.exact.get(k) for k in ("bias", "two_by_two", "n_orders", "n_matched", "run_id")},
        "score": None if run.score is None else {k: run.score.get(k) for k in ("run_id", "two_by_two", "bias")},
        "notes": list(dict.fromkeys(run.notes)),
        "files": {},
    }
    files = doc["files"]
    files["orders"] = str(_write_orders(outp / "orders.csv", rows))
    if run.report is not None:
        files["reconcile"] = str(outp / "reconcile.json")
    if run.score_dir is not None:
        files["score"] = str(run.score_dir)
    if run.data_score_dir is not None and run.data_score_dir != run.score_dir:
        files["score_data"] = str(run.data_score_dir)
    if run.exact is not None:
        files["exact"] = str(outp / "exact")
    files["compare"] = str(outp / "compare.json")
    write_doc(doc, outp)
    return CompareResult(doc, rows, outp)


ORDER_COLUMNS = (
    "order",
    "symbol",
    "side",
    "qty",
    "backtest_price",
    "live_price",
    "placed",
    "outcome",
    "category",
    "backtest_filled_qty",
    "live_filled_qty",
    "backtest_fill_price",
    "live_fill_price",
    "backtest_first_fill",
    "live_first_fill",
    "price_gap",
    "pnl_gap",
    "tz",
    "why",
)


def _write_orders(path: Path, rows: list[dict[str, Any]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(ORDER_COLUMNS)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r[c] for c in ORDER_COLUMNS])
    return path


def write_doc(doc: dict[str, Any], out: Path) -> None:
    """Writes `compare.json`."""
    (out / "compare.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n", encoding="utf-8"
    )


__all__ = ["HORIZONS_NS", "ORDER_COLUMNS", "REASONS", "SCHEMA", "CompareResult", "canonical_live", "compare"]
