"""Reference numbers recomputed directly from the real-data artifacts of the author's earlier private et prototype.

Read-only over `<et-root>/out` and `<et-root>/data`. Streams every file (no full-tape pandas), so it runs in a few
minutes and well under 1 GB of RAM. Writes one JSON document of aggregate numbers and digests (no event-level rows)
that `validate_et.py` compares queuetruth outputs against.

Usage (runs today, needs only Python 3.11+ and pyarrow):
    python -I et_reference.py --et-root <et checkout> --out expected.json

Digest conventions (validate_et.py renders queuetruth outputs the same way before hashing):
- `bbo_rows_sha256[instrument_id]`: sha256 over the et `bbo.csv` data lines of that instrument, in file order,
  each line exactly `ts_event,instrument_id,bid_px,bid_sz,ask_px,ask_sz,sequence\n` (empty side: price
  9223372036854775807, size 0).
- `rank_rows_sha256_relabelled[instrument_id]`: sha256 over et `rank_trace.csv` lines of that instrument in file order,
  with `sweep_id` replaced by the 1-based index of that sweep's first appearance within the instrument's rows.
- `rank_rows_sha256_no_sweep[instrument_id]`: same lines with the `sweep_id` column dropped.
- `depth{N}_rows_sha256[instrument_id]`: sha256 over et `depth.csv` data lines (N levels, et wide layout) per instrument.
- `trades_rows_sha256[instrument_id]`: sha256 over `trades.csv` lines `ts_event,instrument_id,price,size,aggressor_side,sequence\n`.
- `shadow_digest`: sha256 over lines `shadow_id,ts_live,ts_first_fill,filled_qty,queue_ahead_at_live,queue_ahead_at_first_fill,reason_class\n`
  sorted by shadow_id; empty string for null; reason_class in {queue_depleted, crossed, never}.
- `queue_trace_digest`: sha256 over lines `shadow_id,ts_event,queue_ahead\n`, shadows sorted by id, rows in file order.
"""  # noqa: E501

from __future__ import annotations  # noqa: I001

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
import itertools

UNDEF = 9223372036854775807
F_LAST = 128
NS = 1_000_000_000

# Per-day layout of the et artifacts. `replay` is the cited full replay (rank oracle on), `fidelity` the mirror-shadow
# run. Instrument ids are ITCH stock-locate codes and differ between days (join across days on symbol).
DAYS = {
    "2019-12-30": {
        "tag": "12302019",
        "itch": "out/itch-12302019",
        "replay": "out/replay-itch-12302019-v2",
        "fidelity": "out/fidelity-itch",
        "midnight_utc_ns": 1577682000000000000,
        "replay_depth_levels": 0,
    },
    "2020-01-30": {
        "tag": "01302020",
        "itch": "out/itch-01302020",
        "replay": "out/replay-itch-01302020",
        "fidelity": "out/fidelity-01302020",
        "midnight_utc_ns": 1580360400000000000,
        "replay_depth_levels": 5,
    },
}
NASDAQ_OPEN_S = 9 * 3600 + 30 * 60
NASDAQ_CLOSE_S = 16 * 3600
CHUNK = 1 << 22


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def file_info(path: Path) -> dict:
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def lines(path: Path):
    """Yield (header, iterator over data lines as bytes incl. newline)."""
    f = open(path, "rb", buffering=CHUNK)
    header = f.readline()
    return header, f


