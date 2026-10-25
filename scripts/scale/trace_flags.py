#!/usr/bin/env python3
"""Trace a W1 day's flagged units to symbols, ITCH messages and causes.

    uv run --frozen --no-sync python scripts/scale/trace_flags.py --plan plan.json --shards SHARDS \\
        --split SPLIT_DIR --out TRACE_DIR [--unit UNIT_ID ...]
    uv run --frozen --no-sync python scripts/scale/trace_flags.py --plan plan.json --shards SHARDS \\
        --restream-command SPLIT/<date>/split_manifest.json --out NEW_SPLIT     # step 1's re-stream command

For every unit whose shard flags a replay check (a crossed or locked book, a price-priority violation, an unknown
order, a trade-parity failure; `symbol_exactness.FLAG_FIELDS`), or for the `--unit`s given:

1. checks the bucket file against the unit spec's sha256 (re-stream the bucket with `itch_split.py --symbols` and
   the same `--buckets`: a bucket's bytes depend only on its symbols' messages; `--restream-command` prints the
   command for the traced units);
2. ingests the unit's symbols as `unit.py` does, replays them and checks that every summable counter equals the
   shard's `exactness` (the pilot result is reproduced, not re-derived);
3. replays every symbol alone (`symbol_exactness.attribute`), which names the flagged symbols and checks that the
   per-symbol counters add up to the unit's;
4. rebuilds every symbol's book independently from the raw ITCH messages (`itch_ref.scan`) and compares its events,
   executions, crossed / negative-spread events and price-priority violations with the engine's per symbol, checks
   that no engine record references an unknown order (the reference yields none) and that the ingest's count of
   unknown-order messages equals the reference's: a difference is a suspected engine (or ingest) defect;
5. classifies each crossed event and each violation by the symbol's trading state at that message (`itch_ref.CAUSES`)
   and keeps the evidence: directory entry, `H`/`K`/`J`/`h` timeline, `Q` cross prints, crossed episodes with their
   opening and closing messages, violations with their messages;
6. draws the unit's shadows of each flagged symbol again (`per_partition` draws are per symbol-day) and counts those
   submitted on a crossed book or while the symbol was not trading (`shadow_exposure`): what the flag means for
   the atlas cells of that symbol-day.

Outputs in TRACE_DIR: `<unit_id>.json` per unit, `symbol_flags.json` (`qt.scale.symbol_flags`: per-unit
attribution and per-symbol flags and causes, the input of `atlas.py reduce-day --symbol-flags` for shards written
before units attributed their flags themselves) and `summary.md` (one row per flagged symbol). Exit code 0 when every
traced unit reproduced its shard, attributed completely and agreed with the reference; 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as K  # noqa: E402
import itch_ref as R  # noqa: E402
import symbol_exactness as SE  # noqa: E402

SYMBOL_FLAGS_SCHEMA = "qt.scale.symbol_flags"
#: Engine counter -> reference counter compared per symbol.
COMPARED = {
    "stats.events": "events",
    "executions_checked": "executions_checked",
    "invariants.crossed_books_after_last": "crossed_books_after_last",
    "invariants.negative_spreads": "negative_spreads",
    "price_priority_violations": "price_priority_violations",
}
#: Engine counters of records that reference an unknown order. The reference, like the ingest's mapper, yields no
#: record for an `E`/`C`/`X`/`D`/`U` of an unknown order (it counts the message in `unknown_order_refs`, as the
#: ingest counts it in `data_quality.json` `itch_unknown_order_ref`), so these must be zero for every symbol.
UNKNOWN_ORDER_COUNTERS = ("invariants.unknown_order_refs", "invariants.fills_unknown_order", "executions_unknown_order")


def _get(ex: dict[str, Any], dotted: str) -> Any:
    cur: Any = ex
    for part in dotted.split("."):
        cur = (cur or {}).get(part)
    return cur


def _find(doc: Any, key: str) -> Any:
    """The first value of `key` anywhere in a JSON document (data_quality.json nests its source counters)."""
    if isinstance(doc, dict):
        if key in doc:
            return doc[key]
        doc = list(doc.values())
    if isinstance(doc, list):
        for v in doc:
            if (got := _find(v, key)) is not None:
                return got
    return None


def primary_cause(causes: dict[str, int]) -> str | None:
    """The cause with the most events (ties: `itch_ref.CAUSES` order, most specific first)."""
    if not causes:
        return None
    order = list(R.CAUSES)
    return min(causes, key=lambda c: (-causes[c], order.index(c) if c in order else len(order)))


def compare(engine: dict[str, dict[str, Any]], ref: R.Scan) -> dict[str, list[str]]:
    """Per symbol, the compared counters where the engine and the reference differ (`{}` when all agree)."""
    by_sym = {t.symbol: t for t in ref.symbols.values()}
    out: dict[str, list[str]] = {}
    for sym in sorted(set(engine) | set(by_sym)):
        ex, t = engine.get(sym), by_sym.get(sym)
        if ex is None and t is not None and t.events == 0:
            continue  # a directory entry without records: the ingest writes no partition (leaf `missing_symbols`)
        if ex is None or t is None:
            out[sym] = ["missing from the " + ("engine" if ex is None else "reference")]
            continue
        rc = R.symbol_json(t, 0, 0)["counts"]
        bad = [f"{e}: engine {_get(ex, e)} != reference {rc[r]}" for e, r in COMPARED.items() if _get(ex, e) != rc[r]]
        bad += [f"{e}: engine {_get(ex, e)} != reference 0" for e in UNKNOWN_ORDER_COUNTERS if _get(ex, e)]
        if bad:
            out[sym] = bad
    return out


def shadow_exposure(ds: Path, unit: dict[str, Any], traces: dict[str, R.SymbolTrace], wd: Path) -> dict[str, Any]:
    """Per flagged symbol and shadow population: the unit's shadows (drawn again: `per_partition` draws are seeded
    per symbol-day, so these are the shadows the unit scored), how many were submitted on a crossed or locked book
    or while the symbol was halted, paused or not yet trading (`H` state other than T), and how many the exact fill
    rule fills, first while the symbol was not trading (finding F2 in `findings/README.md`: the venue matches
    nothing then, so those fills are not real)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    import queuetruth as qt

    if not traces:
        return {}
    inst = pq.read_table(ds / "instruments.parquet").to_pylist()
    midnight = {r["instrument_id"]: r["midnight_utc_ns"] for r in inst}
    by_loc = {unit["locates"][s]: t for s, t in traces.items()}
    out: dict[str, dict[str, Any]] = {s: {} for s in traces}
    for pop, spec in unit["populations"].items():
        quotas = {str(unit["locates"][s]): int(unit["quotas"].get(pop, {}).get(s, 0)) for s in traces}
        if not any(quotas.values()):
            continue
        sh = wd / f"exposure-{pop}.parquet"
        qt.sample_shadows(
            {
                "dataset": {"path": str(ds)},
                "out": str(sh),
                "n": 0,
                "seed": unit["seed"],
                "allocation": "per_partition",
                "partition_quotas": quotas,
                "placements": spec["placements"],
                "qty": spec["qty"],
            }
        )
        tab = pq.read_table(sh, columns=["instrument_id", "ts_submit"])
        iids = tab.column("instrument_id").to_pylist()
        subs = tab.column("ts_submit").cast(pa.int64()).to_pylist()  # a timestamp column: UTC ns
        for t in traces.values():
            out[t.symbol][pop] = {
                "shadows": 0,
                "in_crossed_book": 0,
                "in_halt": 0,
                "filled": 0,
                "first_fill_in_halt": 0,
                "first_fill_in_halt_by_reason": {},
            }
        for iid, sub in zip(iids, subs, strict=True):
            t = by_loc[iid]
            ns = sub - midnight[iid]
            e = out[t.symbol][pop]
            e["shadows"] += 1
            e["in_crossed_book"] += R.in_spans(ns, t.crossed_spans)
            e["in_halt"] += R.in_spans(ns, t.halt_spans)
        # the exact fill rule's verdict on these shadows (replay defaults: latency 0, fill rule exact)
        rp = wd / f"exposure-{pop}-replay"
        qt.replay({"dataset": {"path": str(ds)}, "out": str(rp), "shadows": {"path": str(sh)}})
        res = pq.read_table(
            rp / "shadow_results.parquet", columns=["instrument_id", "ts_first_fill", "first_fill_reason"]
        )
        firsts = res.column("ts_first_fill").cast(pa.int64()).to_pylist()
        for iid, first, why in zip(
            res.column("instrument_id").to_pylist(), firsts, res.column("first_fill_reason").to_pylist(), strict=True
        ):
            if first is None:
                continue
            t = by_loc[iid]
            e = out[t.symbol][pop]
            e["filled"] += 1
            if R.in_spans(first - midnight[iid], t.halt_spans):
                e["first_fill_in_halt"] += 1
                e["first_fill_in_halt_by_reason"][why] = e["first_fill_in_halt_by_reason"].get(why, 0) + 1
        shutil.rmtree(rp, ignore_errors=True)
        sh.unlink(missing_ok=True)
    return out


