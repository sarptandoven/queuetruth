"""`queuetruth pm ingest`: the user's own Kalshi or Polymarket captures -> queuetruth datasets and tables.

Output directory:

```
<out>/pm_ingest.json         qt.pm_ingest_summary
<out>/markets.json           what the captures say about each market (grid, fees, rewards, settlement)
<out>/book/                  L2-only dataset of the book as the venue published it (own orders included)
<out>/book_own_free/         the same book without the user's own resting orders (only with own orders)
<out>/live/                  canonical live log of the user's own orders and fills (only with own orders)
<out>/pm/queue_samples.parquet   qt.pm_queue_samples (Kalshi official queue positions)
<out>/pm/own_entries.parquet     qt.pm_own_entries (where each own order joined its level)
<out>/pm/chain_fills.parquet     qt.pm_chain_fills (Polymarket on-chain fills, with --chain)
<out>/pm/filled_orders.parquet   qt.pm_filled_orders (every resting order that filled, in level order)
<out>/pm/chain_queue.parquet     qt.pm_chain_queue (queue ahead of each own order at its first on-chain fill)
```

`out` must not exist or be empty (`QT-CONFIG-001`, as `queuetruth ingest`): a second run never mixes its files with
an earlier run's, and `queuetruth.pm.workdir.load` reads only the files `pm_ingest.json` lists.

`book/` and `book_own_free/` are ordinary queuetruth L2 datasets: `queuetruth zoo`, `calibrate-live`, `score` and
`dq` read them (`book/` with `live/` for the scorer and live calibration, which remove own orders themselves;
`book_own_free/` for zoo runs of hypothetical quotes). Instrument ids are 1.. in sorted symbol order unless
`instrument_ids` maps symbols. Tick sizes come from the market's price grid when captured, else the coarsest grid
(at most $0.01) that every captured price lies on. Reader anomalies that change what the book shows (Kalshi
messages dropped from a second subscription) join the data-quality anomalies of both datasets.

Constrained replay (Polymarket with `chain`): the operator fills the makers of a match in its `makerOrders` order,
which the exchange emits as consecutive `OrderFilled` logs, so the chain says which resting orders filled and in
what order. For each own order, the published batch holding its first fill's print (found by transaction hash,
side and price) is located, and every maker fill at the order's level in that batch's transactions that precedes
its own fill (block, log index) was queue ahead of it at the batch start: under price-time priority, which
Polymarket's documentation does not state and the kit assumes, the whole queue ahead is consumed before the order
fills. That is then the order's queue ahead, even when the fill completed the order (where the book alone only
bounds it); `pm calibrate` fits on it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.live import write_canonical_log
from queuetruth.pm.batches import Batch, build_batches
from queuetruth.pm.chain import ChainDecode, decode
from queuetruth.pm.dataset import Instrument, Series, write_dataset
from queuetruth.pm.kalshi import read_kalshi
from queuetruth.pm.models import PmIngestSpec, PmIngestSummary, validate_spec
from queuetruth.pm.ownfree import OwnFreeStats, remove_own
from queuetruth.pm.polymarket import read_polymarket
from queuetruth.pm.tables import table
from queuetruth.pm.terms import TERMS_NOTICE
from queuetruth.pm.units import ONE, price_from_fraction, ratio_price
from queuetruth.pm.venue import ANOMALY_COUNTERS, VenueCapture
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.schemas.runid import run_id, spec_echo
from queuetruth.score._io import read_live_log
from queuetruth.score.l2_own import own_orders_from_log

DEFAULT_MATCH_WINDOW_NS = {"kalshi": 1_000_000_000, "polymarket": 2_000_000_000}
#: The live-log builder's note that fills derived from cumulative quantities lost to per-fill records.
_DERIVED = re.compile(r"^\w+: (\d+) fill\(s\) derived from cumulative quantities dropped where per-fill records exist$")


def _tick(cap: VenueCapture, symbol: str) -> int:
    """Tick size (price units) of a market: its grid's finest step, else the coarsest step (<= $0.01) every
    captured price lies on."""
    info = cap.markets[symbol]
    if info.grid is not None:
        return info.grid.finest
    g = 0
    for m in cap.book.get(symbol, []):
        for _s, p, _q in m.levels:
            g = math.gcd(g, p)
    for t in cap.trades.get(symbol, []):
        g = math.gcd(g, t.price)
    g = math.gcd(g, ONE)
    return math.gcd(g, 100) if g else 100


def _resolve(ref: dict[str, Any]) -> str:
    p = Path(ref["path"])
    if p.is_file():
        return sha256_file(p)
    raise QtError("QT-INPUT-001", f"{p}: input not found")


def _write_json(path: Path, doc: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return {"name": path.name, "rows": None, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def ingest(spec: PmIngestSpec | dict[str, Any]) -> PmIngestSummary:
    """Runs `pm ingest` (see the module docstring)."""
    sp = validate_spec(PmIngestSpec, spec)
    if not sp.out:
        raise QtError("QT-CONFIG-001", "out: an output directory is required")
    out = Path(sp.out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise QtError(
            "QT-CONFIG-001",
            f"out: {out} exists and is not empty",
            "write each ingest to a new directory (a second run would mix its files with the first's)",
        )
    paths = [r.path for r in sp.inputs]
    window = sp.match_window_ns if sp.match_window_ns is not None else DEFAULT_MATCH_WINDOW_NS[sp.venue]
    own_window = sp.own_match_window_ns if sp.own_match_window_ns is not None else window
    tx_times: dict[str, int] = {}
    chain: ChainDecode | None = None
    if sp.venue == "kalshi":
        if sp.chain:
            raise QtError("QT-CONFIG-001", "chain: on-chain exports are Polymarket only")
        if sp.markets:
            raise QtError("QT-CONFIG-001", "markets: YES / NO token maps are Polymarket only")
        cap = read_kalshi(paths, account=sp.account, yes_price=sp.kalshi_yes_price)
    else:
        if sp.kalshi_yes_price != "auto":
            raise QtError("QT-CONFIG-001", "kalshi_yes_price applies to venue kalshi only")
        cap, tx_times = read_polymarket(paths, account=sp.account, markets=dict(sp.markets))
        if sp.chain:
            chain = decode([r.path for r in sp.chain], sp.exchanges)
    b = cap.builder
    assert b is not None
    res = b.finish()
    own_symbols = {str(o["instrument"]) for o in res.orders if o.get("instrument")}
    symbols = sorted(s for s in cap.markets if cap.book.get(s) or cap.trades.get(s) or s in own_symbols)
    if not symbols:
        raise QtError("QT-INPUT-002", f"no {sp.venue} market data or own orders found in the inputs")
    ids = _instrument_ids(symbols, sp.instrument_ids)
    instruments = {
        s: Instrument(
            ids[s],
            s,
            _tick(cap, s),
            cap.markets[s].size_exp,
            currency=cap.markets[s].currency,
        )
        for s in symbols
    }
    series: list[Series] = []
    batches_of: dict[str, list[Batch]] = {}
    for s in symbols:
        b, st = build_batches(cap.book.get(s, []), cap.trades.get(s, []), match_window_ns=window)
        batches_of[s] = b
        series.append(Series(instruments[s], b, st))
    for srs in series:
        for k in ("rest_book_checks", "rest_book_mismatches", "rest_book_mismatch_levels", "rest_book_unchecked"):
            if getattr(srs.stats, k):
                cap.count(k, getattr(srs.stats, k))
    reader_anomalies = {k: cap.counters[k] for k in ANOMALY_COUNTERS if cap.counters.get(k)}
    source_format = f"pm_{sp.venue}"
    files: list[dict[str, Any]] = []
    m_book, dq_book, f_book = write_dataset(
        out / "book",
        series,
        source_format=source_format,
        source_venue=sp.venue,
        inputs=paths,
        sequence_gaps=cap.sequence_gaps,
        sequence_regressions=cap.sequence_regressions,
        extra_anomalies=reader_anomalies,
    )
    files += [{**f, "name": f"book/{f['name']}"} for f in f_book]
    summary: dict[str, Any] = {
        "venue": sp.venue,
        "terms": TERMS_NOTICE,
        "book": {
            "dataset_id": m_book.dataset_id,
            "records": dq_book.records,
            "verdict": dq_book.verdict,
            "reasons": list(dq_book.reasons),
        },
        "book_own_free": None,
        "live": None,
        "own_free": None,
        "queue_samples": 0,
        "chain": None,
    }
    # ---- own orders ----------------------------------------------------------------------------------------------
    for o in res.orders:
        o["instrument_id"] = ids.get(str(o.get("instrument")))
    # fills derived from cumulative counts lose to the venue's own fill messages: expected when both are captured,
    # so they are counted (derived_fills_superseded) rather than warned about once per order
    warnings = list(cap.warnings) + [w for w in res.warnings if not _DERIVED.match(w)]
    for s, srs in zip(symbols, series, strict=True):
        flipped = srs.stats.trades_other_side_explained
        if flipped and flipped >= max(5, srs.stats.trades // 5):
            warnings.append(
                f"{s}: {flipped} of {srs.stats.trades} prints match no book decrease on the side they hit but would "
                "on the other side: check the venue's trade side convention (the reader takes it as the taker's)"
            )
    kept = sum(1 for e in res.events if e["event_type"] == "fill" and str(e.get("reason") or "").startswith("derived"))
    if cap.counters.get("derived_fills", 0) > kept:
        cap.count("derived_fills_superseded", cap.counters["derived_fills"] - kept)
    own_keys: dict[str, str] = {}
    if res.orders:
        live_dir = out / "live"
        lf = write_canonical_log(live_dir, res.orders, res.events)
        files += [{**f.model_dump(), "name": f"live/{f.name}"} for f in lf]
        orders, events = read_live_log(live_dir)
        own, own_warn = own_orders_from_log(orders, events)
        warnings += own_warn
        summary["live"] = {
            "n_orders": len(orders),
            "n_events": len(events),
            "n_fills": sum(1 for e in events if e["event_type"] == "fill"),
            "dedupe_dropped": res.dedupe_dropped,
        }
        own_keys = {str(o["venue_order_id"]).lower(): str(o["order_key"]) for o in orders if o.get("venue_order_id")}
        cids: dict[str, set[str]] = {}
        for o in orders:
            vid = o.get("venue_order_id")
            if vid and vid in cap.cids:
                cids[str(o["order_key"])] = set(cap.cids[vid])
        stats = OwnFreeStats()
        free_series: list[Series] = []
        entry_rows: list[dict[str, Any]] = []
        for s in symbols:
            mine = [o for o in own if o.instrument_id == ids[s]]
            r = remove_own(batches_of[s], mine, cids, own_window)
            stats.add(r.stats)
            free_series.append(Series(instruments[s], r.batches, series[symbols.index(s)].stats))
            entry_rows += [
                {
                    "order_key": e.key,
                    "instrument_id": ids[s],
                    "side": e.side,
                    "price": e.price,
                    "seq": e.seq,
                    "inside": e.inside,
                    "ts_event": e.ts_event,
                    "how": e.how,
                    "qty": e.qty,
                    "end_seq": e.end_seq,
                    "end_inside": e.end_inside,
                }
                for e in r.entries
            ]
        m_free, dq_free, f_free = write_dataset(
            out / "book_own_free",
            free_series,
            source_format=source_format,
            source_venue=sp.venue,
            inputs=paths,
            sequence_gaps=cap.sequence_gaps,
            sequence_regressions=cap.sequence_regressions,
            extra_anomalies={**reader_anomalies, "own_size_clamped": stats.clamps},
        )
        files += [{**f, "name": f"book_own_free/{f['name']}"} for f in f_free]
        summary["book_own_free"] = {
            "dataset_id": m_free.dataset_id,
            "records": dq_free.records,
            "verdict": dq_free.verdict,
            "reasons": list(dq_free.reasons),
        }
        summary["own_free"] = asdict(stats)
        ent = write_table(table("qt.pm_own_entries", entry_rows), out / "pm" / "own_entries.parquet")
        files.append({**ent, "name": "pm/own_entries.parquet"})
        if stats.unaligned or stats.outside:
            warnings.append(
                f"own orders: {stats.unaligned} resting change(s) matched no book change within {own_window} ns and "
                f"{stats.outside} fell after the book data; their queue entry points are approximate"
            )
        if stats.clamps:
            warnings.append(f"own orders: {stats.clamps} level size(s) smaller than the own resting size were clamped")
    # ---- queue samples -------------------------------------------------------------------------------------------
    if cap.samples:
        key_by_vid = {k: v for k, v in own_keys.items()}
        rows = []
        for q in cap.samples:
            key = key_by_vid.get(q.order_id.lower())
            rows.append(
                {
                    "order_key": key,
                    "venue_order_id": q.order_id,
                    "instrument_id": ids.get(q.market) if q.market else None,
                    "queue_position": q.queue_position,
                    "ts_local_recv": q.recv_ns,
                    "ts_local_send": q.send_ns,
                    "location": q.location,
                }
            )
        unknown = sum(1 for r in rows if r["order_key"] is None)
        if unknown:
            warnings.append(f"queue positions: {unknown} sample(s) name orders that are not in the own-order log")
        ent = write_table(table("qt.pm_queue_samples", rows), out / "pm" / "queue_samples.parquet")
        files.append({**ent, "name": "pm/queue_samples.parquet"})
        summary["queue_samples"] = len(rows)
    # ---- chain ---------------------------------------------------------------------------------------------------
    if chain is not None:
        by_iid = {ids[s]: batches_of[s] for s in symbols}
        summary["chain"], chain_files, chain_warn = _chain_tables(chain, cap, ids, own_keys, tx_times, by_iid, out)
        files += chain_files
        warnings += chain_warn
    # ---- markets and summary -------------------------------------------------------------------------------------
    markets_doc = {
        "schema": "qt.pm_markets",
        "schema_version": 1,
        "venue": sp.venue,
        "terms": TERMS_NOTICE,
        "markets": [
            {**cap.markets[s].describe(), "instrument_id": ids[s], "tick_size": instruments[s].tick_size}
            for s in symbols
        ],
    }
    files.append(_write_json(out / "markets.json", markets_doc))
    spec_doc = sp.model_dump(mode="json")
    summary_doc = {
        "run_id": run_id(spec_doc, __version__, _resolve),
        "spec": spec_echo(spec_doc),
        **summary,
        "instruments": [
            {
                "instrument_id": ids[s],
                "symbol": s,
                "tick_size": instruments[s].tick_size,
                "size_exp": instruments[s].size_exp,
                "batches": len(batches_of[s]),
                "trades": series[i].stats.trades,
                "trades_matched": series[i].stats.trades_matched,
                "trades_unmatched": series[i].stats.trades_unmatched,
                "block_trades": series[i].stats.block_trades,
                "feed_gaps": len(series[i].stats.feed_gaps),
            }
            for i, s in enumerate(symbols)
        ],
        "counters": dict(sorted(cap.counters.items())),
        "warnings": warnings,
        "files": sorted(files, key=lambda f: f["name"]),
    }
    doc = PmIngestSummary.model_validate(summary_doc)
    _write_json(out / "pm_ingest.json", doc.model_dump(mode="json"))
    return doc


def _instrument_ids(symbols: list[str], given: dict[str, int]) -> dict[str, int]:
    if not given:
        return {s: i for i, s in enumerate(symbols, start=1)}
    missing = [s for s in symbols if s not in given]
    if missing:
        raise QtError("QT-CONFIG-001", f"instrument_ids: no id for {missing}")
    vals = [given[s] for s in symbols]
    if len(set(vals)) != len(vals) or any(v < 0 or v > 2**32 - 1 for v in vals):
        raise QtError("QT-CONFIG-001", "instrument_ids: ids must be distinct uint32 values")
    return {s: int(given[s]) for s in symbols}


def _chain_tables(
    chain: ChainDecode,
    cap: VenueCapture,
    ids: dict[str, int],
    own_keys: dict[str, str],
    tx_times: dict[str, int],
    batches: dict[int, list[Batch]],
    out: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """`chain_fills`, `filled_orders` and `chain_queue` tables and the chain part of the summary."""
    token: dict[str, tuple[str, bool]] = {}
    for sym, info in cap.markets.items():
        if info.yes_asset:
            token[str(info.yes_asset)] = (sym, True)
        if info.no_asset:
            token[str(info.no_asset)] = (sym, False)
    rows: list[dict[str, Any]] = []
    inexact = unknown_token = 0
    for f in chain.fills:
        tid = str(f.token_id)
        hit = token.get(tid)
        sym, is_yes = hit if hit else (None, None)
        if hit is None:
            unknown_token += 1
        price = canon = None
        exact = True
        if f.tokens > 0:
            # a maker fills at its own limit price; a taker's row is the average over the makers it matched
            p_units, exact = price_from_fraction(ratio_price(f.collateral, f.tokens), "fill price", f.tx)
            if not exact and f.role == "maker":
                inexact += 1
            if is_yes is not None:
                buy = f.side == "BUY"
                canon_buy = buy if is_yes else not buy
                canon = 66 if canon_buy else 65
                price = p_units if is_yes else ONE - p_units
        rows.append(
            {
                "tx": f.tx,
                "block": f.block,
                "log_index": f.log_index,
                "ts_block": f.block_ts_ns,
                "ts_match": tx_times.get(f.tx),
                "exchange": f.exchange,
                "version": f.version,
                "role": f.role,
                "match_idx": f.match_idx,
                "maker_pos": f.maker_pos,
                "order_hash": f.order_hash,
                "maker": f.maker,
                "taker": f.taker,
                "side": f.side,
                "token_id": tid,
                "outcome": None if is_yes is None else ("yes" if is_yes else "no"),
                "instrument_id": ids.get(sym) if sym else None,
                "canon_side": canon,
                "price": price,
                "price_exact": exact,
                "size": f.tokens,
                "collateral": f.collateral,
                "fee": f.fee,
                "fee_asset": f.fee_asset,
                "own": f.order_hash.lower() in own_keys,
            }
        )
    files = []
    ent = write_table(table("qt.pm_chain_fills", rows), out / "pm" / "chain_fills.parquet")
    files.append({**ent, "name": "pm/chain_fills.parquet"})
    orders: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["role"] != "maker":
            continue
        o = orders.get(r["order_hash"])
        if o is None:
            o = {
                "order_hash": r["order_hash"],
                "maker": r["maker"],
                "instrument_id": r["instrument_id"],
                "canon_side": r["canon_side"],
                "price": r["price"],
                "n_fills": 0,
                "filled": 0,
                "first_block": r["block"],
                "first_log_index": r["log_index"],
                "last_block": r["block"],
                "ts_first_match": r["ts_match"],
                "own": r["own"],
                "price_consistent": True,
            }
            orders[r["order_hash"]] = o
        o["n_fills"] += 1
        o["filled"] += r["size"]
        o["last_block"] = r["block"]
        if r["price"] != o["price"]:
            o["price_consistent"] = False
    ordered = sorted(orders.values(), key=lambda o: (o["first_block"], o["first_log_index"]))
    ent = write_table(table("qt.pm_filled_orders", ordered), out / "pm" / "filled_orders.parquet")
    files.append({**ent, "name": "pm/filled_orders.parquet"})
    checked, mismatched = print_sides(chain, token, ids, batches)
    queue_rows, unplaced, no_hash = chain_queue(rows, batches, own_keys)
    if queue_rows:
        ent = write_table(table("qt.pm_chain_queue", queue_rows), out / "pm" / "chain_queue.parquet")
        files.append({**ent, "name": "pm/chain_queue.parquet"})
    makers = [r for r in rows if r["role"] == "maker"]
    summary = {
        "logs": chain.logs,
        "removed_logs": chain.removed,
        "duplicate_logs": chain.duplicate_logs,
        "other_events": chain.other_events,
        "fills": len(rows),
        "maker_fills": len(makers),
        "taker_fills": sum(1 for r in rows if r["role"] == "taker"),
        "operator_fills": chain.operator_fills,
        "matches": len(chain.matches),
        "filled_orders": len(ordered),
        "own_maker_fills": sum(1 for r in makers if r["own"]),
        "fills_with_ws_time": sum(1 for r in rows if r["ts_match"] is not None),
        "inexact_prices": inexact,
        "unknown_tokens": unknown_token,
        "inconsistent_order_prices": sum(1 for o in ordered if not o["price_consistent"]),
        "queue_truth_orders": len(queue_rows),
        "queue_truth_unplaced": unplaced,
        "prints_side_checked": checked,
        "prints_side_mismatch": mismatched,
    }
    warn = list(chain.warnings)
    if mismatched:
        warn.append(
            f"chain: {mismatched} of {checked} print(s) with a transaction hash name an aggressor side that no taker "
            "order of that transaction had: the market channel's trade side is read as the taker's side"
        )
    if unknown_token:
        warn.append(f"chain: {unknown_token} fill(s) on tokens of markets without metadata (not mapped to a ladder)")
    if inexact:
        warn.append(f"chain: {inexact} maker fill price(s) are not on the 0.0001 grid (rounded half-up)")
    if unplaced:
        warn.append(
            f"chain: {unplaced} own order(s) with an on-chain fill have no print of that transaction in the book "
            f"({no_hash} print(s) carry no transaction hash); no queue truth for them"
        )
    return summary, files, warn


def print_sides(
    chain: ChainDecode, token: dict[str, tuple[str, bool]], ids: dict[str, int], batches: dict[int, list[Batch]]
) -> tuple[int, int]:
    """(prints checked, prints whose aggressor no taker order of their transaction had). The market channel's
    `last_trade_price.side` is documented only as BUY or SELL; the reader takes it as the taker's side. The chain
    names each match's taker order and its side (`OrdersMatched`), so every print carrying a transaction hash of the
    export is checked against the canonical taker sides (a BUY of YES or a SELL of NO buys YES) of that
    transaction's matches on the same market."""
    sides: dict[tuple[str, int], set[int]] = {}
    for m in chain.matches:
        hit = token.get(str(m.taker_token_id))
        if hit is None or hit[0] not in ids:
            continue
        sym, is_yes = hit
        buys_yes = (m.taker_side == "BUY") == is_yes
        sides.setdefault((m.tx.lower(), ids[sym]), set()).add(66 if buys_yes else 65)
    checked = mismatched = 0
    for iid, bs in batches.items():
        for b in bs:
            for t in b.trades:
                want = sides.get((t.tx_hash.lower(), iid)) if t.tx_hash else None
                if want is None:
                    continue
                checked += 1
                mismatched += int(t.aggressor not in want)
    return checked, mismatched


