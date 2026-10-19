"""Self-test of validate_et.py's renderers and digests without queuetruth: et CSV outputs are converted into
qt-shaped Parquet tables (qt.bbo full, qt.rank_trace with per-instrument sweep ids, qt.depth long change-only,
qt.l2 depth rows), then fed through the exact functions RD-3 / RD-4 / RD-7 use, and the digests must equal
expected.json. Proves the comparison code, so a later mismatch points at queuetruth, not at the driver.

    python -I selftest_renderers.py --et-root <et> --expected expected.json --scratch <dir> [--days 2019-12-30,2020-01-30]
Writes up to ~0.5 GB of Parquet into --scratch and deletes it unless --keep.
"""  # noqa: E501

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import validate_et as V  # noqa: E402

UNDEF = V.UNDEF
TS = pa.timestamp("ns", tz="UTC")


def md(name: str) -> dict:
    return {b"qt.schema": name.encode(), b"qt.schema_version": b"1", b"qt.engine_version": b"selftest"}


class Writer:
    def __init__(self, path: Path, schema: pa.Schema, name: str):
        self.schema = schema.with_metadata(md(name))
        self.w = pq.ParquetWriter(path, self.schema, compression="zstd")
        self.buf: dict[str, list] = {f.name: [] for f in schema}
        self.n = 0

    def add(self, *vals):
        for f, v in zip(self.schema, vals, strict=False):
            self.buf[f.name].append(v)
        self.n += 1
        if self.n % 500_000 == 0:
            self.flush()

    def flush(self):
        if self.buf[self.schema[0].name]:
            self.w.write_table(pa.Table.from_pydict(self.buf, schema=self.schema))
            self.buf = {f.name: [] for f in self.schema}

    def close(self):
        self.flush()
        self.w.close()


def instruments_of(src: Path) -> list[int]:
    seen = set()
    with open(src, "rb") as f:
        f.readline()
        for ln in f:
            seen.add(int(ln.split(b",", 2)[1]))
    return sorted(seen)


def convert_bbo(src: Path, dst: Path):
    """et interleaves instruments in file order; qt writes partitions one after another (SPEC 5.1.6), so rows are
    regrouped per instrument in partition order (one pass per instrument)."""
    sch = pa.schema(
        [
            ("ts_event", TS),
            ("instrument_id", pa.uint32()),
            ("sequence", pa.uint64()),
            ("bid_px", pa.int64()),
            ("bid_sz", pa.uint64()),
            ("bid_ct", pa.uint32()),
            ("ask_px", pa.int64()),
            ("ask_sz", pa.uint64()),
            ("ask_ct", pa.uint32()),
        ]
    )
    w = Writer(dst, sch, "qt.bbo")
    for want in instruments_of(src):
        tag = f",{want},".encode()
        with open(src, "rb") as f:
            f.readline()
            for ln in f:
                if tag not in ln[:40]:
                    continue
                t, i, bp, bs, ap, as_, q = map(int, ln.split(b","))
                if i != want:
                    continue
                w.add(t, i, q, None if bp == UNDEF else bp, bs, 0, None if ap == UNDEF else ap, as_, 0)
    w.close()


def convert_rank(src: Path, dst: Path):
    rows = []
    with open(src) as f:
        f.readline()
        for ln in f:
            rows.append(ln.rstrip("\n").split(","))
    # per-instrument sweep counter, numbered by the et global id rank (not first appearance) to exercise relabelling
    ids: dict[str, list[int]] = {}
    for r in rows:
        ids.setdefault(r[1], []).append(int(r[9]))
    remap = {i: {g: k + 1 for k, g in enumerate(sorted(set(v)))} for i, v in ids.items()}
    sch = pa.schema(
        [
            ("ts_event", TS),
            ("instrument_id", pa.uint32()),
            ("sequence", pa.uint64()),
            ("order_id", pa.uint64()),
            ("side", pa.uint8()),
            ("price", pa.int64()),
            ("rank", pa.uint32()),
            ("rank_sweep_adjusted", pa.uint32()),
            ("level_depth", pa.uint32()),
            ("sweep_id", pa.uint64()),
            ("exec_kind", pa.dictionary(pa.int8(), pa.utf8())),
        ]
    )
    w = Writer(dst, sch, "qt.rank_trace")
    for r in rows:
        w.add(
            int(r[0]),
            int(r[1]),
            int(r[2]),
            int(r[3]),
            ord(r[4]),
            int(r[5]),
            int(r[6]),
            int(r[7]),
            int(r[8]),
            remap[r[1]][int(r[9])],
            r[10],
        )
    w.close()


