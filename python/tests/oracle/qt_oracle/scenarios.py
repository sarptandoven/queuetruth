"""Hand-built tapes in the SPEC 6.2 notation, and a writer for qt dataset directories (integration tests).

Event lines:
- `A o1 B 100 60` add, `C o1 60` remove 60, `M o1 B 100 70` modify, `R` clear,
- `T A 100 60` print (aggressor side, price, size), `T N 100 500` side-N print,
- `F o1 60` execution of resting o1 (price = o1's current price; `F o1 60 @99` sets the record price).
Every record gets `sequence` = its 1-based index in the stream (SPEC 6.2), or one sequence per event with
`seq_per_event=True` (ITCH: one message per event). The last record of each event carries LAST. Prices are integers
in ticks of 1 unless scaled by the caller.
"""

from __future__ import annotations

import json
from pathlib import Path

from .engine import Amend, ShadowSpec
from .tape import FLAG_LAST, UNDEF_PRICE, InstrumentMeta, Rec


def _oid(tok: str) -> int:
    return int(tok[1:]) if tok[0].isalpha() else int(tok)


def build_tape(
    events: list[tuple[int, list[str]]], *, instrument_id: int = 1, seq_per_event: bool = False, price_scale: int = 1
) -> list[Rec]:
    recs: list[Rec] = []
    px_of: dict[int, tuple[str, int]] = {}
    n = 0
    for ev_i, (ts, lines) in enumerate(events):
        start = len(recs)
        for line in lines:
            t = line.split()
            n += 1
            seq = ev_i + 1 if seq_per_event else n
            act = t[0]
            if act == "A" or act == "M":
                oid, side, px, sz = _oid(t[1]), t[2], int(t[3]) * price_scale, int(t[4])
                px_of[oid] = (side, px)
                recs.append(Rec(ts, instrument_id, act, side, px, sz, oid, 0, seq))
            elif act == "C":
                oid, sz = _oid(t[1]), int(t[2])
                side, px = px_of.get(oid, ("N", UNDEF_PRICE))
                recs.append(Rec(ts, instrument_id, "C", side, px, sz, oid, 0, seq))
            elif act == "F":
                oid, sz = _oid(t[1]), int(t[2])
                side, px = px_of.get(oid, ("N", UNDEF_PRICE))
                if len(t) > 3 and t[3].startswith("@"):
                    px = int(t[3][1:]) * price_scale
                recs.append(Rec(ts, instrument_id, "F", side, px, sz, oid, 0, seq))
            elif act == "T":
                recs.append(Rec(ts, instrument_id, "T", t[1], int(t[2]) * price_scale, int(t[3]), 0, 0, seq))
            elif act == "R":
                recs.append(Rec(ts, instrument_id, "R", "N", UNDEF_PRICE, 0, 0, 0, seq))
            else:
                raise ValueError(f"bad line {line!r}")
        if len(recs) > start:
            recs[-1].flags |= FLAG_LAST
    return recs


def meta_for(
    venue: str = "cme",
    *,
    tick_size: int = 1,
    instrument_id: int = 1,
    close_s: int = 86_400,
    open_s: int = 34_200,
    midnight_utc_ns: int = 0,
) -> InstrumentMeta:
    """Scenario instrument (SPEC 6.2: tick 1, midnight 0, close 86,400 s). `open_s` only places the Nasdaq
    cross-print windows; scenario timestamps (tens of ns) are far from them."""
    return InstrumentMeta(instrument_id, "1970-01-01", venue, tick_size, midnight_utc_ns, open_s, close_s)


def shadow(
    sid: str = "X",
    side: str = "B",
    price: int | None = 100,
    qty: int = 10,
    ts_submit: int = 15,
    *,
    latency_ns: int = 0,
    ts_cancel: int | None = None,
    price_mode: str = "abs",
    offset_ticks: int = 0,
    instrument_id: int = 1,
) -> ShadowSpec:
    return ShadowSpec(
        sid,
        instrument_id,
        side,
        qty,
        ts_submit,
        price if price_mode == "abs" else None,
        price_mode,
        offset_ticks,
        latency_ns,
        ts_cancel,
    )


def amend(sid: str, ts_send: int, new_qty: int) -> Amend:
    return Amend(sid, ts_send, new_qty)


# ----------------------------------------------------------------- qt dataset writer (integration tests)