def chain_queue(
    rows: list[dict[str, Any]], batches: dict[int, list[Batch]], own_keys: dict[str, str]
) -> tuple[list[dict[str, Any]], int, int]:
    """`qt.pm_chain_queue` rows (see the module docstring), the number of own orders with a maker fill whose
    print is in no published batch, and the number of prints without a transaction hash.

    `rows` are `qt.pm_chain_fills` rows (dicts), `batches` the published batches per instrument id, `own_keys` the
    own order ids (lower case) -> order key."""
    makers = sorted((r for r in rows if r["role"] == "maker"), key=lambda r: (r["block"], r["log_index"]))
    by_tx: dict[str, list[dict[str, Any]]] = {}
    for r in makers:
        by_tx.setdefault(r["tx"].lower(), []).append(r)
    where: dict[tuple[int, str, int, int], int] = {}
    no_hash = 0
    for iid, bs in batches.items():
        for k, b in enumerate(bs):
            for t in b.trades:
                if not t.tx_hash:
                    no_hash += 1
                    continue
                where.setdefault((iid, t.tx_hash.lower(), t.hit_side, t.price), k)
    first: dict[str, dict[str, Any]] = {}
    for r in makers:
        h = r["order_hash"].lower()
        if h in own_keys and h not in first:
            first[h] = r
    out: list[dict[str, Any]] = []
    unplaced = 0
    for h, f in first.items():
        iid, side, price = f["instrument_id"], f["canon_side"], f["price"]
        k = where.get((iid, f["tx"].lower(), side, price)) if None not in (iid, side, price) else None
        if k is None:
            unplaced += 1
            continue
        b = batches[iid][k]
        txs = sorted({t.tx_hash.lower() for t in b.trades if t.tx_hash and t.hit_side == side and t.price == price})
        pos = (f["block"], f["log_index"])
        ahead = [
            r
            for tx in txs
            for r in by_tx.get(tx, [])
            if (r["instrument_id"], r["canon_side"], r["price"]) == (iid, side, price)
            and (r["block"], r["log_index"]) < pos
        ]
        out.append(
            {
                "order_key": own_keys[h],
                "order_hash": f["order_hash"],
                "instrument_id": iid,
                "side": side,
                "price": price,
                "seq": b.seq,
                "ts_event": b.ts_event,
                "tx": f["tx"],
                "block": f["block"],
                "log_index": f["log_index"],
                "ahead_others": sum(int(r["size"]) for r in ahead if not r["own"]),
                "ahead_own": sum(int(r["size"]) for r in ahead if r["own"]),
                "makers_ahead": len(ahead),
            }
        )
    out.sort(key=lambda r: (r["block"], r["log_index"]))
    return out, unplaced, no_hash


__all__ = ["DEFAULT_MATCH_WINDOW_NS", "chain_queue", "ingest", "print_sides"]