def trace_unit(
    unit: dict[str, Any], shard: dict[str, Any], split: Path, work: Path | None, threads: int, verify: bool = True
) -> dict[str, Any]:
    import queuetruth as qt

    t_start = time.perf_counter()
    bucket = split / unit["bucket"]["file"]
    if not bucket.exists():
        raise FileNotFoundError(f"{bucket}: re-stream it with itch_split.py (same --buckets, --symbols of the bucket)")
    got = K.sha256_file(bucket)
    if verify and unit["bucket"].get("file_sha256") and got != unit["bucket"]["file_sha256"]:
        raise ValueError(f"{bucket.name}: sha256 {got} != unit spec {unit['bucket']['file_sha256']}")
    locates: dict[str, int] = unit["locates"]
    loc2sym = {v: k for k, v in locates.items()}
    ref = R.scan(bucket, locates=locates)
    wd = Path(tempfile.mkdtemp(prefix=f"qt-trace-{unit['unit_id']}-", dir=work))
    try:
        ds = wd / "ds"
        qt.ingest(
            {
                "format": "itch",
                "inputs": [{"path": str(bucket)}],
                "out": str(ds),
                "symbols": unit["symbols"],
                **unit.get("ingest", K.INGEST_DEFAULTS),
            }
        )
        dq = json.loads((ds / "data_quality.json").read_text()) if (ds / "data_quality.json").exists() else {}
        ingest_unknown = int(_find(dq, "itch_unknown_order_ref") or 0)
        whole = SE.replay_counts(ds, wd / "replay", threads)
        shutil.rmtree(wd / "replay", ignore_errors=True)
        published = (shard.get("leaf") or {}).get("exactness") or {}
        reproduced = SE.sum_check(published, {"unit": whole}) if published else ["shard has no exactness"]
        attr = SE.attribute(ds, whole, loc2sym, threads, wd, full=True)
        flagged = {t.symbol: t for t in ref.symbols.values() if t.symbol in attr["symbols"]}
        exposure = shadow_exposure(ds, unit, flagged, wd)
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    engine = attr.pop("counts")
    diff = compare(engine, ref)
    ref_unknown = sum(t.unknown_refs for t in ref.symbols.values())
    if ingest_unknown != ref_unknown:  # message-level unknown-order references: the unit's ingest vs the reference
        diff["(unit)"] = [
            f"ingest itch_unknown_order_ref {ingest_unknown} != reference unknown_order_refs {ref_unknown}"
        ]
    symbols = {}
    for t in sorted(ref.symbols.values(), key=lambda x: x.symbol):
        flags = attr["symbols"].get(t.symbol, {})
        if not flags and not (t.crossed or t.ppv or t.unknown_refs):
            continue
        causes: dict[str, int] = {}
        for k, v in (*t.crossed_by_cause.items(), *t.ppv_by_cause.items()):
            causes[k] = causes.get(k, 0) + v
        symbols[t.symbol] = {
            "engine_flags": flags,
            "engine_matches_reference": t.symbol not in diff,
            "differences": diff.get(t.symbol, []),
            "primary_cause": primary_cause(causes),
            "causes": dict(sorted(causes.items())),
            "shadow_exposure": exposure.get(t.symbol),
            **R.symbol_json(t, 200, 400),
        }
    return {
        "unit_id": unit["unit_id"],
        "bucket": {
            "file": bucket.name,
            "file_sha256": got,
            "matches_unit_spec": got == unit["bucket"].get("file_sha256"),
        },
        "shard_leaf_hash": shard.get("leaf_hash"),
        "shard_exactness_flags": SE.flags_of(published),
        "reproduced": not reproduced,
        "reproduction_mismatches": reproduced,
        "attribution": attr,
        "reference": {
            "messages": ref.messages,
            "sequence": ref.sequence,
            "system_events": ref.to_json()["system_events"],
        },
        "engine_vs_reference_differences": diff,
        "symbols": symbols,
        "wall_s": round(time.perf_counter() - t_start, 1),
    }


