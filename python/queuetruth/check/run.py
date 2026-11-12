"""`check`: a backtest's own resting limit orders replayed in the exact queue of an order-level dataset.

Steps (each one is existing machinery; this module only joins them):

1. Import the backtest's output with its simulator importer (`queuetruth.live.importers`, the same code as
   `live import --venue sim-*`), first without an instrument map to learn its instrument names.
2. Map those names to the dataset's symbols (`queuetruth.check.symbols`) and import again with that map, so prices
   and quantities are in the dataset's units. The canonical log goes to `<out>/backtest`.
3. Entry time: a backtest's clock is the data's clock, so the time its acknowledgement carries is when the order
   joined the book in the backtest. Report-only exports (NautilusTrader's orders report, Backtrader's notification
   lines) stamp it as a record time; it is used as the entry time all the same. An order without an
   acknowledgement enters at its submit time.
4. Score the log against the exact replay (`queuetruth.score.build_scorecard`, arm `exact`, no self-exclusion:
   the backtest's orders are not on the tape). The backtest is the scorer's "live" arm and the exact queue its
   simulator arm, so the scorer's `live_only` orders are the backtest's phantom fills and its P&L gap
   (`R_sim - R_live`) is the real queue's P&L minus the backtest's.
5. Restate the scorecard in the backtest's words (`CheckResult`).

Orders are judged over one evaluation window from entry (default 1 hour): a fill counts when it happens within
the window and before the backtest cancelled the order, in both arms. P&L is each arm's fills marked to the mid
at the markout horizon (default 1 minute), net of the backtest's fees.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import statistics
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from queuetruth.check.detect import BACKTESTERS, detect_all
from queuetruth.check.symbols import (
    Instrument,
    dataset_symbols,
    match,
    order_counts,
    unmapped_error,
    write_map,
)
from queuetruth.errors import QtError

SCHEMA = "qt.check"
DEFAULT_WINDOW = "1h"
DEFAULT_MARKOUT = "1min"
#: Resting time-in-force values (`queuetruth.live.canonical.RESTING_TIF`) for the limit-order count.
_RESTING = ("gtc", "day", "post_only", "gtx", "gtd")


@dataclass
class CheckResult:
    """The answer of `check`: `summary` is the one-line answer, `doc` the full `qt.check` document, `orders` one
    row per checked order in human units, `out` the output directory. Without data `doc` is the atlas estimate
    (`qt.check_estimate`, `doc["mode"] == "atlas"`) and `orders` holds one row per tier and side."""

    doc: dict[str, Any]
    orders: list[dict[str, Any]] = field(default_factory=list)
    out: Path | None = None

    @property
    def summary(self) -> str:
        """The answer in one plain sentence."""
        return str(self.doc["answer"])

    def to_dict(self) -> dict[str, Any]:
        """The `qt.check` document (JSON-ready)."""
        return self.doc

    def table(self) -> Any:
        """One row per checked order as a pandas DataFrame (symbols, decimal prices, venue-local times)."""
        try:
            import pandas as pd
        except ImportError as e:
            raise QtError("QT-CONFIG-001", "CheckResult.table needs pandas", "pip install pandas") from e
        return pd.DataFrame(self.orders)

    def text(self, *, max_symbols: int = 5, max_examples: int = 3) -> str:
        """The terminal report: the answer first, then the 2x2 table, the worst symbols and `why` hints."""
        if self.doc.get("mode") == "atlas":
            from queuetruth.check.estimate import text as estimate_text

            return estimate_text(self.doc)
        from queuetruth.check.render import text

        return text(self.doc, max_symbols=max_symbols, max_examples=max_examples)

    def to_html(self) -> str:
        """A self-contained HTML report."""
        if self.doc.get("mode") == "atlas":
            from queuetruth.check.estimate import html as estimate_html

            return estimate_html(self.doc)
        from queuetruth.check.render import html

        return html(self.doc, self.orders)

    def _repr_html_(self) -> str:
        return self.to_html()

    def __str__(self) -> str:
        return self.summary


def _ns(v: Any, what: str) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        if v <= 0:
            raise QtError("QT-USAGE-001", f"check: {what} must be positive, got {v}")
        return v
    from queuetruth.pm.cli import duration_ns

    n = duration_ns(str(v))
    if n <= 0:
        raise QtError("QT-USAGE-001", f"check: {what} must be positive, got {v!r}")
    return n


def _paths(backtest: str | Path | Sequence[str | Path]) -> list[str]:
    items = [backtest] if isinstance(backtest, str | Path) else list(backtest)
    if not items:
        raise QtError("QT-USAGE-001", "check: no backtest file given")
    out = []
    for p in items:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"check: {p}: no such file")
        out.append(str(p))
    return out


def _dataset(data: str | Path) -> tuple[Path, dict[str, Any]]:
    root = Path(data)
    mp = root / "manifest.json"
    if not mp.is_file():
        raise QtError(
            "QT-INPUT-001",
            f"check: {root} is not a queuetruth dataset (no manifest.json)",
            "write one with `queuetruth ingest` (order-level data) or try `queuetruth fixtures synth --out synth`",
        )
    man = json.loads(mp.read_text())
    if str(man.get("kind", "mbo")) == "l2":
        raise QtError(
            "QT-CONFIG-001",
            f"check: {root} is an L2-only dataset; replaying orders in the exact queue needs order-level (MBO) data",
            "ingest order-level data (ITCH, IEX DEEP+, Databento MBO) for the backtest's days",
        )
    return root, man


def _import(kind: str, paths: list[str], imap: Any, extra: dict[str, Any]) -> Any:
    from queuetruth import backtesters
    from queuetruth.live.importers import ImportOptions

    return backtesters.get(kind).import_sim(paths, ImportOptions(account="backtest", imap=imap, extra=dict(extra)))


def _vectorbt_labels(paths: list[str]) -> list[str]:
    """Column labels of VectorBT records (each column is its own backtest; check keeps those that are symbols)."""
    from queuetruth.live.importers.vectorbt import _table

    labels: set[str] = set()
    for i, p in enumerate(paths):
        _, rows = _table(Path(p), i)
        labels |= {str(r["Column"]) for r, _ in rows if r.get("Column") not in (None, "")}
    return sorted(labels)


def _entry_times(events: list[dict[str, Any]]) -> int:
    """Marks the acknowledgement times of a backtest as entry times (step 3); returns how many were changed."""
    from queuetruth.live.canonical import ENTRY_KINDS

    n = 0
    for e in events:
        if (
            e["event_type"] == "ack"
            and e.get("ts_exchange") is not None
            and e.get("ts_exchange_kind") not in ENTRY_KINDS
        ):
            e["ts_exchange_kind"] = "match_time"
            n += 1
    return n


def _span(man: dict[str, Any]) -> tuple[int, int] | None:
    lo = [int(p["ts_min"]) for p in man.get("partitions") or [] if p.get("ts_min") is not None]
    hi = [int(p["ts_max"]) for p in man.get("partitions") or [] if p.get("ts_max") is not None]
    return (min(lo), max(hi)) if lo and hi else None


def _first_ts(events: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        ts = e.get("ts_exchange") or e.get("ts_local_send") or e.get("ts_local_recv")
        if ts is not None:
            k = str(e["order_key"])
            out[k] = min(out.get(k, int(ts)), int(ts))
    return out


def _day(ns: int) -> str:
    return dt.datetime.fromtimestamp(ns / 1e9, tz=dt.UTC).date().isoformat()


def _local(ns: int | None, tz: str) -> str | None:
    if ns is None:
        return None
    t = dt.datetime.fromtimestamp(ns // 1_000_000_000, tz=ZoneInfo(tz)).replace(microsecond=(ns // 1000) % 1_000_000)
    return t.isoformat(timespec="milliseconds")


def _dec(v: Any, exp: int | None) -> str | None:
    if v is None or exp is None:
        return None
    from decimal import Decimal

    return format(Decimal(int(v)).scaleb(int(exp)).normalize(), "f")


def _seconds(ns: Any) -> float | None:
    return None if ns is None else round(int(ns) / 1e9, 3)


def _fnum(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None


def check(
    backtest: str | Path | Sequence[str | Path],
    data: str | Path | None = None,
    *,
    backtester: str | None = None,
    instrument_map: str | Path | None = None,
    out: str | Path | None = None,
    window: str | int = DEFAULT_WINDOW,
    markout: str | int = DEFAULT_MARKOUT,
    options: dict[str, Any] | None = None,
    tier: str | None = None,
    bar: str | None = None,
    atlas: str | Path | None = None,
    yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    refresh: bool = False,
    seed: int | None = None,
) -> CheckResult:
    """How wrong is a backtest: its own resting limit orders replayed in the exact queue of `data`, or without
    `data` an estimate from the atlas touch tables (`queuetruth.check.estimate`).

    `backtest`: the backtester's output (one path or several of one run): hftbacktest order snapshots,
    NautilusTrader reports or order events, Backtrader `QtOrderLog` lines, VectorBT order records, LEAN result and
    order events, or a Freqtrade backtest result. `backtester` names it when the files do not say. `data`: an
    order-level dataset covering the backtest's days; without it the answer is the atlas estimate for a bar backtest:
    `tier` (for symbols the atlas does not hold), `bar` (when the log does not say), `atlas` (a directory or URL of
    the atlas data API), `yes` / `confirm` (consent to download it), `refresh` and `seed` go to
    `queuetruth.check.estimate.estimate`.
    Symbols map from the dataset's symbology; `instrument_map` (CSV, venue = the dataset's venue) overrides that.
    `window` (default 1h) is how long after entry a fill counts; `markout` (default 1 min) the P&L horizon.
    `options` go to the importer (e.g. `timeframe`, `data_tz`, `column`). `out` (default: a new temporary
    directory) receives `backtest/` (the imported log), `instrument_map.csv`, `score/`, `check.json` and
    `orders.csv`."""
    paths = _paths(backtest)
    if data is None:
        from queuetruth.check.estimate import estimate

        return estimate(
            paths,
            backtester=backtester,
            tier=tier,
            bar=bar,
            atlas=atlas,
            yes=yes,
            confirm=confirm,
            refresh=refresh,
            out=out,
            options=options,
            seed=seed,
        )
    window_ns, markout_ns = _ns(window, "window"), _ns(markout, "markout")
    if backtester is not None and backtester not in BACKTESTERS:
        raise QtError("QT-USAGE-001", f"check: backtester must be one of {list(BACKTESTERS)}, got {backtester!r}")
    kind = backtester or detect_all(paths)
    root, man = _dataset(data)
    venue = str(man.get("venue") or "dataset")
    outp = Path(out) if out is not None else Path(tempfile.mkdtemp(prefix="qt-check-"))
    outp.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    extra: dict[str, Any] = dict(options or {})
    if kind == "vectorbt" and "order_type" not in extra:
        extra["order_type"] = "limit"
        notes.append(
            "VectorBT records are fills only: each is checked as a limit order resting at its fill price "
            "(option order_type=market leaves them out)"
        )
    extra.setdefault("map_venue", venue)

    # ---- symbols -------------------------------------------------------------------------------------------
    from queuetruth.live import load_instrument_map, write_canonical_log

    symbols: dict[str, Instrument] = dataset_symbols(root) if instrument_map is None else {}
    mapping: dict[str, str] = {}
    if instrument_map is None:
        if kind == "vectorbt" and not any(extra.get(k) for k in ("column", "instrument", "instruments")):
            labels = _vectorbt_labels(paths)
            if len(labels) > 1:
                m = match(labels, symbols)
                if not m.mapped:
                    raise unmapped_error(m, {})
                extra["instruments"] = dict(m.mapped)
        first = _import(kind, paths, None, extra)
        counts = order_counts(first.orders)
        m = match(list(counts), symbols)
        if not m.mapped:
            raise unmapped_error(m, counts)
        notes += m.notes
        for name, why in sorted(m.unmapped.items()):
            notes.append(f"{counts.get(name, 0)} order(s) on {name} not checked: {name} {why}")
        mapping = m.mapped
        map_path = write_map(outp / "instrument_map.csv", venue, mapping, symbols)
    else:
        map_path = Path(instrument_map)
    imap = load_instrument_map(map_path)
    res = _import(kind, paths, imap, extra)
    notes += [str(w) for w in res.warnings if "pair_with" not in str(w)]
    n_entry = _entry_times(res.events)
    if n_entry:
        notes.append(f"{n_entry} order(s) enter the queue at the backtest's acknowledgement time (a record time)")
    log_dir = outp / "backtest"
    write_canonical_log(log_dir, res.orders, res.events, instrument_map_sha256=imap.sha256)

    # ---- what can be checked -------------------------------------------------------------------------------
    n_orders = len(res.orders)
    limit = [o for o in res.orders if o.get("order_type") == "limit" and o.get("tif") in _RESTING]
    if not limit:
        types: dict[str, int] = {}
        for o in res.orders:
            k = f"{o.get('order_type')}/{o.get('tif')}"
            types[k] = types.get(k, 0) + 1
        shown = ", ".join(f"{n} {k}" for k, n in sorted(types.items())) or "none"
        raise QtError(
            "QT-INPUT-002",
            f"check: the backtest has no resting limit order to replay ({n_orders} order(s): {shown})",
            "only resting limit orders have a queue position; market orders fill the same way everywhere",
        )
    span = _span(man)
    first_ts = _first_ts(res.events)
    lim_ts = [first_ts[str(o["order_key"])] for o in limit if str(o["order_key"]) in first_ts]
    if span is not None and lim_ts and not any(span[0] <= t <= span[1] for t in lim_ts):
        raise QtError(
            "QT-INPUT-002",
            f"check: the backtest's orders run from {_day(min(lim_ts))} to {_day(max(lim_ts))} (UTC), the dataset "
            f"covers {_day(span[0])} to {_day(span[1])}: no order falls inside it",
            "check against a dataset of the backtest's days",
        )
    mapped_ids = {str(o["order_key"]) for o in limit if o.get("instrument_id") is not None}
    if not mapped_ids:
        raise QtError(
            "QT-INPUT-002",
            f"check: none of the backtest's {len(limit)} limit order(s) is on an instrument of the instrument map",
            "the venue column of --instrument-map must be the dataset's venue " + repr(venue),
        )

    # ---- the exact replay ------------------------------------------------------------------------------------
    from queuetruth import api

    score_dir = outp / "score"
    sm = api.score(
        {
            "live": {"path": str(log_dir)},
            "dataset": {"path": str(root)},
            "sim": {"kind": "exact"},
            "out": str(score_dir),
            "self_exclude": False,
            "window_ns": window_ns,
        }
    )
    doc, orders = _result(
        sm.model_dump(mode="json", by_alias=True),
        score_dir,
        root,
        log_dir,
        markout_ns=markout_ns,
        window_ns=window_ns,
        symbols=symbols or _symbols_or_none(root),
    )
    doc.update(
        {
            "backtester": kind,
            "backtest": paths,
            "dataset": str(root),
            "out": str(outp),
            "n_orders": n_orders,
            "n_limit": len(limit),
            "instruments": mapping,
            "instrument_map": str(map_path),
        }
    )
    doc["notes"] = notes + doc.pop("_notes", [])
    doc["answer"] = _answer(doc)
    doc["files"] = _write(outp, doc, orders)
    return CheckResult(doc=_order(doc), orders=orders, out=outp)


def _symbols_or_none(root: Path) -> dict[str, Instrument]:
    """The dataset's symbols for display; none when it has no instruments table (the user's map gave the ids)."""
    try:
        return dataset_symbols(root)
    except QtError:
        return {}


def _result(
    sm: dict[str, Any],
    score_dir: Path,
    root: Path,
    log_dir: Path,
    *,
    markout_ns: int,
    window_ns: int,
    symbols: dict[str, Instrument],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The `qt.check` numbers and the per-order rows from the scorecard's files."""
    import pyarrow.parquet as pq

    from queuetruth.score.pnl_gap import pick_horizon, pnl_gap_table

    sym_of: dict[int, Instrument] = {}
    for inst in symbols.values():
        for iid in inst.ids.values():
            sym_of[iid] = inst
    rows = pq.read_table(score_dir / "score_orders.parquet").to_pylist()
    sf = pq.read_table(score_dir / "score_shortfall.parquet")
    h = pick_horizon(sf.column("horizon_ns").to_pylist(), markout_ns) if sf.num_rows else None
    gap_by_order: dict[str, float] = {}
    if h is not None:
        for r in sf.to_pylist():
            if (
                r["scope"] == "order"
                and int(r["horizon_ns"]) == h
                and r.get("valued")
                and r.get("total_ccy") is not None
            ):
                gap_by_order[str(r["order_key"])] = -float(r["total_ccy"]) + 0.0  # backtest minus real
    word = {"both": "both", "live_only": "phantom", "sim_only": "real_only", "neither": "neither"}
    orders: list[dict[str, Any]] = []
    for r in rows:
        iid = int(r["instrument_id"])
        inst = sym_of.get(iid)
        tz = inst.tz if inst is not None else "UTC"
        ts = int(r["ts_entry"].value) if hasattr(r["ts_entry"], "value") else int(r["ts_entry"].timestamp() * 1e9)
        orders.append(
            {
                "order": str(r["order_key"]),
                "symbol": inst.symbol if inst is not None else str(iid),
                "side": r["side"],
                "price": _dec(r["limit_px"], r.get("price_exp")),
                "qty": _dec(r["qty"], r.get("size_exp")),
                "entry": _local(ts, tz),
                "backtest_filled": bool(r["live_filled"]),
                "backtest_fill_after_s": _seconds(r.get("ttf_live_ns")),
                "real_filled": bool(r["sim_filled"]),
                "real_fill_after_s": _seconds(r.get("ttf_sim_ns")),
                "queue_ahead": r.get("queue_ahead_at_entry"),
                "outcome": word.get(str(r["class"]), str(r["class"])),
                "pnl_gap": gap_by_order.get(str(r["order_key"])),
            }
        )
    n = len(orders)
    two = {k: sum(1 for o in orders if o["outcome"] == k) for k in ("both", "phantom", "real_only", "neither")}
    bt_filled = two["both"] + two["phantom"]
    real_filled = two["both"] + two["real_only"]
    bias = sm.get("bias") or {}
    lo, hi = _fnum(bias.get("ci_lo")), _fnum(bias.get("ci_hi"))
    fill_gap = {
        "orders": bt_filled - real_filled,
        "ci_lo": _neg(None if hi is None else hi * n),
        "ci_hi": _neg(None if lo is None else lo * n),
    }
    pnl: dict[str, Any] = {"markout": human_duration(h) if h else None, "markout_ns": h}
    groups = pnl_gap_table(sf, markout_ns) if sf.num_rows else []
    if groups:
        g = max(groups, key=lambda x: int(x.get("n_valued") or 0))
        mean, glo, ghi = _fnum(g.get("mean_ccy")), _fnum(g.get("ci_lo")), _fnum(g.get("ci_hi"))
        flip = {"sim_too_high": "backtest_too_low", "sim_too_low": "backtest_too_high"}
        pnl.update(
            {
                "currency": g.get("currency"),
                "per_order": _neg(mean),
                "ci_lo": _neg(ghi),
                "ci_hi": _neg(glo),
                "direction": flip.get(str(g["direction"]), str(g["direction"])),
                "n_valued": int(g.get("n_valued") or 0),
                "reason": g.get("reason"),
            }
        )
        if len(groups) > 1:
            pnl["other_currencies"] = sorted(str(x.get("currency")) for x in groups if x is not g)
    both = [o for o in orders if o["outcome"] == "both"]
    ttf = {
        "n_both": len(both),
        "backtest_median_s": _median([o["backtest_fill_after_s"] for o in both]),
        "real_median_s": _median([o["real_fill_after_s"] for o in both]),
    }
    per_sym: dict[str, dict[str, Any]] = {}
    for o in orders:
        s = per_sym.setdefault(
            o["symbol"],
            {
                "symbol": o["symbol"],
                "orders": 0,
                "backtest_filled": 0,
                "real_filled": 0,
                "phantom": 0,
                "real_only": 0,
                "_gaps": [],
            },
        )
        s["orders"] += 1
        s["backtest_filled"] += o["backtest_filled"]
        s["real_filled"] += o["real_filled"]
        s["phantom"] += o["outcome"] == "phantom"
        s["real_only"] += o["outcome"] == "real_only"
        if o["pnl_gap"] is not None:
            s["_gaps"].append(o["pnl_gap"])
    syms = []
    for s in per_sym.values():
        gaps = s.pop("_gaps")
        s["pnl_gap_per_order"] = sum(gaps) / len(gaps) if gaps else None
        syms.append(s)
    syms.sort(key=lambda s: (-(s["backtest_filled"] - s["real_filled"]), -s["phantom"], s["symbol"]))
    from queuetruth.api import why_suggestion

    examples = []
    for o in sorted((o for o in orders if o["outcome"] == "phantom"), key=lambda o: (o["symbol"], o["entry"] or "")):
        examples.append(
            {
                "order": o["order"],
                "symbol": o["symbol"],
                "side": o["side"],
                "price": o["price"],
                "entry": o["entry"],
                "outcome": o["outcome"],
                "why": why_suggestion(str(root), live=str(log_dir), order_key=o["order"]),
            }
        )
        if len(examples) == 3:
            break
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "window": human_duration(window_ns),
        "window_ns": window_ns,
        "n_checked": n,
        "backtest_filled": bt_filled,
        "real_filled": real_filled,
        "two_by_two": two,
        "fill_gap": fill_gap,
        "pnl": pnl,
        "time_to_fill": ttf,
        "symbols": syms,
        "examples": examples,
        "scorecard": {"run_id": sm.get("run_id"), "verdict": (sm.get("verdict") or {}).get("headline")},
        "_notes": [str(w) for w in sm.get("warnings") or [] if _keep_warning(str(w))],
    }
    return doc, orders


