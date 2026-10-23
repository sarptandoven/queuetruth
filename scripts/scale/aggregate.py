#!/usr/bin/env python3
"""Aggregate a W1 run: verify the shards, write the Merkle-rooted run manifest and the leaderboards.

    uv run --frozen --no-sync python scripts/scale/aggregate.py --plan plan.json --shards SHARDS_DIR --out ATLAS_DIR
    uv run --frozen --no-sync python scripts/scale/aggregate.py ... --groups tiers.json   # {symbol: group} per run

Reads `<shards>/<unit_id>/` for every unit of the plan. Per shard: the leaf hash is recomputed from the leaf, the
unit spec hash must equal the plan's unit, and every output file's content digest must equal the leaf's (so a
shard cannot be swapped or edited without changing the root). Missing or failed shards are listed, never dropped
silently. Outputs in `--out`:

- `atlas_manifest.json` (`qt.scale.atlas_manifest`): run id, plan sha256, the Merkle root over the leaves in unit-id
  order (`common.py`), every leaf with its Merkle proof, the universe size, exclusions, missing / invalid shards,
  build info of the aggregator. `verify_unit.py` re-runs one unit and checks its leaf against this root.
- `zoo_errors_by_group.parquet`: `evaluate_blocks` per (date, mode, shadow population, group): the `qt.zoo_errors`
  rows the zoo would have written for that group's symbols run together (within-day 30-minute block bootstrap).
- `leaderboard.parquet`: per (mode, shadow population, group, model, horizon), stratum `all`, never-cancel
  population: exact fill rate, model fill rate and fill-rate bias with 95% CIs from the two-way (dates x symbols)
  pigeonhole cluster bootstrap, BH-adjusted p-values of the bias across models and horizons per (mode, population,
  group).
- `per_symbol.parquet`: per symbol, the same metrics with a day-cluster bootstrap CI (needs >= 2 dates; one date
  gives the within-day block CI instead), the empirical-Bayes posterior bias shrunk toward the group mean
  (DerSimonian-Laird), BH-adjusted p-values across symbol x model cells per (mode, population, horizon).
- `exactness.json`: summed replay checks per (date, group) and overall (records, invariants, executions checked,
  price-priority violations, rank-0 shares from the FIFO rank histograms, parity) and the zoo #312 abort counts.
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

ATLAS_SCHEMA = "qt.scale.atlas_manifest"


def load_shards(plan: dict[str, Any], shards: Path, deep: bool = True) -> tuple[list[dict], list[dict]]:
    """(valid shard manifests in unit-id order, problems)."""
    import pyarrow.parquet as pq
    from unit import STAT_KEYS

    keys = {
        "block_stats": STAT_KEYS,
        "shadows": ["mode", "shadow_pop", "symbol", "session_date", "block"],
        "exact": ["mode", "shadow_pop", "model_id", "horizon_ns", "symbol", "session_date"],
        "predictions_sample": ["mode", "shadow_pop", "model_id", "shadow_id", "horizon_ns"],
    }
    ok, problems = [], []
    for u in sorted(plan["units"], key=lambda x: x["unit_id"]):
        d = shards / u["unit_id"]
        mp = d / "shard_manifest.json"
        if not mp.exists():
            problems.append({"unit_id": u["unit_id"], "problem": "missing"})
            continue
        m = json.loads(mp.read_text())
        bad = []
        if K.leaf_hash(m["leaf"]) != m["leaf_hash"]:
            bad.append("leaf hash does not match the leaf")
        if m["leaf"]["unit_spec_sha256"] != K.sha256_hex(K.canonical_json(u)):
            bad.append("unit spec differs from the plan")
        if deep:
            for name, f in m["files"].items():
                t = pq.read_table(d / f["file"])
                if K.table_digest(t, keys[name]) != m["leaf"]["outputs"][name]["digest"]:
                    bad.append(f"{name}: content digest differs from the leaf")
        if bad:
            problems.append({"unit_id": u["unit_id"], "problem": "; ".join(bad)})
        else:
            m["_dir"] = str(d)
            ok.append(m)
    return ok, problems


def sum_exactness(items: list[dict[str, Any]]) -> dict[str, Any]:
    tot: dict[str, Any] = defaultdict(int)
    inv: dict[str, int] = defaultdict(int)
    par: dict[str, int] = defaultdict(int)
    hist = [0] * 6
    hist_sw = [0] * 6
    for e in items:
        if not e:
            continue
        for k, v in e["stats"].items():
            tot[k] += v
        for k, v in e["invariants"].items():
            inv[k] += v
        for k, v in e["parity"].items():
            par[k] += v or 0
        for k in ("executions_checked", "executions_unknown_order", "price_priority_violations", "sweeps"):
            tot[k] += e.get(k) or 0
        hist = [a + b for a, b in zip(hist, e.get("fifo_rank_histogram") or [0] * 6, strict=True)]
        hist_sw = [a + b for a, b in zip(hist_sw, e.get("fifo_rank_histogram_sweep_adjusted") or [0] * 6, strict=True)]
    out = dict(tot)
    out["invariants"] = dict(inv)
    out["invariants_all_zero"] = all(v == 0 for v in inv.values())
    out["parity"] = dict(par)
    out["fifo_rank_histogram"] = hist
    out["fifo_rank_histogram_sweep_adjusted"] = hist_sw
    out["rank0_share"] = hist[0] / sum(hist) if sum(hist) else None
    out["rank0_share_sweep_adjusted"] = hist_sw[0] / sum(hist_sw) if sum(hist_sw) else None
    return out


def concat_outputs(shards: list[dict], name: str, groups: dict[str, str] | None) -> Any:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from queuetruth.eval import blocks as B

    ts = []
    for m in shards:
        t = pq.read_table(Path(m["_dir"]) / m["files"][name]["file"])
        if t.num_rows and "symbol" in t.column_names:
            syms = t.column("symbol").to_pylist()
            t = t.append_column("group", pa.array([(groups or {}).get(s, "all") for s in syms], pa.utf8()))
        ts.append(t)
    return B.concat(ts)


def _filter(t: Any, **eq: Any) -> Any:
    import pyarrow as pa
    import pyarrow.compute as pc

    mask = None
    for k, v in eq.items():
        m = pc.equal(t[k], pa.scalar(v, t.schema.field(k).type))
        mask = m if mask is None else pc.and_(mask, m)
    return t.filter(mask) if mask is not None else t


def _distinct(t: Any, cols: list[str]) -> list[tuple]:
    return sorted({tuple(r[c] for c in cols) for r in t.select(cols).to_pylist()})


def leaderboards(stats: Any, shadows: Any, h_max: int, b: int, seed: int) -> dict[str, list[dict[str, Any]]]:
    import numpy as np

    from queuetruth.eval import blocks as B

    out: dict[str, list[dict[str, Any]]] = {"by_group": [], "leaderboard": [], "per_symbol": []}
    for date, mode, pop, grp in _distinct(stats, ["session_date", "mode", "shadow_pop", "group"]):
        st = _filter(stats, session_date=date, mode=mode, shadow_pop=pop, group=grp)
        sh = _filter(shadows, session_date=date, mode=mode, shadow_pop=pop, group=grp)
        for r in B.evaluate_blocks(st, sh, h_max_ns=h_max, bootstrap_b=b, seed=seed):
            out["by_group"].append({"session_date": date, "mode": mode, "shadow_pop": pop, "group": grp, **r})
    allpop = _filter(stats, stratum_kind="all", population="")
    for mode, pop, grp in _distinct(allpop, ["mode", "shadow_pop", "group"]):
        st = _filter(allpop, mode=mode, shadow_pop=pop, group=grp)
        rows: dict[tuple, dict[str, Any]] = {}
        for metric in ("exact_fill_rate", "model_fill_rate", "fill_rate_bias"):
            for r in B.cluster_bootstrap(
                st,
                metric=metric,
                clusters=("session_date", "symbol"),
                group=("model_id", "horizon_ns"),
                bootstrap_b=b,
                seed=seed,
            ):
                row = rows.setdefault(
                    (r["model_id"], r["horizon_ns"]),
                    {
                        "mode": mode,
                        "shadow_pop": pop,
                        "group": grp,
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
        adj = B.bh([r["fill_rate_bias_p_boot"] if r["fill_rate_bias_p_boot"] is not None else np.nan for r in lb])
        for r, q in zip(lb, adj, strict=True):
            r["fill_rate_bias_p_bh"] = None if np.isnan(q) else float(q)
        out["leaderboard"] += lb
        # per symbol: day clusters (or the within-day blocks of a one-date run)
        per = B.cluster_bootstrap(
            st,
            metric="fill_rate_bias",
            clusters=("session_date",),
            group=("symbol", "model_id", "horizon_ns"),
            bootstrap_b=b,
            seed=seed,
        )
        if per and max(r["n_session_date"] for r in per) < 2:
            per = B.cluster_bootstrap(
                st,
                metric="fill_rate_bias",
                clusters=("block",),
                group=("symbol", "model_id", "horizon_ns"),
                bootstrap_b=b,
                seed=seed,
            )
        by_cell: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for r in per:
            by_cell[(r["model_id"], r["horizon_ns"])].append(r)
        for (model_id, hz), rs in by_cell.items():
            sh_ = B.shrink(
                [r["value"] if r["value"] is not None else np.nan for r in rs],
                [r["se"] if r["se"] is not None else np.nan for r in rs],
            )
            for r, post in zip(rs, sh_["posterior"], strict=True):
                out["per_symbol"].append(
                    {
                        "mode": mode,
                        "shadow_pop": pop,
                        "group": grp,
                        "symbol": r["symbol"],
                        "model_id": model_id,
                        "horizon_ns": hz,
                        "n": r["n"],
                        "bias": r["value"],
                        "ci_lo": r["ci_lo"],
                        "ci_hi": r["ci_hi"],
                        "se": r["se"],
                        "p_boot": r["p_boot"],
                        "bias_shrunk": None if np.isnan(post) else float(post),
                        "group_mu": sh_["mu"],
                        "group_tau2": sh_["tau2"],
                    }
                )
    # BH across symbol x model cells per (mode, population, horizon)
    fam: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(out["per_symbol"]):
        fam[(r["mode"], r["shadow_pop"], r["horizon_ns"])].append(i)
    for idx in fam.values():
        ps = [out["per_symbol"][i]["p_boot"] for i in idx]
        adj = B.bh([p if p is not None else np.nan for p in ps])
        for i, q in zip(idx, adj, strict=True):
            out["per_symbol"][i]["p_bh"] = None if np.isnan(q) else float(q)
    return out


def main(argv: list[str] | None = None) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--shards", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--groups", type=Path, help="JSON {symbol: group}; default one group 'all'")
    ap.add_argument("--bootstrap-b", type=int, default=1000)
    ap.add_argument("--no-deep-verify", action="store_true", help="skip re-hashing every output's content")
    a = ap.parse_args(argv)
    plan = json.loads(a.plan.read_text())
    groups = json.loads(a.groups.read_text()) if a.groups else None
    shards, problems = load_shards(plan, a.shards, deep=not a.no_deep_verify)
    leaves = [m["leaf_hash"] for m in shards]
    root = K.merkle_root(leaves)
    a.out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": ATLAS_SCHEMA,
        "schema_version": 1,
        "run_id": plan["run_id"],
        "plan_sha256": K.sha256_file(a.plan),
        "merkle_root": root,
        "leaves": [
            {"unit_id": m["unit_id"], "leaf_hash": m["leaf_hash"], "proof": K.merkle_proof(leaves, i)}
            for i, m in enumerate(shards)
        ],
        "units_planned": len(plan["units"]),
        "units_valid": len(shards),
        "problems": problems,
        "universe_symbol_days": len(plan["universe"]),
        "exclusions": plan["exclusions"],
        "build": K.build_info(),
    }
    (a.out / "atlas_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    stats = concat_outputs(shards, "block_stats", groups)
    shadows = concat_outputs(shards, "shadows", groups)
    if stats.num_rows:
        res = leaderboards(stats, shadows, plan["run"]["h_max_ns"], a.bootstrap_b, plan["run"]["seed"])
    else:  # no valid shard: the manifest lists the problems; empty tables, never a partial result
        res = {"by_group": [], "leaderboard": [], "per_symbol": []}
    names = {"by_group": "zoo_errors_by_group", "leaderboard": "leaderboard", "per_symbol": "per_symbol"}
    for k, rows in res.items():
        pq.write_table(pa.Table.from_pylist(rows), a.out / f"{names[k]}.parquet", compression="zstd")
    ex: dict[str, Any] = {"by_date_group": {}, "zoo_hbt_312_abort": defaultdict(int)}
    per: dict[tuple, list] = defaultdict(list)
    sym_group = groups or {}
    for m in shards:
        u_date = m["unit_id"].split("-")[0]
        g = {sym_group.get(p["symbol"], "all") for p in m["leaf"]["inputs"]["partitions"]} or {"all"}
        per[(u_date, "+".join(sorted(g)))].append(m["leaf"]["exactness"])
        for flags in m["leaf"]["zoo"].values():
            for model, n in flags["hbt_312_abort"].items():
                ex["zoo_hbt_312_abort"][model] += n
    for (d, g), items in sorted(per.items()):
        ex["by_date_group"][f"{d}/{g}"] = sum_exactness(items)
    ex["overall"] = sum_exactness([m["leaf"]["exactness"] for m in shards])
    (a.out / "exactness.json").write_text(json.dumps(ex, indent=1, default=dict) + "\n")
    print(
        f"run {plan['run_id']}: {len(shards)}/{len(plan['units'])} shards valid, root {root}; "
        f"{len(problems)} problem(s) -> {a.out}"
    )
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
