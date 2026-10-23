#!/usr/bin/env python3
"""The multi-day Fill-Model Bias Atlas from W1 shards: a per-day reducer and a cross-day combiner.

    uv run --frozen --no-sync python scripts/scale/atlas.py reduce-day --plan plan.json --shards DIR --out DAY_DIR
    uv run --frozen --no-sync python scripts/scale/atlas.py combine --day DAY_DIR [--day ...] --out ATLAS_DIR

`aggregate.py` holds a whole run in memory; a full-market day at symbol-day granularity is ~51M stats rows, so the
atlas reduces each day on its own first (streaming one shard at a time) and combines the small per-day tables.

reduce-day (one plan = one day): verifies every shard as `aggregate.py` does (leaf hash, unit spec hash, output
content digests), then writes
- `cells_all.parquet`: stratum `all`, summed over blocks, one row per (mode, shadow population, model, population,
  horizon, symbol-day) with the summable fields of `queuetruth.eval.blocks` plus the symbol's liquidity `tier`
  (`plan.py`: rank by the day's executions);
- `strata_by_tier.parquet`: the queue-decile and liquidity-bucket strata summed over the symbols of each tier;
- `day_manifest.json`: run id, date, the day's Merkle root over its leaves (with proofs), invalid / missing shards,
  units whose replay checks flagged anything, summed exactness per tier, #312 abort counts, telemetry totals.

combine (any number of days):
- `leaderboard_by_tier.parquet`: per (mode, shadow population, tier incl. `ALL`, model, horizon) the exact fill rate,
  model fill rate and fill-rate bias (never-cancel population) with 95% CIs from the two-way pigeonhole bootstrap
  over dates x symbols (`blocks.cluster_bootstrap`), `overfill_bias` at the last horizon, and BH-adjusted p-values
  of the bias across models and horizons within (mode, population, tier);
- `per_symbol.parquet`: per symbol, model, horizon, mode and population: the cross-day bias with a day-cluster
  bootstrap CI (symbols seen on >= 2 days), the empirical-Bayes estimate shrunk toward its tier (DerSimonian-Laird),
  and BH-adjusted p-values across symbols per (mode, population, model, horizon);
- `maps_by_tier.parquet`: bias per tier x queue decile / liquidity bucket with day-cluster bootstrap CIs;
- `atlas_manifest.json`: every day's root and the root over the day roots in date order (`common.merkle_root`).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as K  # noqa: E402

SUMS = ("n", "sum_p", "n_y", "sum_d", "sum_sq", "sum_ll", "qty_n", "qty_num", "qty_den", "b", "c")
CELL_KEYS = ["mode", "shadow_pop", "model_id", "population", "horizon_ns", "symbol", "session_date"]
STRATA_KEYS = [
    "mode",
    "shadow_pop",
    "model_id",
    "population",
    "stratum_kind",
    "stratum",
    "horizon_ns",
    "tier",
    "session_date",
]


def _sum_by(t: Any, keys: list[str]) -> Any:
    g = t.group_by(keys).aggregate([(f, "sum") for f in SUMS])
    return g.select([*keys, *[f"{f}_sum" for f in SUMS]]).rename_columns([*keys, *SUMS])


def reduce_day(plan: dict[str, Any], shards: Path, out: Path) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from aggregate import load_shards, sum_exactness

    ok, problems = load_shards(plan, shards, deep=True)
    tier = {u["symbol"]: u["tier"] for u in plan["universe"]}
    cells, strata = [], []
    for m in ok:
        t = pq.read_table(Path(m["_dir"]) / m["files"]["block_stats"]["file"])
        if not t.num_rows:
            continue
        t = t.append_column("tier", pa.array([tier.get(s, "?") for s in t.column("symbol").to_pylist()], pa.utf8()))
        is_all = pc.equal(t["stratum_kind"], "all")
        cells.append(_sum_by(t.filter(is_all), [*CELL_KEYS, "tier"]))
        strata.append(_sum_by(t.filter(pc.invert(is_all)), STRATA_KEYS))
    out.mkdir(parents=True, exist_ok=True)
    cells_t = pa.concat_tables(cells) if cells else pa.table({})
    strata_t = _sum_by(pa.concat_tables(strata), STRATA_KEYS) if strata else pa.table({})
    pq.write_table(cells_t, out / "cells_all.parquet", compression="zstd")
    pq.write_table(strata_t, out / "strata_by_tier.parquet", compression="zstd")
    leaves = [m["leaf_hash"] for m in ok]
    by_tier: dict[str, list] = defaultdict(list)
    aborts: dict[str, int] = defaultdict(int)
    flagged = []
    for m in ok:
        ex = m["leaf"]["exactness"]
        tiers = {tier.get(p["symbol"], "?") for p in m["leaf"]["inputs"]["partitions"]}
        for tr in tiers:
            by_tier[tr].append(ex)  # a unit spans tiers: its counts go to each (labelled per unit, see note)
        if ex and (
            any(ex["invariants"].values()) or ex["price_priority_violations"] or ex["parity"].get("trades_mismatches")
        ):
            flagged.append(
                {
                    "unit_id": m["unit_id"],
                    "invariants": {k: v for k, v in ex["invariants"].items() if v},
                    "price_priority_violations": ex["price_priority_violations"],
                    "trades_mismatches": ex["parity"].get("trades_mismatches"),
                    "symbols": m["leaf"]["inputs"]["partitions"]
                    and [p["symbol"] for p in m["leaf"]["inputs"]["partitions"]],
                }
            )
        for flags in m["leaf"]["zoo"].values():
            for model, n in flags["hbt_312_abort"].items():
                aborts[model] += n
    tel = [m["telemetry"] for m in ok]
    doc = {
        "schema": "qt.scale.day_manifest",
        "run_id": plan["run_id"],
        "date": plan["sources"][0]["date"] if plan["sources"] else None,
        "source": plan["sources"][0] if plan["sources"] else None,
        "merkle_root": K.merkle_root(leaves),
        "leaves": [
            {"unit_id": m["unit_id"], "leaf_hash": m["leaf_hash"], "proof": K.merkle_proof(leaves, i)}
            for i, m in enumerate(ok)
        ],
        "units_planned": len(plan["units"]),
        "units_valid": len(ok),
        "problems": problems,
        "flagged_units": flagged,
        "symbol_days": len(plan["universe"]),
        "symbols_by_tier": {t: sum(1 for u in plan["universe"] if u["tier"] == t) for t in sorted(set(tier.values()))},
        "exclusions": len(plan["exclusions"]),
        "exactness_overall": sum_exactness([m["leaf"]["exactness"] for m in ok]),
        "exactness_note": "per-tier sums count a unit holding several tiers in each of them; overall is exact",
        "exactness_by_unit_tiers": {t: sum_exactness(v) for t, v in sorted(by_tier.items())},
        "zoo_hbt_312_abort": dict(sorted(aborts.items())),
        "telemetry": {
            "records": sum(t["records"] for t in tel),
            "shadows": sum(t["shadows"] for t in tel),
            "cpu_s": round(sum(t["cpu_s"] for t in tel), 1),
            "wall_s": round(sum(t["wall_s"] for t in tel), 1),
            "max_unit_wall_s": max((t["wall_s"] for t in tel), default=None),
            "max_peak_rss_mb": max((t["peak_rss_mb"] for t in tel), default=None),
        },
    }
    (out / "day_manifest.json").write_text(json.dumps(doc, indent=1) + "\n")
    return {k: doc[k] for k in ("run_id", "date", "merkle_root", "units_valid", "units_planned", "symbol_days")}


# ---- combine -----------------------------------------------------------------------------------------------------


def _day_bootstrap(num: Any, den: Any, b: int, seed: int, tag: str) -> tuple[Any, Any, Any, Any]:
    """Per row of `num` / `den` (rows x days): (value, ci_lo, ci_hi, se) from a day-cluster bootstrap shared by all
    rows (`block_counts` over the days, stream `bootstrap_seed(seed, "scale_day:<tag>")`)."""
    import numpy as np

    from queuetruth.stats import block_counts, bootstrap_seed

    d = num.shape[1]
    w = block_counts(d, b, bootstrap_seed(seed, f"scale_day:{tag}")).astype(float)
    tot_n, tot_d = num.sum(axis=1), den.sum(axis=1)
    value = np.where(tot_d > 0, tot_n / np.where(tot_d > 0, tot_d, 1.0), np.nan)
    rn, rd = w @ num.T, w @ den.T  # (b, rows)
    with np.errstate(invalid="ignore", divide="ignore"):
        reps = np.where(rd > 0, rn / rd, np.nan)
    lo = np.nanquantile(reps, 0.025, axis=0)
    hi = np.nanquantile(reps, 0.975, axis=0)
    se = np.nanstd(reps, axis=0, ddof=1)
    one = (den > 0).sum(axis=1) < 2  # a single day: no between-day variation to resample
    lo[one] = hi[one] = se[one] = np.nan
    return value, lo, hi, se


def combine(day_dirs: list[Path], out: Path, bootstrap_b: int = 1000, seed: int = 0) -> dict[str, Any]:
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from queuetruth.eval import blocks as B

    days = [(json.loads((d / "day_manifest.json").read_text()), d) for d in day_dirs]
    days = sorted(days, key=lambda x: x[0]["date"][4:] + x[0]["date"][:4])  # MMDDYYYY -> chronological
    cells = pa.concat_tables([pq.read_table(d / "cells_all.parquet") for _, d in days], promote_options="default")
    never = pc.equal(cells["population"], "")
    cells = cells.filter(never)
    h_max = int(pc.max(cells["horizon_ns"]).as_py())
    out.mkdir(parents=True, exist_ok=True)

    # 1. leaderboard by tier: two-way dates x symbols
    board = []
    tiers = [*sorted(set(cells.column("tier").to_pylist())), "ALL"]
    for mode in sorted(set(cells.column("mode").to_pylist())):
        for pop in sorted(set(cells.column("shadow_pop").to_pylist())):
            base = cells.filter(pc.and_(pc.equal(cells["mode"], mode), pc.equal(cells["shadow_pop"], pop)))
            for tr in tiers:
                st = base if tr == "ALL" else base.filter(pc.equal(base["tier"], tr))
                rows: dict[tuple, dict[str, Any]] = {}
                for metric in ("exact_fill_rate", "model_fill_rate", "fill_rate_bias", "overfill_bias"):
                    for r in B.cluster_bootstrap(
                        st,
                        metric=metric,
                        clusters=("session_date", "symbol"),
                        group=("model_id", "horizon_ns"),
                        bootstrap_b=bootstrap_b,
                        seed=seed,
                    ):
                        if metric == "overfill_bias" and r["horizon_ns"] != h_max:
                            continue
                        row = rows.setdefault(
                            (r["model_id"], r["horizon_ns"]),
                            {
                                "mode": mode,
                                "shadow_pop": pop,
                                "tier": tr,
                                "model_id": r["model_id"],
                                "horizon_ns": r["horizon_ns"],
                                "n": r["n"],
                                "n_dates": r["n_session_date"],
                                "n_symbols": r["n_symbol"],
                            },
                        )
                        for f in ("value", "ci_lo", "ci_hi", "se"):
                            row[f"{metric}_{f}"] = r[f]
                        if metric == "fill_rate_bias":
                            row["fill_rate_bias_p_boot"] = r["p_boot"]
                lb = list(rows.values())
                adj = B.bh(
                    [
                        r.get("fill_rate_bias_p_boot") if r.get("fill_rate_bias_p_boot") is not None else np.nan
                        for r in lb
                    ]
                )
                for r, q in zip(lb, adj, strict=True):
                    r["fill_rate_bias_p_bh"] = None if np.isnan(q) else float(q)
                board += lb
    pq.write_table(pa.Table.from_pylist(board), out / "leaderboard_by_tier.parquet", compression="zstd")

    # 2. per symbol across days (vectorised day bootstrap per (mode, pop, model, horizon))
    date_list = sorted(set(cells.column("session_date").to_pylist()))
    di = {d: i for i, d in enumerate(date_list)}
    per = []
    keys = cells.select(["mode", "shadow_pop", "model_id", "horizon_ns"]).to_pylist()
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, k in enumerate(keys):
        groups[(k["mode"], k["shadow_pop"], k["model_id"], k["horizon_ns"])].append(i)
    sym_all = cells.column("symbol").to_pylist()
    day_all = cells.column("session_date").to_pylist()
    tier_all = cells.column("tier").to_pylist()
    sum_d = cells.column("sum_d").to_numpy()
    n_all = cells.column("n").to_numpy().astype(float)
    for (mode, pop, model, hz), idx in sorted(groups.items()):
        syms = sorted({sym_all[i] for i in idx})
        si = {s: j for j, s in enumerate(syms)}
        num = np.zeros((len(syms), len(date_list)))
        den = np.zeros((len(syms), len(date_list)))
        tiers_of: dict[str, list[str]] = defaultdict(list)
        for i in idx:
            num[si[sym_all[i]], di[day_all[i]]] += sum_d[i]
            den[si[sym_all[i]], di[day_all[i]]] += n_all[i]
            tiers_of[sym_all[i]].append(tier_all[i])
        value, lo, hi, se = _day_bootstrap(num, den, bootstrap_b, seed, f"{mode}:{pop}:{model}:{hz}")
        home = {s: max(set(v), key=v.count) for s, v in tiers_of.items()}
        post = np.full(len(syms), np.nan)
        for tr in set(home.values()):
            j = [si[s] for s in syms if home[s] == tr]
            post[j] = B.shrink(value[j], se[j])["posterior"]
        # p-values from the day-bootstrap SE (normal approximation; the replicates are not kept per symbol)
        from scipy.stats import norm

        z = np.where(se > 0, value / np.where(se > 0, se, 1.0), np.nan)
        p = 2 * norm.sf(np.abs(z))
        p_bh = B.bh(p)
        n_days = (den > 0).sum(axis=1)
        for s in syms:
            j = si[s]
            per.append(
                {
                    "mode": mode,
                    "shadow_pop": pop,
                    "model_id": model,
                    "horizon_ns": hz,
                    "symbol": s,
                    "tier": home[s],
                    "n": int(den[j].sum()),
                    "n_days": int(n_days[j]),
                    "bias": _f(value[j]),
                    "ci_lo": _f(lo[j]),
                    "ci_hi": _f(hi[j]),
                    "se": _f(se[j]),
                    "bias_shrunk": _f(post[j]),
                    "p_normal": _f(p[j]),
                    "p_bh": _f(p_bh[j]),
                }
            )
    pq.write_table(pa.Table.from_pylist(per), out / "per_symbol.parquet", compression="zstd")

    # 3. maps: tier x queue decile / liquidity bucket, day-cluster bootstrap
    st = pa.concat_tables([pq.read_table(d / "strata_by_tier.parquet") for _, d in days], promote_options="default")
    st = st.filter(pc.equal(st["population"], ""))
    mkeys = ["mode", "shadow_pop", "model_id", "stratum_kind", "stratum", "horizon_ns", "tier"]
    rows = st.select([*mkeys, "session_date", "sum_d", "n"]).to_pylist()
    cell_idx: dict[tuple, int] = {}
    for r in rows:
        cell_idx.setdefault(tuple(r[k] for k in mkeys), len(cell_idx))
    num = np.zeros((len(cell_idx), len(date_list)))
    den = np.zeros((len(cell_idx), len(date_list)))
    for r in rows:
        j = cell_idx[tuple(r[k] for k in mkeys)]
        num[j, di[r["session_date"]]] += r["sum_d"]
        den[j, di[r["session_date"]]] += r["n"]
    value, lo, hi, se = _day_bootstrap(num, den, bootstrap_b, seed, "maps")
    maps = [
        dict(zip(mkeys, k, strict=True))
        | {
            "n": int(den[j].sum()),
            "n_days": int((den[j] > 0).sum()),
            "bias": _f(value[j]),
            "ci_lo": _f(lo[j]),
            "ci_hi": _f(hi[j]),
            "se": _f(se[j]),
        }
        for k, j in cell_idx.items()
    ]
    pq.write_table(pa.Table.from_pylist(maps), out / "maps_by_tier.parquet", compression="zstd")

    roots = [d["merkle_root"] for d, _ in days]
    manifest = {
        "schema": "qt.scale.atlas_manifest",
        "schema_version": 1,
        "atlas_root": K.merkle_root(roots),
        "rule": "Merkle root over the day roots in date order; each day root covers that day's shard leaves",
        "days": [
            {
                "date": d["date"],
                "run_id": d["run_id"],
                "merkle_root": d["merkle_root"],
                "units_valid": d["units_valid"],
                "units_planned": d["units_planned"],
                "problems": len(d["problems"]),
                "flagged_units": len(d["flagged_units"]),
                "symbol_days": d["symbol_days"],
                "source_url": (d.get("source") or {}).get("url"),
                "source_sha256": (d.get("source") or {}).get("compressed_sha256"),
            }
            for d, _ in days
        ],
        "symbol_days": sum(d["symbol_days"] for d, _ in days),
        "distinct_symbols": len(set(sym_all)),
        "bootstrap_b": bootstrap_b,
        "seed": seed,
        "build": K.build_info(),
    }
    (out / "atlas_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return {
        "atlas_root": manifest["atlas_root"],
        "days": len(days),
        "symbol_days": manifest["symbol_days"],
        "distinct_symbols": manifest["distinct_symbols"],
    }


def _f(x: Any) -> float | None:
    import math

    return None if x is None or not math.isfinite(float(x)) else float(x)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reduce-day")
    r.add_argument("--plan", type=Path, required=True)
    r.add_argument("--shards", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("combine")
    c.add_argument("--day", type=Path, action="append", required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--bootstrap-b", type=int, default=1000)
    a = ap.parse_args(argv)
    if a.cmd == "reduce-day":
        print(json.dumps(reduce_day(json.loads(a.plan.read_text()), a.shards, a.out)))
    else:
        print(json.dumps(combine(a.day, a.out, a.bootstrap_b)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