def human_duration(ns: int) -> str:
    """`1h`, `90s`, `1min`, `500ms`: the largest unit that divides `ns`."""
    for unit, size in (("h", 3_600 * 10**9), ("min", 60 * 10**9), ("s", 10**9), ("ms", 10**6), ("us", 10**3)):
        if ns >= size and ns % size == 0:
            return f"{ns // size}{unit}"
    return f"{ns}ns"


def _neg(x: float | None) -> float | None:
    """`-x` without a negative zero (the gaps are stated backtest minus real; the scorer's are real minus backtest)."""
    return None if x is None else -x + 0.0


def _keep_warning(w: str) -> bool:
    """Scorer warnings that concern a backtest check (self-exclusion and live-log wording do not apply)."""
    return not w.startswith(("self_exclusion", "live: ", "sim: ", "ci_block=", "clusters: ", "race: "))


def _median(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return round(statistics.median(v), 3) if v else None


def _whole(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+,.0f}" if round(x) != 0 else "0"


def _answer(doc: dict[str, Any]) -> str:
    """The one-line answer."""
    from queuetruth.score.pnl_gap import money

    n, bf, rf = doc["n_checked"], doc["backtest_filled"], doc["real_filled"]
    fg = doc["fill_gap"]
    ci = f"95% CI {_whole(fg['ci_lo'])} to {_whole(fg['ci_hi'])}" if fg["ci_lo"] is not None else "no CI"
    s = (
        f"Your backtest filled {bf:,} of {n:,} limit order{'' if n == 1 else 's'} within {doc['window']}; in the "
        f"real queue {rf:,} would have filled (backtest minus real: {_whole(fg['orders'])} orders, {ci})."
    )
    left = doc["n_limit"] - n
    if left > 0:
        s += (
            f" {left:,} more of its {doc['n_limit']:,} limit orders could not be checked (outside the data or on "
            "instruments it does not hold; see the notes)."
        )
    ph = doc["two_by_two"]["phantom"]
    s += f" {ph:,} fill{'s are' if ph != 1 else ' is'} phantom"
    p = doc["pnl"]
    if p.get("per_order") is None:
        why = p.get("reason") or "no order could be valued"
        return s + f"; the P&L gap is not computable ({why})."
    cur = f" {p['currency']}" if p.get("currency") else ""
    lo, hi, v = p["ci_lo"], p["ci_hi"], p["per_order"]
    pci = f"95% CI {money(lo)} to {money(hi)}" if lo is not None and hi is not None else "no CI"
    at = f"at the {p['markout']} markout"
    if p["direction"] == "backtest_too_high":
        return s + f"; the backtest's P&L is {money(v, signed=False)}{cur} per order too high ({pci}) {at}."
    if p["direction"] == "backtest_too_low":
        return s + f"; the backtest's P&L is {money(v, signed=False)}{cur} per order too low ({pci}) {at}."
    return s + f"; no P&L gap shown: backtest minus real is {money(v)}{cur} per order ({pci} includes 0) {at}."


def _order(doc: dict[str, Any]) -> dict[str, Any]:
    """The document with the answer first."""
    first = ("schema", "schema_version", "answer")
    return {**{k: doc[k] for k in first}, **{k: v for k, v in doc.items() if k not in first}}


def _write(outp: Path, doc: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, str]:
    files = {
        "check": str(outp / "check.json"),
        "orders": str(outp / "orders.csv"),
        "backtest_log": str(outp / "backtest"),
        "scorecard": str(outp / "score"),
    }
    with (outp / "orders.csv").open("w", newline="") as f:
        cols = list(orders[0]) if orders else ["order"]
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(orders)
    body = _order({**doc, "files": files})
    (outp / "check.json").write_text(json.dumps(body, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return files