def bucket_key(ts: int, midnight: int) -> str:
    s = (ts - midnight) // NS
    b = (s // 1800) * 1800
    return f"{b // 3600:02d}:{(b % 3600) // 60:02d}"


# ---- scanners --------------------------------------------------------------------------------------------------


def scan_mbo(path: Path) -> dict:
    """et MBO CSV: ts_event,ts_recv,instrument_id,action,side,price,size,order_id,flags,sequence[,channel_id]."""
    header, f = lines(path)
    cols = header.decode().strip().split(",")
    has_channel = "channel_id" in cols
    per = defaultdict(
        lambda: {
            "records": 0,
            "events": 0,
            "actions": Counter(),
            "trade_sides": Counter(),
            "flags": Counter(),
            "ts_first": None,
            "ts_last": None,
            "out_of_order_ts_event": 0,
            "ts_recv_ne_ts_event": 0,
            "max_size": 0,
            "price_min": None,
            "price_max": None,
            "last_has_last_flag": None,
            "seq_first": None,
            "seq_last": None,
            "seq_regressions": 0,
        }
    )
    total = Counter()
    channels = Counter()
    seq_prev_global = None
    seq_regress_global = 0
    for ln in f:
        p = ln.split(b",")
        ts = int(p[0])
        inst = int(p[2])
        act = p[3].decode()
        flags = int(p[8])
        seq = int(p[9])
        d = per[inst]
        d["records"] += 1
        d["actions"][act] += 1
        d["flags"][flags] += 1
        if flags & F_LAST:
            d["events"] += 1
        if act == "T":
            d["trade_sides"][p[4].decode()] += 1
        if d["ts_first"] is None:
            d["ts_first"] = ts
            d["seq_first"] = seq
        elif ts < d["ts_last"]:
            d["out_of_order_ts_event"] += 1
        if d["seq_last"] is not None and seq < d["seq_last"]:
            d["seq_regressions"] += 1
        d["ts_last"] = ts
        d["seq_last"] = seq
        if p[1] != p[0]:
            d["ts_recv_ne_ts_event"] += 1
        sz = int(p[6])
        if sz > d["max_size"]:
            d["max_size"] = sz
        px = int(p[5])
        if px != UNDEF and act in ("A", "M"):
            if d["price_min"] is None or px < d["price_min"]:
                d["price_min"] = px
            if d["price_max"] is None or px > d["price_max"]:
                d["price_max"] = px
        d["last_has_last_flag"] = bool(flags & F_LAST)
        if has_channel:
            channels[p[10].strip().decode()] += 1
        if seq_prev_global is not None and seq < seq_prev_global:
            seq_regress_global += 1
        seq_prev_global = seq
    f.close()
    out = {}
    for inst in sorted(per):
        d = per[inst]
        d["actions"] = dict(sorted(d["actions"].items()))
        d["trade_sides"] = dict(sorted(d["trade_sides"].items()))
        d["flags"] = {str(k): v for k, v in sorted(d["flags"].items())}
        out[str(inst)] = d
        for k in ("records", "events"):
            total[k] += d[k]
        for a, n in d["actions"].items():
            total["action_" + a] += n
    return {
        "header": cols,
        "instruments": sorted(per),
        "records": total["records"],
        "events": total["events"],
        "trades": total["action_T"],
        "fills": total["action_F"],
        "adds": total["action_A"],
        "cancels": total["action_C"],
        "modifies": total.get("action_M", 0),
        "clears": total.get("action_R", 0),
        "sequence_regressions_file_order": seq_regress_global,
        "channel_ids": dict(channels.most_common()) if has_channel else None,
        "per_instrument": out,
    }


def scan_trades(path: Path) -> dict:
    """et trades.csv: ts_event,instrument_id,price,size,aggressor_side,sequence."""
    header, f = lines(path)
    per = Counter()
    sides = Counter()
    hashes: dict[int, hashlib._Hash] = {}
    size_sum = Counter()
    for ln in f:
        p = ln.split(b",")
        inst = int(p[1])
        per[inst] += 1
        sides[p[4].decode()] += 1
        size_sum[inst] += int(p[3])
        h = hashes.get(inst)
        if h is None:
            h = hashes[inst] = hashlib.sha256()
        h.update(ln)
    f.close()
    return {
        "header": header.decode().strip().split(","),
        "rows": sum(per.values()),
        "rows_by_instrument": {str(k): per[k] for k in sorted(per)},
        "size_by_instrument": {str(k): size_sum[k] for k in sorted(size_sum)},
        "aggressor_sides": dict(sorted(sides.items())),
        "trades_rows_sha256": {str(k): hashes[k].hexdigest() for k in sorted(hashes)},
    }


def scan_bbo(path: Path) -> dict:
    """et bbo.csv (one row after every F_LAST record): ts_event,instrument_id,bid_px,bid_sz,ask_px,ask_sz,sequence."""
    header, f = lines(path)
    per = Counter()
    hashes: dict[int, hashlib._Hash] = {}
    change_rows = Counter()
    prev: dict[int, bytes] = {}
    one_sided = Counter()
    crossed = 0
    for ln in f:
        p = ln.split(b",")
        inst = int(p[1])
        per[inst] += 1
        h = hashes.get(inst)
        if h is None:
            h = hashes[inst] = hashlib.sha256()
        h.update(ln)
        key = b",".join(p[2:6])
        if prev.get(inst) != key:
            change_rows[inst] += 1
            prev[inst] = key
        bid, ask = int(p[2]), int(p[4])
        if bid == UNDEF or ask == UNDEF:
            one_sided[inst] += 1
        elif bid >= ask:
            crossed += 1
    f.close()
    return {
        "header": header.decode().strip().split(","),
        "rows": sum(per.values()),
        "rows_by_instrument": {str(k): per[k] for k in sorted(per)},
        "bbo_rows_sha256": {str(k): hashes[k].hexdigest() for k in sorted(hashes)},
        "change_rows_px_sz_only_by_instrument": {str(k): change_rows[k] for k in sorted(change_rows)},
        "rows_with_empty_side_by_instrument": {str(k): one_sided[k] for k in sorted(one_sided)},
        "rows_locked_or_crossed": crossed,
    }


def scan_depth(path: Path) -> dict:
    """et depth.csv (wide, N levels per side, one row after every F_LAST record)."""
    header, f = lines(path)
    cols = header.decode().strip().split(",")
    n_levels = sum(1 for c in cols if c.startswith("bid_px_"))
    per = Counter()
    hashes: dict[int, hashlib._Hash] = {}
    for ln in f:
        inst = int(ln.split(b",", 2)[1])
        per[inst] += 1
        h = hashes.get(inst)
        if h is None:
            h = hashes[inst] = hashlib.sha256()
        h.update(ln)
    f.close()
    return {
        "levels": n_levels,
        "header": cols,
        "rows": sum(per.values()),
        "rows_by_instrument": {str(k): per[k] for k in sorted(per)},
        f"depth{n_levels}_rows_sha256": {str(k): hashes[k].hexdigest() for k in sorted(hashes)},
    }


def relabel_rank_line(p: list[bytes], sweep_map: dict, inst: int) -> bytes:
    m = sweep_map.setdefault(inst, {})
    sid = p[9]
    if sid not in m:
        m[sid] = len(m) + 1
    q = list(p)
    q[9] = str(m[sid]).encode()
    return b",".join(q)


def scan_rank_trace(path: Path, midnight: int) -> dict:
    """et rank_trace.csv: ts_event,instrument_id,sequence,order_id,side,price,rank,rank_sweep_adjusted,level_depth,
    sweep_id,exec_kind. Recomputes the RankOracle aggregates from the rows."""
    header, f = lines(path)
    per = Counter()
    h_rel: dict[int, hashlib._Hash] = {}
    h_nos: dict[int, hashlib._Hash] = {}
    sweep_map: dict = {}
    sweep_sizes: Counter = Counter()
    hist = [0] * 6
    hist_adj = [0] * 6
    level_depth_pos = 0
    at_price = Counter()
    by_bucket = defaultdict(lambda: [0, 0, 0])  # checked, rank0, rank0_adj
    first60 = [0, 0, 0]  # checked, violations raw, violations adj
    open_lo = midnight + NASDAQ_OPEN_S * NS
    open_hi = open_lo + 60 * NS
    per_inst_stats = defaultdict(lambda: Counter())
    for ln in f:
        p = ln.rstrip(b"\n").split(b",")
        ts = int(p[0])
        inst = int(p[1])
        rank = int(p[6])
        radj = int(p[7])
        ld = int(p[8])
        kind = p[10].decode()
        per[inst] += 1
        hist[min(rank, 5)] += 1
        hist_adj[min(radj, 5)] += 1
        if ld > 0:
            level_depth_pos += 1
        at_price[(kind, "n")] += 1
        if radj == 0:
            at_price[(kind, "rank0_adj")] += 1
        if rank == 0:
            at_price[(kind, "rank0")] += 1
        sweep_sizes[(inst, p[9])] += 1
        b = by_bucket[bucket_key(ts, midnight)]
        b[0] += 1
        b[1] += rank == 0
        b[2] += radj == 0
        if open_lo <= ts < open_hi:
            first60[0] += 1
            first60[1] += rank != 0
            first60[2] += radj != 0
        s = per_inst_stats[inst]
        s["checked"] += 1
        s["rank0"] += rank == 0
        s["rank0_adj"] += radj == 0
        s[kind] += 1
        s[kind + "_rank0_adj"] += radj == 0
        hr = h_rel.get(inst)
        if hr is None:
            hr = h_rel[inst] = hashlib.sha256()
            h_nos[inst] = hashlib.sha256()
        hr.update(relabel_rank_line(p, sweep_map, inst) + b"\n")
        h_nos[inst].update(b",".join(p[:9] + p[10:]) + b"\n")
    f.close()
    n = sum(per.values())
    sweeps = len(sweep_sizes)
    multi = sum(1 for v in sweep_sizes.values() if v > 1)
    buckets = {}
    for k in sorted(by_bucket):
        c, r0, ra = by_bucket[k]
        buckets[k] = {
            "checked": c,
            "rank0": r0,
            "rank0_sweep_adjusted": ra,
            "rank0_share": r0 / c,
            "rank0_share_sweep_adjusted": ra / c,
        }
    return {
        "header": header.decode().strip().split(","),
        "rows": n,
        "rows_by_instrument": {str(k): per[k] for k in sorted(per)},
        "fifo_rank_histogram": hist,
        "fifo_rank_histogram_sweep_adjusted": hist_adj,
        "rank0_share": hist[0] / n,
        "rank0_share_sweep_adjusted": hist_adj[0] / n,
        "price_priority_violations_rows_level_depth_gt0": level_depth_pos,
        "sweeps_distinct_ids": sweeps,
        "sweeps_multi_fill": multi,
        "exec_kind": {
            k: {
                "n": at_price[(k, "n")],
                "rank0": at_price[(k, "rank0")],
                "rank0_sweep_adjusted": at_price[(k, "rank0_adj")],
                "rank0_share_sweep_adjusted": at_price[(k, "rank0_adj")] / at_price[(k, "n")],
            }
            for k in sorted({k for k, _ in at_price})
        },
        "first_60s_after_open": {
            "checked": first60[0],
            "rank_violations": first60[1],
            "rank_violations_sweep_adjusted": first60[2],
            "rank0_share": 1 - first60[1] / first60[0] if first60[0] else 1.0,
            "rank0_share_sweep_adjusted": 1 - first60[2] / first60[0] if first60[0] else 1.0,
        },
        "rank0_share_by_bucket": buckets,
        "per_instrument": {str(k): dict(per_inst_stats[k]) for k in sorted(per_inst_stats)},
        "rank_rows_sha256_relabelled": {str(k): h_rel[k].hexdigest() for k in sorted(h_rel)},
        "rank_rows_sha256_no_sweep": {str(k): h_nos[k].hexdigest() for k in sorted(h_nos)},
    }


def reason_class(r: str) -> str:
    return {
        "queue_depleted": "queue_depleted",
        "crossed": "crossed",
        "never": "never",
        "trade_through": "crossed",
        "crossed_at_live": "crossed",
    }.get(r, r)


def scan_shadows(shadows_csv: Path, fills_csv: Path, trace_csv: Path | None) -> dict:
    sh = {}
    header, f = lines(shadows_csv)
    for ln in f:
        p = ln.decode().rstrip("\n").split(",")
        sh[p[0]] = p
    f.close()
    cols = header.decode().strip().split(",")
    n_cancel = sum(1 for p in sh.values() if p[7] != "")
    lat = Counter(p[6] for p in sh.values())
    by_inst = Counter(p[1] for p in sh.values())
    by_side = Counter(p[2] for p in sh.values())

    header2, f = lines(fills_csv)
    rows = {}
    for ln in f:
        p = ln.decode().rstrip("\n").split(",")
        rows[p[0]] = p
    f.close()
    reasons = Counter(r[11] for r in rows.values())
    filled = [r for r in rows.values() if int(r[8]) > 0]
    partial = [r for r in filled if int(r[8]) < int(r[4])]
    crossed_at_live = [r for r in rows.values() if r[11] == "crossed" and r[7] == r[6]]
    h = hashlib.sha256()
    for sid in sorted(rows):
        r = rows[sid]
        h.update(f"{sid},{r[6]},{r[7]},{r[8]},{r[9]},{r[10]},{reason_class(r[11])}\n".encode())
    cancelled_before_fill = sum(1 for sid, r in rows.items() if sh[sid][7] != "" and int(r[8]) == 0)
    res = {
        "shadows_csv_header": cols,
        "n_shadows": len(sh),
        "n_with_ts_cancel": n_cancel,
        "latency_ns_values": dict(lat),
        "by_instrument": dict(sorted(by_inst.items())),
        "by_side": dict(sorted(by_side.items())),
        "shadow_fills_header": header2.decode().strip().split(","),
        "n_rows": len(rows),
        "ids_equal": set(rows) == set(sh),
        "n_filled": len(filled),
        "n_partial_only": len(partial),
        "fill_reason_counts": dict(sorted(reasons.items())),
        "n_crossed_at_live": len(crossed_at_live),
        "sum_qty": sum(int(r[4]) for r in rows.values()),
        "sum_filled_qty": sum(int(r[8]) for r in rows.values()),
        "sum_queue_ahead_at_submit": sum(int(r[9]) for r in rows.values() if r[9] != ""),
        "n_unfilled_with_ts_cancel": cancelled_before_fill,
        "shadow_digest": h.hexdigest(),
    }
    if trace_csv is not None and trace_csv.exists():
        header3, f = lines(trace_csv)
        tr = defaultdict(list)
        n = 0
        for ln in f:
            p = ln.decode().rstrip("\n").split(",")
            tr[p[1]].append((int(p[0]), int(p[2])))
            n += 1
        f.close()
        ht = hashlib.sha256()
        first_ts_eq_live = first_val_eq_qa = nonincreasing = 0
        traced_filled = traced_unfilled = 0
        for sid in sorted(tr):
            seq = tr[sid]
            for ts, qa in seq:
                ht.update(f"{sid},{ts},{qa}\n".encode())
            r = rows.get(sid)
            if r is not None:
                first_ts_eq_live += seq[0][0] == int(r[6])
                first_val_eq_qa += r[9] != "" and seq[0][1] == int(r[9])
                if int(r[8]) > 0:
                    traced_filled += 1
                else:
                    traced_unfilled += 1
            nonincreasing += all(b[1] <= a[1] for a, b in itertools.pairwise(seq))
        res["queue_trace"] = {
            "header": header3.decode().strip().split(","),
            "rows": n,
            "shadows": len(tr),
            "traced_shadows_filled": traced_filled,
            "traced_shadows_unfilled": traced_unfilled,
            "first_row_ts_equals_ts_live": first_ts_eq_live,
            "first_row_value_equals_queue_ahead_at_submit": first_val_eq_qa,
            "shadows_with_nonincreasing_trace": nonincreasing,
            "queue_trace_digest": ht.hexdigest(),
            "semantics": "et emits (ts, queue_ahead) when the value differs from the last emitted value, checked "
            "after activation (ts = ts_live) and after every event end while the shadow is still "
            "live; the event that fully fills or a cancel that ends the shadow emits nothing",
        }
    return res


def scan_real_orders(path: Path) -> dict:
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    t = pq.read_table(
        path,
        columns=[
            "instrument_id",
            "side",
            "outcome",
            "filled_qty",
            "n_fills",
            "size",
            "priority_lost",
            "n_modify",
            "ts_add",
            "queue_ahead",
        ],
    )
    out = {"rows": t.num_rows}
    vc = pc.value_counts(t["outcome"]).to_pylist()
    out["outcome_counts"] = {d["values"]: d["counts"] for d in sorted(vc, key=lambda d: d["values"])}
    out["sum_n_fills"] = pc.sum(t["n_fills"]).as_py()
    out["sum_filled_qty"] = pc.sum(t["filled_qty"]).as_py()
    out["sum_size"] = pc.sum(t["size"]).as_py()
    out["n_with_fill"] = pc.sum(pc.greater(t["n_fills"], 0)).as_py()
    out["n_priority_lost"] = pc.sum(t["priority_lost"].cast("int64")).as_py()
    out["n_modify_sum"] = pc.sum(t["n_modify"]).as_py()
    out["n_queue_ahead_zero"] = pc.sum(pc.equal(t["queue_ahead"], 0)).as_py()
    vc = pc.value_counts(t["instrument_id"]).to_pylist()
    out["by_instrument"] = {str(d["values"]): d["counts"] for d in sorted(vc, key=lambda d: d["values"])}
    out["columns"] = pq.ParquetFile(path).schema_arrow.names
    return out


def scan_crypto(et: Path, venue: str) -> dict:
    d = et / "out" / venue
    rr = json.loads((d / "engine" / "run_report.json").read_text())
    stats = dict(rr["summary"]["stats"])
    stats.pop("rank_oracle", None)
    verdict = json.loads((d / f"{venue}_verdict.json").read_text())
    conv = json.loads((d / "convert_stats.json").read_text())
    mbo = scan_mbo(d / "mbo.csv")
    for v in mbo["per_instrument"].values():
        v.pop("flags", None)
    tol = verdict["tolerance"]
    cap = sorted((et / "data" / venue).iterdir())[-1]
    return {
        "capture_dir": str(cap.relative_to(et)),
        "capture_files": {
            p.name: {"bytes": p.stat().st_size, "lines": sum(1 for _ in open(p, "rb")), "sha256": sha256_file(p)}
            for p in sorted(cap.iterdir())
        },
        "converted_mbo": {
            "records": mbo["records"],
            "events": mbo["events"],
            "trades": mbo["trades"],
            "actions": mbo["per_instrument"]["1"]["actions"],
            "sha256": sha256_file(d / "mbo.csv"),
        },
        "engine_stats": stats,
        "engine_anomalies": rr.get("anomalies"),
        "parity": {k: v for k, v in (rr.get("parity") or {}).items() if k != "replay"},
        "convert_stats": conv,
        "verdict": {
            "status": verdict["status"],
            "exact_pass": verdict["exact_pass"],
            "bbo_rows": tol["bbo"]["rows"],
            "bbo_exact": tol["bbo"]["exact"],
            "bbo_within_500ms": tol["bbo"]["within_tolerance"],
            "bbo_share_exact": verdict["bbo_match_share_exact"],
            "bbo_share_within_500ms": verdict["bbo_match_share_within_tolerance"],
            "depth10_share_within_500ms": verdict["depth_match_share_within_tolerance"],
        },
    }


def breadth_rows(et: Path, tag: str, symbols: list[str]) -> dict:
    import csv

    p = et / "out" / "real-breadth" / tag / "summary.csv"
    with open(p) as f:
        rows = {r["symbol"]: r for r in csv.DictReader(f)}
    agg = json.loads((et / "out" / "real-breadth" / tag / "summary.json").read_text())["aggregate"]
    return {"source": str(p.relative_to(et)), "aggregate": agg, "symbols": {s: rows[s] for s in symbols if s in rows}}


def extra_checks(et: Path) -> dict:
    """README claims that live outside the four main artifact families: OCaml twin identity and the 118-symbol-day
    breadth run (per-symbol invariants / parity files)."""
    out: dict = {"ocaml": {}, "breadth_all_symbol_days": {}}
    for tag, rep in (("12302019", "replay-itch-12302019-v2"), ("01302020", "replay-itch-01302020")):
        o = et / "out"
        eng = o / f"ocaml-engine-{tag}"
        rank_sorted = {}
        for name, path in (
            ("rust", o / rep / "rank_trace.csv"),
            ("ocaml", o / f"ocaml-oracle-{tag}" / "rank_trace.csv"),
        ):
            with open(path, "rb") as f:
                f.readline()
                rank_sorted[name] = hashlib.sha256(b"".join(sorted(f))).hexdigest()
        out["ocaml"][tag] = {
            "bbo_identical": sha256_file(o / rep / "bbo.csv") == sha256_file(eng / "bbo.csv"),
            "trades_identical": sha256_file(o / rep / "trades.csv") == sha256_file(eng / "trades.csv"),
            "rank_trace_identical_after_sort": rank_sorted["rust"] == rank_sorted["ocaml"],
        }
    n = bad = recs = ex = 0
    for d in sorted((et / "out" / "real-breadth").glob("*/symbols/*")):
        inv = json.loads((d / "invariants.json").read_text())
        par = json.loads((d / "parity_report.json").read_text())
        n += 1
        keys = [
            "cancels_exceeding_resting",
            "crossed_books_after_last",
            "duplicate_adds",
            "fills_exceeding_resting",
            "fills_unknown_order",
            "negative_spreads",
            "unexpected_modifies",
            "unknown_order_refs",
            "price_priority_violations",
            "executions_unknown_order",
        ]
        if any(inv.get(k) for k in keys) or par.get("trades_mismatches") or not par.get("pass"):
            bad += 1
        recs += par["replay"]["records"]
        ex += inv["executions_checked"]
    out["breadth_all_symbol_days"] = {
        "symbol_days": n,
        "with_nonzero_invariant_or_parity_mismatch": bad,
        "records": recs,
        "executions_checked": ex,
    }
    return out


# ---- main ------------------------------------------------------------------------------------------------------


def compute(et: Path, with_depth: bool = True) -> dict:
    t0 = time.time()
    doc: dict = {
        "schema": "qtdev.et_expected",
        "schema_version": 1,
        "et_root": "<et-root>",
        "et_head": "06b2396",
        "generated_by": "scripts/realdata/et_reference.py",
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "conventions": __doc__,
        "days": {},
    }
    for date, cfg in DAYS.items():
        print(f"[{date}] scanning", file=sys.stderr)
        itch, rep, fid = et / cfg["itch"], et / cfg["replay"], et / cfg["fidelity"]
        midnight = cfg["midnight_utc_ns"]
        day: dict = {
            "session_date": date,
            "midnight_utc_ns": midnight,
            "open_s": NASDAQ_OPEN_S,
            "close_s": NASDAQ_CLOSE_S,
        }
        inst_csv = (itch / "instruments.csv").read_text().splitlines()
        day["instruments"] = {r.split(",")[0]: r.split(",")[1] for r in inst_csv[1:]}
        files = {}
        for p in [
            itch / "mbo.csv",
            itch / "trades.csv",
            itch / "instruments.csv",
            itch / "stats.json",
            rep / "bbo.csv",
            rep / "trades.csv",
            rep / "rank_trace.csv",
            rep / "invariants.json",
            rep / "parity_report.json",
            rep / "run_report.json",
            fid / "shadows.csv",
            fid / "engine" / "shadow_fills.csv",
            fid / "engine" / "queue_trace.csv",
            fid / "engine" / "bbo.csv",
            fid / "engine" / "trades.csv",
            fid / "engine_pass1" / "bbo.csv",
            fid / "real_orders.parquet",
            fid / "fidelity.json",
            fid / "calibration_real_v2.json",
        ]:
            print(f"  sha256 {p.relative_to(et)}", file=sys.stderr)
            files[str(p.relative_to(et))] = {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
        day["files"] = files
        print("  mbo", file=sys.stderr)
        day["mbo"] = scan_mbo(itch / "mbo.csv")
        print("  trades", file=sys.stderr)
        day["trades_source"] = scan_trades(itch / "trades.csv")
        day["trades_derived"] = scan_trades(rep / "trades.csv")
        print("  bbo", file=sys.stderr)
        day["bbo"] = scan_bbo(rep / "bbo.csv")
        print("  rank_trace", file=sys.stderr)
        day["rank_trace"] = scan_rank_trace(rep / "rank_trace.csv", midnight)
        inv = json.loads((rep / "invariants.json").read_text())
        inv.pop("examples", None)
        day["et_invariants_json"] = inv
        day["et_parity_report"] = {
            k: v
            for k, v in json.loads((rep / "parity_report.json").read_text()).items()
            if k not in ("replay", "examples")
        }
        rr = json.loads((rep / "run_report.json").read_text())["summary"]["stats"]
        rr.pop("rank_oracle", None)
        day["et_run_report_stats"] = rr
        day["et_itch_stats"] = json.loads((itch / "stats.json").read_text())
        print("  shadows", file=sys.stderr)
        day["shadows"] = scan_shadows(
            fid / "shadows.csv", fid / "engine" / "shadow_fills.csv", fid / "engine" / "queue_trace.csv"
        )
        fj = json.loads((fid / "fidelity.json").read_text())
        day["et_fidelity_json"] = fj
        cal = json.loads((fid / "calibration_real_v2.json").read_text())
        day["et_calibration_v2"] = {
            "n_orders": cal["n_orders"],
            "n_train": cal["n_train"],
            "n_val": cal["n_val"],
            "n_test": cal["n_test"],
            "post_method": cal["post_method"],
            "brier_skill_post": {
                h: {
                    "value": v["brier_skill"],
                    "ci": v["brier_skill_ci"],
                    "vs_back_of_queue": v.get("brier_skill_vs_baseline"),
                    "base_rate": v["base_rate"],
                }
                for h, v in cal["post"].items()
            },
        }
        print("  real_orders", file=sys.stderr)
        day["real_orders"] = scan_real_orders(fid / "real_orders.parquet")
        if with_depth:
            print("  depth (fidelity engine_pass1, 3 levels)", file=sys.stderr)
            day["depth_pass1"] = scan_depth(fid / "engine_pass1" / "depth.csv")
            day["depth_pass1"]["file"] = str((fid / "engine_pass1" / "depth.csv").relative_to(et))
            if cfg["replay_depth_levels"]:
                print("  depth (replay, 5 levels)", file=sys.stderr)
                day["depth_replay"] = scan_depth(rep / "depth.csv")
                day["depth_replay"]["file"] = str((rep / "depth.csv").relative_to(et))
        day["breadth_cross_check"] = breadth_rows(et, cfg["tag"], ["AAPL", "MSFT", "SPY", "TSLA"])
        doc["days"][date] = day
    print("[crypto]", file=sys.stderr)
    doc["crypto"] = {v: scan_crypto(et, v) for v in ("bitstamp", "bitfinex")}
    doc["crypto"]["fidelity_summary"] = json.loads((et / "out" / "fidelity-crypto-summary.json").read_text())
    print("[extra]", file=sys.stderr)
    doc["extra"] = extra_checks(et)
    doc["timings_s"] = round(time.time() - t0, 1)
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--et-root",
        type=Path,
        default=os.environ.get("QT_ET_ROOT"),
        required=not os.environ.get("QT_ET_ROOT"),
        help="local checkout of the et prototype (default: $QT_ET_ROOT)",
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-depth", action="store_true", help="skip the 2-4 GB depth.csv scans")
    a = ap.parse_args(argv)
    doc = compute(a.et_root, with_depth=not a.no_depth)
    a.out.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n")
    print(f"wrote {a.out} in {doc['timings_s']} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
