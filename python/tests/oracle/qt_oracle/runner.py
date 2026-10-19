"""Dataset-level driver: shadow/amend readers, partition routing and the end-to-end oracle run."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .engine import NS, Amend, OracleConfig, PartitionRunner, ShadowSpec
from .tape import Partition
from .venue import venue_facts


def _ns(v) -> int | None:
    if v is None or v == "":
        return None
    if hasattr(v, "value"):
        return int(v.value)
    return int(v)


def _rows_parquet(path: Path) -> list[dict[str, Any]]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    t = pq.read_table(path)
    cols = {}
    for name in t.column_names:
        c = t.column(name)
        if pa.types.is_timestamp(c.type):
            c = c.cast(pa.int64())
        elif pa.types.is_dictionary(c.type):
            c = c.cast(c.type.value_type)
        cols[name] = c.to_pylist()
    return [{k: v[i] for k, v in cols.items()} for i in range(t.num_rows)]


def _side(v) -> str:
    s = v if isinstance(v, str) else chr(int(v))
    return {"buy": "B", "sell": "A", "S": "A"}.get(s, s)


def read_shadows(path: str | Path) -> list[ShadowSpec]:
    """qt.shadow_spec Parquet, et shadows.csv (abs prices) or a JSON list of ShadowSpec fields."""
    path = Path(path)
    if path.suffix == ".parquet":
        rows = _rows_parquet(path)
    elif path.suffix == ".json":
        rows = json.loads(path.read_text())
    else:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        mode = r.get("price_mode") or "abs"
        price = r.get("price")
        out.append(
            ShadowSpec(
                shadow_id=str(r["shadow_id"]),
                instrument_id=int(r["instrument_id"]),
                side=_side(r["side"]),
                qty=int(r["qty"]),
                ts_submit=_ns(r["ts_submit"]),
                price=None if price in (None, "") or mode == "touch" else int(price),
                price_mode=mode,
                offset_ticks=int(r.get("offset_ticks") or 0),
                latency_ns=int(r.get("latency_ns") or 0),
                ts_cancel=_ns(r.get("ts_cancel")),
            )
        )
    return out


def read_amends(path: str | Path | None) -> dict[str, list[Amend]]:
    if path is None:
        return {}
    path = Path(path)
    if path.suffix == ".parquet":
        rows = _rows_parquet(path)
    elif path.suffix == ".json":
        rows = json.loads(path.read_text())
    else:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    out: dict[str, list[Amend]] = {}
    for r in rows:
        out.setdefault(str(r["shadow_id"]), []).append(Amend(str(r["shadow_id"]), _ns(r["ts_send"]), int(r["new_qty"])))
    return out


def read_order_ids(path: str | Path) -> frozenset[int]:
    """Order ids to exclude (ruling A1): a text file with one id per line, or Parquet with a `venue_order_id` or
    `order_id` column (e.g. a canonical live log's orders.parquet)."""
    path = Path(path)
    if path.suffix == ".parquet":
        rows = _rows_parquet(path)
        key = "venue_order_id" if rows and "venue_order_id" in rows[0] else "order_id"
        return frozenset(int(r[key]) for r in rows if r.get(key) not in (None, ""))
    return frozenset(int(x) for x in path.read_text().split() if x.strip())


def route_shadows(
    parts: list[Partition], shadows: list[ShadowSpec]
) -> tuple[dict[int, list[ShadowSpec]], list[ShadowSpec]]:
    """Partition index -> shadows: same instrument, the partition with the greatest local midnight <= ts_submit
    (and at most 25 h after it). Shadows without a partition are returned separately."""
    by_inst: dict[int, list[tuple[int, int]]] = {}
    for i, p in enumerate(parts):
        by_inst.setdefault(p.meta.instrument_id, []).append((p.meta.midnight_utc_ns, i))
    for v in by_inst.values():
        v.sort()
    routed: dict[int, list[ShadowSpec]] = {}
    orphans = []
    for s in shadows:
        cands = by_inst.get(s.instrument_id, [])
        best = None
        for mid, i in cands:
            if (mid <= s.ts_submit and (s.ts_submit - mid) < 25 * 3600 * NS) or len(cands) == 1:
                best = i
        if best is None:
            orphans.append(s)
        else:
            routed.setdefault(best, []).append(s)
    return routed, orphans


def run_oracle(
    parts: list[Partition],
    shadows: list[ShadowSpec],
    amends: dict[str, list[Amend]] | None = None,
    cfg: OracleConfig | None = None,
    *,
    cross_window_s: float = 1.0,
    progress=None,
) -> dict[str, Any]:
    """Runs every shadow; returns {"results": [...], "stats": {...}, "orphans": [...]}."""
    cfg = cfg or OracleConfig()
    routed, orphans = route_shadows(parts, shadows)
    results: list[dict[str, Any]] = []
    stats: dict[str, int] = {}
    for i, p in enumerate(parts):
        sh = routed.get(i)
        if not sh:
            continue
        facts = venue_facts(p.meta.venue, open_s=p.meta.open_s, close_s=p.meta.close_s, cross_window_s=cross_window_s)
        runner = PartitionRunner(p.meta, sh, amends or {}, cfg, facts, p.cross_sequences)
        results.extend(runner.run(p.iter_records()))
        for k, v in runner.stats.items():
            stats[k] = stats.get(k, 0) + v
        if progress:
            progress(
                f"partition {p.meta.session_date}/{p.meta.instrument_id}: {len(sh)} shadows, "
                f"{runner.stats['records']} records"
            )
    for s in orphans:
        results.append(
            {
                "shadow_id": s.shadow_id,
                "instrument_id": s.instrument_id,
                "side": s.side,
                "price": s.price,
                "qty": s.qty,
                "qty_final": s.qty,
                "ts_submit": s.ts_submit,
                "ts_live": s.ts_submit + s.latency_ns,
                "ts_activated_event": None,
                "ts_cancel_effective": None,
                "queue_ahead_at_live": None,
                "rank_at_live": None,
                "level_qty_at_live": None,
                "filled_qty": 0,
                "n_fills": 0,
                "ts_first_fill": None,
                "ts_last_fill": None,
                "ts_full_fill": None,
                "first_fill_reason": "never",
                "first_fill_liquidity": None,
                "queue_ahead_at_first_fill": None,
                "end_state": "censored_gap",
                "ts_end": None,
                "fills": [],
                "trajectory": [],
                "notes": [],
                "diag": {"no_partition": True},
            }
        )
    return {"results": results, "stats": stats, "orphans": [s.shadow_id for s in orphans]}


def run_et_csv_stream(
    mbo_csv: str | Path,
    shadows: list[ShadowSpec],
    amends: dict[str, list[Amend]] | None = None,
    cfg: OracleConfig | None = None,
    *,
    venue: str,
    midnight_utc_ns: int,
    open_s: int,
    close_s: int,
    tick_size: int,
    session_date: str = "unknown",
    instrument_ids: set[int] | None = None,
    cross_window_s: float = 1.0,
    progress=None,
) -> dict[str, Any]:
    """One streaming pass over an et `mbo.csv` (instruments interleaved, one session): a runner and an event
    buffer per instrument that has shadows; memory stays at the books, not the file."""
    from .tape import FLAG_LAST, UNDEF_PRICE, InstrumentMeta, Rec

    cfg = cfg or OracleConfig()
    by_inst: dict[int, list[ShadowSpec]] = {}
    for s in shadows:
        if instrument_ids is None or s.instrument_id in instrument_ids:
            by_inst.setdefault(s.instrument_id, []).append(s)
    runners: dict[int, PartitionRunner] = {}
    bufs: dict[int, list] = {}
    for inst, sh in by_inst.items():
        meta = InstrumentMeta(inst, session_date, venue, tick_size, midnight_utc_ns, open_s, close_s)
        facts = venue_facts(venue, open_s=open_s, close_s=close_s, cross_window_s=cross_window_s)
        runners[inst] = PartitionRunner(meta, sh, amends or {}, cfg, facts)
        bufs[inst] = []
    n = 0
    with open(mbo_csv, newline="") as f:
        rd = csv.reader(f)
        ix = {name: i for i, name in enumerate(next(rd))}
        i_ts, i_inst, i_act, i_side = ix["ts_event"], ix["instrument_id"], ix["action"], ix["side"]
        i_px, i_sz, i_id, i_fl, i_seq = ix["price"], ix["size"], ix["order_id"], ix["flags"], ix["sequence"]
        for row in rd:
            inst = int(row[i_inst])
            buf = bufs.get(inst)
            if buf is None:
                continue
            px = row[i_px]
            fl = int(row[i_fl] or 0)
            buf.append(
                Rec(
                    int(row[i_ts]),
                    inst,
                    row[i_act],
                    row[i_side],
                    UNDEF_PRICE if px == "" else int(px),
                    int(row[i_sz]),
                    int(row[i_id] or 0),
                    fl,
                    int(row[i_seq] or 0),
                )
            )
            if fl & FLAG_LAST:
                runners[inst].push_event(buf)
                bufs[inst] = []
            n += 1
            if progress and n % 1_000_000 == 0:
                progress(f"{n} records")
    results: list[dict[str, Any]] = []
    stats: dict[str, int] = {}
    for inst, r in runners.items():
        if bufs[inst]:
            r.push_event(bufs[inst])
        results.extend(r.finish())
        for k, v in r.stats.items():
            stats[k] = stats.get(k, 0) + v
    return {"results": results, "stats": stats, "orphans": []}
