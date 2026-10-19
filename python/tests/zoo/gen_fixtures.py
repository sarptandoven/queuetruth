"""Seeded generator of the O-ZOO-2 parity fixtures (`tests/fixtures/zoo/hbt_parity/`).

200 random model-level L2 sequences per hbt family (`hbt_risk_adverse`, `hbt_power`, `hbt_power2`, `hbt_power3`,
`hbt_log`, `hbt_log2`), each driven through `python/tests/reference/hbt_ref.py` under both exchanges. Ops: `arrive`
(a = level qty), `depth` (a = prev, b = new), `trade` (a = qty at the price, aggressor opposite), `through`,
`cross`. Randomness: the xoshiro256** mirror of `qt_types` seeded with `stable_seed(20261018, [b"zoo_parity",
family, k as 4 LE bytes])`, so the fixtures do not depend on numpy.

Files: `sequences.parquet` (seq_id, family, n, lot, qty, op_idx, op, a, b) and `expected.parquet` (seq_id, exch,
op_idx, f_bits, t_bits, fill_qty_bits, leaves_bits, status, error). Run `python gen_fixtures.py --write` to rewrite
them and the zoo `MANIFEST.sha256`; the pytest suite regenerates in memory and compares.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "reference"))

import hbt_ref  # noqa: E402
from rng import Xoshiro256StarStar, stable_seed  # noqa: E402

ROOT = HERE.parents[2]
OUT = ROOT / "tests" / "fixtures" / "zoo" / "hbt_parity"
SEED = 20261018
N_PER_FAMILY = 200
N_CHOICES = (0.5, 1.0, 1.5, 2.0, 3.0)


def _rng(family: str, k: int) -> Xoshiro256StarStar:
    return Xoshiro256StarStar(stable_seed(SEED, [b"zoo_parity", family.encode(), k.to_bytes(4, "little")]))


def gen_sequence(family: str, k: int) -> tuple[float | None, float, float, list[tuple[str, float, float]]]:
    """(n, lot, qty, ops) of sequence `k` of a family."""
    r = _rng(family, k)
    lot = 0.5 if k % 5 == 4 else 1.0
    n = (
        N_CHOICES[r.range_inclusive(0, len(N_CHOICES) - 1)]
        if family in ("hbt_power", "hbt_power2", "hbt_power3")
        else None
    )
    qty = lot * float(1 + r.range_inclusive(0, 3))
    if k % 7 == 3:
        qty = lot * 1.4
    level = lot * float(r.range_inclusive(0, 60))
    ops: list[tuple[str, float, float]] = [("arrive", level, 0.0)]
    for _ in range(15 + r.range_inclusive(0, 30)):
        u = r.next_f64()
        if u < 0.35:
            new = max(0.0, level - lot * float(r.range_inclusive(1, 15)))
            ops.append(("depth", level, new))
            level = new
        elif u < 0.55:
            new = level + lot * float(r.range_inclusive(1, 15))
            ops.append(("depth", level, new))
            level = new
        elif u < 0.9:
            v = lot * float(r.range_inclusive(1, 12))
            if r.next_f64() < 0.1:
                v = lot * 0.3
            ops.append(("trade", v, 0.0))
            if r.next_f64() < 0.8:
                new = max(0.0, level - v)
                ops.append(("depth", level, new))
                level = new
        elif u < 0.95:
            ops.append(("depth", level, level))
        else:
            ops.append(("depth", level, 0.0))
            level = 0.0
    u = r.next_f64()
    if u < 0.15:
        ops.append(("through", 0.0, 0.0))
    elif u < 0.3:
        ops.append(("cross", 0.0, 0.0))
    return n, lot, qty, ops


def build() -> tuple[pa.Table, pa.Table]:
    """Both fixture tables."""
    seq = {k: [] for k in ("seq_id", "family", "n", "lot", "qty", "op_idx", "op", "a", "b")}
    exp = {
        k: []
        for k in ("seq_id", "exch", "op_idx", "f_bits", "t_bits", "fill_qty_bits", "leaves_bits", "status", "error")
    }
    sid = 0
    for family in hbt_ref.FAMILIES:
        for k in range(N_PER_FAMILY):
            n, lot, qty, ops = gen_sequence(family, k)
            for j, (op, a, b) in enumerate(ops):
                for key, v in zip(seq, (sid, family, n, lot, qty, j, op, a, b), strict=True):
                    seq[key].append(v)
            for exch in ("no_partial", "partial"):
                res = hbt_ref.run_sequence(family, n, lot, qty, exch == "partial", ops)
                for j, r in enumerate(res):
                    exp["seq_id"].append(sid)
                    exp["exch"].append(exch)
                    exp["op_idx"].append(j)
                    for key in ("f_bits", "t_bits", "fill_qty_bits", "leaves_bits", "status", "error"):
                        exp[key].append(r[key])
            sid += 1
    seq_t = pa.table(
        {
            "seq_id": pa.array(seq["seq_id"], pa.int32()),
            "family": pa.array(seq["family"], pa.utf8()),
            "n": pa.array(seq["n"], pa.float64()),
            "lot": pa.array(seq["lot"], pa.float64()),
            "qty": pa.array(seq["qty"], pa.float64()),
            "op_idx": pa.array(seq["op_idx"], pa.int32()),
            "op": pa.array(seq["op"], pa.utf8()),
            "a": pa.array(seq["a"], pa.float64()),
            "b": pa.array(seq["b"], pa.float64()),
        }
    )
    exp_t = pa.table(
        {
            "seq_id": pa.array(exp["seq_id"], pa.int32()),
            "exch": pa.array(exp["exch"], pa.utf8()),
            "op_idx": pa.array(exp["op_idx"], pa.int32()),
            "f_bits": pa.array(exp["f_bits"], pa.uint64()),
            "t_bits": pa.array(exp["t_bits"], pa.uint64()),
            "fill_qty_bits": pa.array(exp["fill_qty_bits"], pa.uint64()),
            "leaves_bits": pa.array(exp["leaves_bits"], pa.uint64()),
            "status": pa.array(exp["status"], pa.utf8()),
            "error": pa.array(exp["error"], pa.bool_()),
        }
    )
    return seq_t, exp_t


def write() -> None:
    """Writes the Parquet fixtures and the zoo manifest."""
    OUT.mkdir(parents=True, exist_ok=True)
    seq_t, exp_t = build()
    pq.write_table(seq_t, OUT / "sequences.parquet", compression="zstd")
    pq.write_table(exp_t, OUT / "expected.parquet", compression="zstd")
    sys.path.insert(0, str(HERE))
    from adopt_hbt_vectors import write_manifest

    write_manifest(OUT.parent)


# ---- O-ZOO-4 faithful: hbt L3 FIFO on constructed MBO sequences ------------------------------------------------

L3_FILE = OUT.parent / "l3_faithful.json"
N_L3 = 300
BT_ID = 1_000_000


def gen_l3_sequence(k: int) -> dict:
    """One single-record-event MBO sequence (CME semantics) with one GTX shadow, and hbt's outcome for it."""
    r = Xoshiro256StarStar(stable_seed(SEED, [b"zoo_l3", k.to_bytes(4, "little")]))
    book: dict[int, tuple[str, int, int]] = {}
    recs: list[list] = []
    ts = 0
    nid = 1

    def best(side: str) -> int | None:
        t = [x[1] for x in book.values() if x[0] == side]
        return (max(t) if side == "B" else min(t)) if t else None

    def add(side: str, tick: int, qty: int) -> None:
        nonlocal ts, nid
        ts += 10
        recs.append([ts, "A", side, tick, qty, nid])
        book[nid] = (side, tick, qty)
        nid += 1

    for _ in range(6 + r.range_inclusive(0, 6)):
        if r.next_f64() < 0.5:
            add("B", 97 + r.range_inclusive(0, 3), 1 + r.range_inclusive(0, 4))
        else:
            add("A", 101 + r.range_inclusive(0, 3), 1 + r.range_inclusive(0, 4))
    s_side = "B" if r.next_f64() < 0.5 else "A"
    if r.next_f64() < 0.05 and best("A") is not None and best("B") is not None:
        s_tick = best("A") if s_side == "B" else best("B")
    else:
        s_tick = 97 + r.range_inclusive(0, 3) if s_side == "B" else 101 + r.range_inclusive(0, 3)
    s_ts = ts + 5
    for _ in range(30 + r.range_inclusive(0, 30)):
        u = r.next_f64()
        bb, ba = best("B"), best("A")
        if u < 0.35 or not book:
            side = "B" if r.next_f64() < 0.5 else "A"
            if side == "B":
                hi = (ba - 1) if ba is not None else 100
                if r.next_f64() < 0.08 and s_side == "A" and (ba is None or s_tick < ba):
                    tick = s_tick  # aggressive bid reaching an ask shadow
                else:
                    tick = min(hi, 96 + r.range_inclusive(0, 4))
            else:
                lo = (bb + 1) if bb is not None else 101
                if r.next_f64() < 0.08 and s_side == "B" and (bb is None or s_tick > bb):
                    tick = s_tick  # aggressive ask reaching a bid shadow
                else:
                    tick = max(lo, 101 + r.range_inclusive(0, 4))
            add(side, tick, 1 + r.range_inclusive(0, 4))
            continue
        oid = sorted(book)[r.range_inclusive(0, len(book) - 1)]
        side, tick, qty = book[oid]
        if u < 0.55:
            q = qty if r.next_f64() < 0.7 else max(1, qty - 1)
            ts += 10
            recs.append([ts, "C", side, tick, q, oid])
            if q >= qty:
                del book[oid]
            else:
                book[oid] = (side, tick, qty - q)
        elif u < 0.7:
            if r.next_f64() < 0.5:
                nq = qty + 1 + r.range_inclusive(0, 2) if r.next_f64() < 0.5 else max(1, qty - 1)
                if nq == qty:
                    nq = qty + 1
                nt = tick
            else:
                nq = qty
                if side == "B":
                    nt = min((best("A") or 101) - 1, 96 + r.range_inclusive(0, 4))
                else:
                    nt = max((best("B") or 100) + 1, 101 + r.range_inclusive(0, 4))
                if nt == tick:
                    continue
            ts += 10
            recs.append([ts, "M", side, nt, nq, oid])
            book[oid] = (side, nt, nq)
        else:
            q = qty if r.next_f64() < 0.6 else 1 + r.range_inclusive(0, qty - 1)
            ts += 10
            recs.append([ts, "F", side, tick, q, oid])
            ts += 10
            recs.append([ts, "C", side, tick, q, oid])
            if q >= qty:
                del book[oid]
            else:
                book[oid] = (side, tick, qty - q)
    ref = hbt_ref.L3Fifo()
    submitted = False
    for t, act, side, tick, size, oid in recs:
        if not submitted and t > s_ts:
            ref.submit(BT_ID, s_side, s_tick, s_ts)
            submitted = True
        if act == "A":
            ref.add_feed(oid, side, tick, float(size), t)
        elif act == "C":
            ref.cancel_feed(oid, float(size))
        elif act == "M":
            ref.modify_feed(oid, tick, float(size), t)
        else:
            ref.fill_feed(oid, tick, t)
    first = next(([t, why] for (t, o, why) in ref.fills if o == BT_ID), None)
    return {"id": k, "shadow": {"side": s_side, "tick": s_tick, "ts": s_ts}, "records": recs, "expected": first}


def build_l3() -> dict:
    """The O-ZOO-4 faithful fixture document."""
    return {
        "schema": "qt.zoo_l3_faithful",
        "schema_version": 1,
        "semantics": "one record per event (LAST); CME rules; shadow = one GTX backtest order submitted after every "
        "record with ts <= shadow.ts; expected = first hbt fill [ts, queue|through|cross|expired] or null",
        "sequences": [gen_l3_sequence(k) for k in range(N_L3)],
    }


def write_l3() -> None:
    """Writes `l3_faithful.json` and the zoo manifest."""
    import json

    L3_FILE.write_text(json.dumps(build_l3(), separators=(",", ":")) + "\n")
    sys.path.insert(0, str(HERE))
    from adopt_hbt_vectors import write_manifest

    write_manifest(OUT.parent)


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
        write_l3()
    else:
        s, e = build()
        print(s.num_rows, e.num_rows)