def symbol_flags_doc(plan: dict[str, Any], traces: list[dict[str, Any]]) -> dict[str, Any]:
    """The `qt.scale.symbol_flags` sidecar: per unit the attribution, per symbol its flags and causes."""
    syms: dict[str, Any] = {}
    for tr in traces:
        for s, d in tr["symbols"].items():
            if not d["engine_flags"]:
                continue
            syms[s] = {
                "unit_id": tr["unit_id"],
                "flags": d["engine_flags"],
                "causes": d["causes"],
                "primary_cause": d["primary_cause"],
                "engine_matches_reference": d["engine_matches_reference"],
            }
    return {
        "schema": SYMBOL_FLAGS_SCHEMA,
        "schema_version": 1,
        "run_id": plan["run_id"],
        "date": plan["sources"][0]["date"] if plan.get("sources") else None,
        "units": {
            tr["unit_id"]: {
                "shard_leaf_hash": tr["shard_leaf_hash"],
                "reproduced": tr["reproduced"],
                "exactness_by_symbol": {k: v for k, v in tr["attribution"].items() if k != "counts"},
            }
            for tr in traces
        },
        "symbols": dict(sorted(syms.items())),
        "causes": R.CAUSES,
    }


def summary_md(traces: list[dict[str, Any]]) -> str:
    lines = [
        "| unit | symbol | crossed | negative | ppv | primary cause | causes | engine = reference |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for tr in traces:
        for s, d in tr["symbols"].items():
            f = d["engine_flags"]
            lines.append(
                f"| {tr['unit_id']} | {s} | {f.get('crossed_books_after_last', 0)} | {f.get('negative_spreads', 0)} "
                f"| {f.get('price_priority_violations', 0)} | {d['primary_cause']} | "
                + ", ".join(f"{k} {v}" for k, v in d["causes"].items())
                + f" | {'yes' if d['engine_matches_reference'] else 'NO'} |"
            )
    return "\n".join(lines) + "\n"


def restream_command(plan: dict[str, Any], split_manifest: dict[str, Any], unit_ids: list[str], out: str) -> dict:
    """The `itch_split.py` invocation that re-creates the buckets of `unit_ids` byte for byte (every symbol of those
    buckets, the same `--buckets`), with each bucket's expected sha256 from the unit specs."""
    units = {u["unit_id"]: u for u in plan["units"]}
    files = sorted({units[u]["bucket"]["file"] for u in unit_ids})
    by_file = {b["file"]: b for b in split_manifest["buckets"]}
    missing = [f for f in files if f not in by_file]
    if missing:
        raise ValueError(f"buckets not in the split manifest: {missing}")
    syms = sorted({s for f in files for s in by_file[f]["symbols"]})
    date, k = split_manifest["date"], len(split_manifest["buckets"])
    return {
        "command": f"python3 -I scripts/scale/itch_split.py --date {date} --buckets {k} --out {out} --symbols "
        + ",".join(syms),
        "symbols": len(syms),
        "expected_sha256": {
            f: next(u["bucket"]["file_sha256"] for u in units.values() if u["bucket"]["file"] == f) for f in files
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--shards", type=Path, required=True)
    ap.add_argument("--split", type=Path, help="directory holding the (re-streamed) bucket files")
    ap.add_argument("--out", type=Path, help="trace directory (with --restream-command: the itch_split.py --out)")
    ap.add_argument(
        "--restream-command",
        type=Path,
        metavar="SPLIT_MANIFEST",
        help="print the itch_split.py command that re-creates the traced units' buckets (from the day's "
        "split_manifest.json) and their expected sha256, and exit",
    )
    ap.add_argument("--unit", action="append", default=[], help="trace this unit (default: every flagged unit)")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--work", type=Path, default=None)
    a = ap.parse_args(argv)
    plan = json.loads(a.plan.read_text())
    units = {u["unit_id"]: u for u in plan["units"]}
    shards = {}
    for uid in units:
        p = a.shards / uid / "shard_manifest.json"
        if p.exists():
            shards[uid] = json.loads(p.read_text())
    if a.unit:
        unknown = sorted(set(a.unit) - set(units))
        if unknown:
            ap.error(f"--unit: not in the plan: {unknown}")
        todo = a.unit
    else:
        todo = sorted(uid for uid, m in shards.items() if SE.is_flagged(m["leaf"].get("exactness")))
    if a.restream_command:
        sm = json.loads(a.restream_command.read_text())
        print(json.dumps(restream_command(plan, sm, todo, str(a.out or "SPLIT")), indent=1))
        return 0
    if a.split is None or a.out is None:
        ap.error("--split and --out are required to trace")
    a.out.mkdir(parents=True, exist_ok=True)
    traces = []
    for uid in todo:
        tr = trace_unit(units[uid], shards.get(uid, {}), a.split, a.work, a.threads)
        (a.out / f"{uid}.json").write_text(json.dumps(tr, indent=1) + "\n")
        traces.append(tr)
        print(
            json.dumps(
                {
                    "unit_id": uid,
                    "reproduced": tr["reproduced"],
                    "attribution_complete": tr["attribution"]["complete"],
                    "flagged_symbols": {s: d["primary_cause"] for s, d in tr["symbols"].items() if d["engine_flags"]},
                    "engine_vs_reference": tr["engine_vs_reference_differences"] or "agree",
                    "wall_s": tr["wall_s"],
                }
            ),
            flush=True,
        )
    (a.out / "symbol_flags.json").write_text(json.dumps(symbol_flags_doc(plan, traces), indent=1) + "\n")
    (a.out / "summary.md").write_text(summary_md(traces))
    ok = all(
        tr["reproduced"] and tr["attribution"]["complete"] and not tr["engine_vs_reference_differences"]
        for tr in traces
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