def write_qt_dataset(
    root: str | Path,
    records: list[Rec],
    meta: InstrumentMeta,
    *,
    session_date: str = "1970-01-01",
    symbol: str = "ORCL1",
) -> Path:
    """Writes a one-partition qt dataset (SPEC 3.3) with queuetruth's own Arrow schemas."""
    import datetime as dt

    import pyarrow as pa

    from queuetruth.schemas.arrow import SCHEMAS
    from queuetruth.schemas.io import sha256_file, write_table

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    date = dt.date.fromisoformat(session_date)
    days = (date - dt.date(1970, 1, 1)).days
    inst = {
        "instrument_id": [meta.instrument_id],
        "session_date": [date],
        "symbol": [symbol],
        "venue": [meta.venue],
        "source_venue": [meta.venue],
        "price_exp": [-9],
        "tick_size": [meta.tick_size],
        "lot_size": [1],
        "size_exp": [0],
        "contract_multiplier": [1.0],
        "currency": ["USD"],
        "session_tz": ["UTC"],
        "midnight_utc_ns": [meta.midnight_utc_ns],
        "open_s": [meta.open_s],
        "close_s": [meta.close_s],
        "source": ["oracle_scenario"],
    }
    write_table(pa.table(inst).cast(SCHEMAS["qt.instruments"]), root / "instruments.parquet", SCHEMAS["qt.instruments"])
    cols = {
        k: []
        for k in (
            "ts_event",
            "ts_recv",
            "instrument_id",
            "action",
            "side",
            "price",
            "size",
            "order_id",
            "flags",
            "sequence",
            "channel_id",
            "publisher_id",
        )
    }
    for r in records:
        cols["ts_event"].append(r.ts_event)
        cols["ts_recv"].append(r.ts_event)
        cols["instrument_id"].append(r.instrument_id)
        cols["action"].append(ord(r.action))
        cols["side"].append(ord(r.side))
        cols["price"].append(r.price)
        cols["size"].append(r.size)
        cols["order_id"].append(r.order_id)
        cols["flags"].append(r.flags)
        cols["sequence"].append(r.sequence)
        cols["channel_id"].append(0)
        cols["publisher_id"].append(0)
    sch = SCHEMAS["qt.mbo"]
    arrays = []
    for f in sch:
        v = cols[f.name]
        if pa.types.is_timestamp(f.type):
            arrays.append(pa.array(v, pa.int64()).cast(f.type))
        else:
            arrays.append(pa.array(v, f.type))
    rel = f"mbo/date={session_date}/instrument_id={meta.instrument_id}/part-00000.parquet"
    fe = write_table(pa.Table.from_arrays(arrays, schema=sch), root / rel, sch)
    sha = fe["sha256"]
    import hashlib

    dataset_id = hashlib.sha256(f"{sha}\nsynthetic".encode()).hexdigest()
    ts = [r.ts_event for r in records] or [0]
    manifest = {
        "schema": "qt.dataset_manifest",
        "schema_version": 1,
        "dataset_id": dataset_id,
        "kind": "mbo",
        "source_format": "synthetic",
        "venue": meta.venue,
        "source_venue": meta.venue,
        "inputs": [],
        "partitions": [
            {
                "instrument_id": meta.instrument_id,
                "session_date": days,
                "files": [{"path": rel, "rows": len(records), "sha256": sha}],
                "rows": len(records),
                "ts_min": min(ts),
                "ts_max": max(ts),
            }
        ],
        "price_exp_by_instrument": {str(meta.instrument_id): -9},
        "created_by": "qt_oracle scenarios",
        "validation_status": "fixture_only",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    dq = {
        "schema": "qt.data_quality",
        "schema_version": 1,
        "records": len(records),
        "maybe_bad_book_records": 0,
        "bad_ts_recv_records": 0,
        "snapshot_records": 0,
        "pre_snapshot_rows_skipped": 0,
        "unknown_order_refs": 0,
        "duplicate_adds": 0,
        "unexpected_modifies": 0,
        "sequence_gaps": 0,
        "sequence_regressions": 0,
        "out_of_order_ts_event": 0,
        "crossed_books_after_last": 0,
        "hidden_prints": 0,
        "off_price_executions": 0,
        "feed_gaps": [],
        "source_specific": {},
        "verdict": "clean",
        "reasons": [],
    }
    (root / "data_quality.json").write_text(json.dumps(dq, indent=2))
    _ = sha256_file  # imported for parity with the testkit writer
    return root


def write_shadow_spec(path: str | Path, shadows: list[ShadowSpec]) -> Path:
    """qt.shadow_spec Parquet for a list of shadows."""
    import pyarrow as pa

    from queuetruth.schemas.arrow import SCHEMAS
    from queuetruth.schemas.io import write_table

    sch = SCHEMAS["qt.shadow_spec"]
    rows = {
        "shadow_id": [s.shadow_id for s in shadows],
        "instrument_id": [s.instrument_id for s in shadows],
        "side": [ord(s.side) for s in shadows],
        "price_mode": [s.price_mode for s in shadows],
        "price": [s.price for s in shadows],
        "offset_ticks": [s.offset_ticks for s in shadows],
        "qty": [s.qty for s in shadows],
        "ts_submit": [s.ts_submit for s in shadows],
        "latency_ns": [s.latency_ns for s in shadows],
        "ts_cancel": [s.ts_cancel for s in shadows],
        "group": [None for _ in shadows],
        "source": ["user" for _ in shadows],
        "order_key": [None for _ in shadows],
    }
    arrays = []
    for f in sch:
        v = rows[f.name]
        if pa.types.is_timestamp(f.type):
            arrays.append(pa.array(v, pa.int64()).cast(f.type))
        elif pa.types.is_dictionary(f.type):
            arrays.append(pa.array(v, pa.utf8()).dictionary_encode().cast(f.type))
        else:
            arrays.append(pa.array(v, f.type))
    path = Path(path)
    write_table(pa.Table.from_arrays(arrays, schema=sch), path, sch)
    return path


def write_shadow_amends(path: str | Path, amends: dict[str, list[Amend]]) -> Path:
    """qt.shadow_amend Parquet."""
    import pyarrow as pa

    from queuetruth.schemas.arrow import SCHEMAS
    from queuetruth.schemas.io import write_table

    sch = SCHEMAS["qt.shadow_amend"]
    rows = [a for v in amends.values() for a in v]
    arrays = [
        pa.array([a.shadow_id for a in rows], pa.utf8()),
        pa.array([a.ts_send for a in rows], pa.int64()).cast(sch.field("ts_send").type),
        pa.array([a.new_qty for a in rows], pa.uint64()),
    ]
    path = Path(path)
    write_table(pa.Table.from_arrays(arrays, schema=sch), path, sch)
    return path