def convert_depth(src: Path, depth_dst: Path, l2_root: Path, date: str):
    """et wide depth (N levels, every event) -> qt.depth long change-only rows and qt.l2 depth rows per partition."""
    dsch = pa.schema(
        [
            ("ts_event", TS),
            ("instrument_id", pa.uint32()),
            ("sequence", pa.uint64()),
            ("side", pa.uint8()),
            ("level", pa.uint8()),
            ("price", pa.int64()),
            ("size", pa.uint64()),
            ("count", pa.uint32()),
        ]
    )
    lsch = pa.schema(
        [
            ("instrument_id", pa.uint32()),
            ("sequence", pa.uint64()),
            ("kind", pa.dictionary(pa.int8(), pa.utf8())),
            ("side", pa.uint8()),
            ("price", pa.int64()),
            ("new_qty", pa.uint64()),
        ]
    )
    dw = Writer(depth_dst, dsch, "qt.depth")
    lw: dict[int, Writer] = {}
    prev: dict[int, list] = {}
    for want in instruments_of(src):
        with open(src) as f:
            hdr = f.readline().strip().split(",")
            n = sum(1 for c in hdr if c.startswith("bid_px_"))
            tag = f",{want},"
            for ln in f:
                if tag not in ln[:40]:
                    continue
                p = list(map(int, ln.split(",")))
                t, i, q = p[0], p[1], p[2]
                if i != want:
                    continue
                lv = [(p[3 + 4 * k], p[4 + 4 * k], p[5 + 4 * k], p[6 + 4 * k]) for k in range(n)]
                old = prev.get(i) or [(UNDEF, 0, UNDEF, 0)] * n
                if i not in lw:
                    d = l2_root / f"date={date}" / f"instrument_id={i}"
                    d.mkdir(parents=True, exist_ok=True)
                    lw[i] = Writer(d / "part-00000.parquet", lsch, "qt.l2")
                for side, off in ((66, 0), (65, 2)):
                    for k in range(n):
                        if lv[k][off : off + 2] != old[k][off : off + 2]:
                            px, sz = lv[k][off : off + 2]
                            dw.add(t, i, q, side, k, None if px == UNDEF else px, sz, 0)
                    was = {o[off]: o[off + 1] for o in old if o[off] != UNDEF}
                    now = {o[off]: o[off + 1] for o in lv if o[off] != UNDEF}
                    for px in sorted(set(was) | set(now)):
                        if was.get(px, 0) != now.get(px, 0):
                            lw[i].add(i, q, "depth", side, px, now.get(px, 0))
                prev[i] = lv
    dw.close()
    for w in lw.values():
        w.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--et-root",
        type=Path,
        default=os.environ.get("QT_ET_ROOT"),
        required=not os.environ.get("QT_ET_ROOT"),
        help="local checkout of the et prototype (default: $QT_ET_ROOT)",
    )
    ap.add_argument("--expected", type=Path, default=HERE.parent.parent / "expected.json")
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--days", default="2019-12-30,2020-01-30")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    exp = json.loads(a.expected.read_text())
    res = {}
    for date in a.days.split(","):
        t0 = time.time()
        cfg, E = V.DAYS[date], exp["days"][date]
        d = a.scratch / date
        d.mkdir(parents=True, exist_ok=True)
        convert_bbo(a.et_root / cfg["replay"] / "bbo.csv", d / "bbo.parquet")
        dig, n = V.bbo_digests(d / "bbo.parquet")
        r = {"bbo_rows": n == E["bbo_full"]["rows_by_instrument"], "bbo_sha": dig == E["bbo_full"]["bbo_rows_sha256"]}
        convert_rank(a.et_root / cfg["replay"] / "rank_trace.csv", d / "rank.parquet")
        rel, nos, rn = V.rank_digests(d / "rank.parquet")
        r |= {
            "rank_rows": rn == E["rank_trace"]["rows_by_instrument"],
            "rank_sha_relabelled": rel == E["rank_trace"]["rank_rows_sha256_relabelled"],
            "rank_sha_no_sweep": nos == E["rank_trace"]["rank_rows_sha256_no_sweep"],
        }
        src = a.et_root / (
            cfg["replay"] + "/depth.csv" if date == V.DAY2 else cfg["fidelity"] + "/engine_pass1/depth.csv"
        )
        levels = [3, 5] if date == V.DAY2 else [3]
        convert_depth(src, d / "depth.parquet", d / "l2", date)
        da = V.render_wide(
            V.iter_events(d / "bbo.parquet"),
            V.iter_cols([d / "depth.parquet"], ["instrument_id", "sequence", "side", "level", "price", "size"]),
            levels,
            mode="depth",
        )
        db = V.render_wide(
            V.iter_events(d / "bbo.parquet"),
            V.iter_cols(
                V.l2_files_in_partition_order(d / "l2"),
                ["instrument_id", "sequence", "kind", "side", "price", "new_qty"],
                dict_cols=("kind",),
            ),
            levels,
            mode="l2",
        )
        for lv in levels:
            want = E["depth"][f"depth{lv}_rows_sha256"]
            r[f"depth{lv}_from_long"] = da["digests"][lv] == want
            r[f"depth{lv}_from_l2_view"] = db["digests"][lv] == want
        r["unconsumed"] = (da["unconsumed_rows"], db["unconsumed_rows"])
        r["scratch_mb"] = V.du_mb(d)
        r["wall_s"] = round(time.time() - t0, 1)
        res[date] = r
        print(date, r, flush=True)
        if not a.keep:
            shutil.rmtree(d)
    ok = all(v is True for r in res.values() for k, v in r.items() if k not in ("unconsumed", "scratch_mb", "wall_s"))
    ok = ok and all(r["unconsumed"] == (0, 0) for r in res.values())
    print(json.dumps({"selftest_renderers": "pass" if ok else "fail", "days": res}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
