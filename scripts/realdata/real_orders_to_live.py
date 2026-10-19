"""Build a canonical live order log (SPEC 3.8: orders.parquet, events.parquet, manifest.json) from real resting
orders of an MBO tape, for RD-11 (scorer on real orders).

Each real order becomes one live order: `submit_sent` (ts_local_send = add ts_event), `ack` (ts_exchange = add
ts_event, match_time), one `fill` per `F` record of the order (ts_exchange = F ts_event, qty = F size, px = F price,
liquidity maker, fill_id = "<order_key>:<sequence>"), and for cancelled lives `cancel_sent` (ts_local_send = removal
ts_event) + `cancel_ack` (ts_exchange = removal ts_event); `done` closes filled and cancelled lives. Scored with
`entry = exchange_ack` and `sim = exact`, the schedule is exactly et's mirror shadow (submit at the add, latency 0,
cancel at the removal), so the 2x2 must equal et `fidelity.json` (day one 235 / 2,739 / 26 / 0).

Sample sources:
  --sample et      (default) the 3,000 orders et sampled (`<fidelity>/fidelity_orders.parquet`), cancel times from
                   et `real_orders.parquet`; identical order set to RD-5, so the expected 2x2 is known exactly.
  --sample qt      seeded sample from queuetruth `real_orders.parquet` (qt.real_orders): equal per instrument x
                   {filled, not filled}, `in_snapshot` excluded.
Fills come from the MBO tape: et `mbo.csv` (--mbo-csv) or a queuetruth dataset directory (--dataset).

Runs today against et artifacts (needs only pyarrow; the queuetruth Arrow schemas are loaded from --qt-root source
when the package is not installed). Writes event-level rows: put the output under `.realdata/` only.

    python -I real_orders_to_live.py --fidelity <et>/out/fidelity-itch --mbo-csv <et>/out/itch-12302019/mbo.csv \
        --instruments-csv <et>/out/itch-12302019/instruments.csv --out .realdata/live/d1-et3000
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
import types
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

VENUE = "nasdaq_itch_tape"
ACCOUNT = "tape"
PRICE_EXP = -9
F_CODE = ord("F")


def load_qt_arrow(qt_root: Path):
    """queuetruth.schemas.arrow (SCHEMAS, DICT_VALUES): installed package first, else loaded from source by path."""
    try:
        from queuetruth.schemas.arrow import DICT_VALUES, SCHEMAS  # QT-API (installed package)

        return SCHEMAS, DICT_VALUES
    except Exception:
        pass
    ver = types.ModuleType("queuetruth._version")
    ver.__version__ = "0.1.0"
    pkg = types.ModuleType("queuetruth")
    pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("queuetruth", pkg)
    sys.modules.setdefault("queuetruth._version", ver)
    spec = importlib.util.spec_from_file_location("qt_arrow_src", qt_root / "python/queuetruth/schemas/arrow.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.SCHEMAS, mod.DICT_VALUES


def px_str(px: int) -> str:
    s = f"{px // 10**9}.{px % 10**9:09d}".rstrip("0").rstrip(".")
    return s


def table(rows: list[dict], name: str, schemas, dicts) -> pa.Table:
    sch = schemas[name]
    cols = []
    for f in sch:
        vals = [r.get(f.name) for r in rows]
        if pa.types.is_dictionary(f.type):
            dv = list(dicts[name][f.name])
            idx = pa.array([None if v is None else dv.index(v) for v in vals], pa.int8())
            cols.append(pa.DictionaryArray.from_arrays(idx, pa.array(dv, pa.utf8())))
        else:
            cols.append(pa.array(vals, f.type))
    return pa.Table.from_arrays(cols, schema=sch)


def sample_et(fidelity: Path) -> list[dict]:
    fo = pq.read_table(
        fidelity / "fidelity_orders.parquet",
        columns=["shadow_id", "instrument_id", "order_id", "side", "price", "size", "ts_add"],
    )
    ro = pq.read_table(
        fidelity / "real_orders.parquet",
        columns=["instrument_id", "order_id", "outcome", "ts_cancel", "ts_done", "filled_qty"],
    )
    want = set(zip(fo["instrument_id"].to_pylist(), fo["order_id"].to_pylist(), strict=False))
    mask = pc.is_in(ro["order_id"], value_set=pa.array(sorted({o for _, o in want})))
    ro = ro.filter(mask)
    life = {
        (i, o): (oc, tc, td, fq)
        for i, o, oc, tc, td, fq in zip(*(ro[c].to_pylist() for c in ro.column_names), strict=False)
        if (i, o) in want
    }
    out = []
    for r in fo.to_pylist():
        oc, tc, _td, _fq = life[(r["instrument_id"], r["order_id"])]
        cancelled = oc in ("cancelled", "partial")
        out.append(
            {
                "instrument_id": r["instrument_id"],
                "order_id": r["order_id"],
                "side": r["side"],
                "price": r["price"],
                "size": r["size"],
                "ts_add": r["ts_add"],
                "ts_cancel": tc if cancelled else None,
                "end": "cancelled" if cancelled else "filled",
            }
        )
    return out


def sample_qt(real_orders: Path, n: int, seed: int) -> list[dict]:
    t = pq.read_table(real_orders)
    t = t.filter(pc.invert(t["in_snapshot"]))
    rows = t.select(
        ["instrument_id", "order_id", "side", "price", "size_at_add", "ts_add", "ts_end", "end_reason", "filled_qty"]
    ).to_pylist()
    strata = defaultdict(list)
    for r in rows:
        strata[(r["instrument_id"], r["filled_qty"] > 0)].append(r)
    rng = random.Random(seed)
    keys = sorted(strata)
    per = max(1, n // len(keys))
    picked = []
    for k in keys:
        pool = strata[k]
        picked += rng.sample(pool, min(per, len(pool)))
    out = []
    for r in sorted(picked, key=lambda r: (r["ts_add"], r["instrument_id"], r["order_id"])):
        ts_add = r["ts_add"].value if hasattr(r["ts_add"], "value") else r["ts_add"]
        ts_end = r["ts_end"].value if hasattr(r["ts_end"], "value") else r["ts_end"]
        cancelled = r["end_reason"] in ("cancelled", "replaced")
        out.append(
            {
                "instrument_id": r["instrument_id"],
                "order_id": r["order_id"],
                "side": chr(r["side"]),
                "price": r["price"],
                "size": r["size_at_add"],
                "ts_add": ts_add,
                "ts_cancel": ts_end if cancelled else None,
                "end": "cancelled" if cancelled else ("filled" if r["end_reason"] == "filled" else "open"),
            }
        )
    return out


def fills_from_csv(mbo_csv: Path, keys: set[tuple[int, int]]) -> dict:
    fills = defaultdict(list)
    oids = {o for _, o in keys}
    with open(mbo_csv, "rb", buffering=1 << 22) as f:
        f.readline()
        for ln in f:
            p = ln.split(b",", 10)
            if p[3] != b"F":
                continue
            oid = int(p[7])
            if oid not in oids:
                continue
            inst = int(p[2])
            if (inst, oid) in keys:
                fills[(inst, oid)].append((int(p[0]), int(p[9]), int(p[5]), int(p[6])))
    return fills


def fills_from_dataset(dataset: Path, keys: set[tuple[int, int]]) -> dict:
    import pyarrow.dataset as ds

    # the files carry instrument_id (uint32) themselves; the hive keys only need matching types
    part = ds.partitioning(pa.schema([("date", pa.string()), ("instrument_id", pa.uint32())]), flavor="hive")
    d = ds.dataset(dataset / "mbo", format="parquet", partitioning=part)
    flt = (ds.field("action") == F_CODE) & ds.field("order_id").isin(sorted({o for _, o in keys}))
    t = d.to_table(columns=["ts_event", "instrument_id", "sequence", "price", "size", "order_id"], filter=flt)
    fills = defaultdict(list)
    for ts, inst, seq, px, sz, oid in zip(
        pc.cast(t["ts_event"], pa.int64()).to_pylist(),
        *(t[c].to_pylist() for c in ("instrument_id", "sequence", "price", "size", "order_id")),
        strict=False,
    ):
        if (inst, oid) in keys:
            fills[(inst, oid)].append((ts, seq, px, sz))
    return fills


def build(orders: list[dict], fills: dict, symbols: dict[int, str]) -> tuple[list[dict], list[dict], dict]:
    o_rows, e_rows = [], []
    stats = {"n_orders": 0, "n_filled": 0, "n_fills": 0, "n_cancelled": 0}
    for r in orders:
        key = f"{VENUE}:{ACCOUNT}:{r['instrument_id']}:{r['order_id']}"
        o_rows.append(
            {
                "order_key": key,
                "venue": VENUE,
                "account": ACCOUNT,
                "instrument": symbols.get(r["instrument_id"], ""),
                "instrument_id": r["instrument_id"],
                "venue_order_id": str(r["order_id"]),
                "client_order_id": None,
                "client_order_id_chain": None,
                "side": "buy" if r["side"] == "B" else "sell",
                "order_type": "limit",
                "tif": "day",
                "post_only": False,
                "reduce_only": None,
                "orig_qty": r["size"],
                "qty_str": str(r["size"]),
                "limit_px": r["price"],
                "px_str": px_str(r["price"]),
                "price_exp": PRICE_EXP,
                "size_exp": 0,
                "strategy_tag": "tape_real_order",
                "validation_status": "real",
                "raw": None,
            }
        )
        ev = []

        def add(et, **kw):
            base = {
                "order_key": key,  # noqa: B023
                "event_seq": len(ev) + 1,  # noqa: B023
                "event_type": et,
                "ts_exchange_kind": "unknown",
                "ts_precision": "ns",
                "source": "synthetic",
                "derived": False,
            }
            base.update(kw)
            ev.append(base)  # noqa: B023

        add("submit_sent", qty=r["size"], px=r["price"], ts_local_send=r["ts_add"], reason="tape add")
        add("ack", leaves_qty=r["size"], cum_qty=0, ts_exchange=r["ts_add"], ts_exchange_kind="match_time")
        cum = 0
        fl = sorted(fills.get((r["instrument_id"], r["order_id"]), []), key=lambda x: (x[0], x[1]))
        for ts, seq, px, sz in fl:
            cum += sz
            add(
                "fill",
                qty=sz,
                px=px,
                leaves_qty=max(r["size"] - cum, 0),
                cum_qty=cum,
                fill_id=f"{key}:{seq}",
                liquidity="maker",
                ts_exchange=ts,
                ts_exchange_kind="match_time",
            )
        stats["n_fills"] += len(fl)
        stats["n_filled"] += bool(fl)
        if r["end"] == "cancelled" and r["ts_cancel"] is not None:
            stats["n_cancelled"] += 1
            add("cancel_sent", ts_local_send=r["ts_cancel"], reason="tape removal")
            add("cancel_ack", leaves_qty=0, cum_qty=cum, ts_exchange=r["ts_cancel"], ts_exchange_kind="match_time")
            add("done", leaves_qty=0, cum_qty=cum, ts_exchange=r["ts_cancel"], ts_exchange_kind="match_time")
        elif r["end"] == "filled":
            add(
                "done",
                leaves_qty=0,
                cum_qty=cum,
                ts_exchange=fl[-1][0] if fl else r["ts_add"],
                ts_exchange_kind="match_time",
            )
        e_rows += ev
        stats["n_orders"] += 1
    return o_rows, e_rows, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sample", choices=["et", "qt"], default="et")
    ap.add_argument("--fidelity", type=Path, help="et fidelity dir (fidelity_orders.parquet, real_orders.parquet)")
    ap.add_argument("--qt-real-orders", type=Path, help="queuetruth replay real_orders.parquet (--sample qt)")
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--mbo-csv", type=Path, help="et mbo.csv (fills source)")
    ap.add_argument("--dataset", type=Path, help="queuetruth dataset dir (fills source)")
    ap.add_argument("--instruments-csv", type=Path, help="et instruments.csv (instrument_id -> symbol)")
    ap.add_argument(
        "--qt-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="queuetruth repository root (default: this checkout)",
    )
    a = ap.parse_args(argv)

    schemas, dicts = load_qt_arrow(a.qt_root)
    orders = sample_et(a.fidelity) if a.sample == "et" else sample_qt(a.qt_real_orders, a.n, a.seed)
    keys = {(r["instrument_id"], r["order_id"]) for r in orders}
    if a.dataset:
        fills = fills_from_dataset(a.dataset, keys)
    elif a.mbo_csv:
        fills = fills_from_csv(a.mbo_csv, keys)
    else:
        raise SystemExit("need --dataset or --mbo-csv for fills")
    symbols = {}
    if a.instruments_csv:
        for ln in a.instruments_csv.read_text().splitlines()[1:]:
            p = ln.split(",")
            symbols[int(p[0])] = p[1]
    elif a.dataset:
        it = pq.read_table(a.dataset / "instruments.parquet", columns=["instrument_id", "symbol"])
        symbols = dict(zip(it["instrument_id"].to_pylist(), it["symbol"].to_pylist(), strict=False))
    o_rows, e_rows, stats = build(orders, fills, symbols)
    a.out.mkdir(parents=True, exist_ok=True)
    kw = dict(compression="zstd", compression_level=3, row_group_size=1_048_576)
    pq.write_table(table(o_rows, "qt.live_orders", schemas, dicts), a.out / "orders.parquet", **kw)
    pq.write_table(table(e_rows, "qt.live_events", schemas, dicts), a.out / "events.parquet", **kw)
    man = {
        "schema": "qt.live_manifest",
        "schema_version": 1,
        "venues": [VENUE],
        "accounts": [ACCOUNT],
        "n_orders": len(o_rows),
        "n_events": len(e_rows),
        "instrument_map_sha256": None,
        "created_by": "scripts/realdata/real_orders_to_live.py",
    }
    (a.out / "manifest.json").write_text(json.dumps(man, indent=1) + "\n")
    digest = hashlib.sha256((a.out / "events.parquet").read_bytes()).hexdigest()
    print(json.dumps({**stats, "n_events": len(e_rows), "out": str(a.out), "events_sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
