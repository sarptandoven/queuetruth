#!/usr/bin/env python3
"""P0 proof of the W1 pipeline: 36 single-symbol units per day aggregate to the published 36-symbol breadth run.

    uv run --frozen --no-sync python scripts/scale/prove_breadth.py                  # both public days
    uv run --frozen --no-sync python scripts/scale/prove_breadth.py --dates 12302019 --tiers mid

Steps (everything under `--out`, default `.realdata/scale/proof`; the raw days are never stored):
1. per day, `itch_split.py --date <d> --symbols <the 36 TIERS names> --buckets 8` (one stream of the public file;
   reused when a split with a valid manifest is already there);
2. `plan.py --quota-rule breadth --one-symbol-per-unit`: 72 units of one symbol-day each, with the breadth run's
   per-tier equal allocation as per-symbol `partition_quotas` (3,000 / 2,000 / 2,000 shadows per tier, remainder
   to the first locates);
3. `unit.py` for every unit (a process pool), then `aggregate.py` with the tiers as groups;
4. compares with `--breadth` (the published `.realdata/repro/breadth/breadth.json`, docs/RESULTS.md 4b) per day,
   tier, population and mode: every `fill_rate_bias` cell (model x stratum all / queue_decile / liquidity_bucket /
   instrument x horizon: value, CI and n), the exact fill rate counts, the shadows per symbol, the #312 abort counts
   per model, and the tier's replay checks (records, events, trades, the 8 invariants, executions checked,
   price-priority violations, rank-0 shares, trade parity).

Values must agree within `--tol` (default 1e-12, float summation order), counts exactly. Writes `proof.json` (every
comparison, the first mismatches, timings, the unit telemetry totals) and prints one line per (day, tier). Exit 0
when everything matches.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as K  # noqa: E402

DATES = {"12302019": "2019-12-30", "01302020": "2020-01-30"}


def _run_unit(args: tuple[dict, str, str, int]) -> tuple[str, float, str | None]:
    from unit import run_unit

    unit, split, out, threads = args
    t0 = time.perf_counter()
    try:
        run_unit(unit, Path(split), Path(out), threads)
        return unit["unit_id"], time.perf_counter() - t0, None
    except Exception as e:  # reported, the proof fails
        return unit["unit_id"], time.perf_counter() - t0, f"{type(e).__name__}: {e}"


def split_day(date: str, out: Path, symbols: list[str], buckets: int, min_free: float) -> Path:
    d = out / "split" / date
    mp = d / "split_manifest.json"
    if mp.exists() and json.loads(mp.read_text()).get("ok"):
        m = json.loads(mp.read_text())
        if all((d / b["file"]).exists() and K.sha256_file(d / b["file"]) == b["file_sha256"] for b in m["buckets"]):
            print(f"[split {date}] reusing {d}", file=sys.stderr)
            return d
    cmd = [
        sys.executable,
        "-I",
        str(HERE / "itch_split.py"),
        "--date",
        date,
        "--symbols",
        ",".join(symbols),
        "--buckets",
        str(buckets),
        "--out",
        str(out / "split"),
        "--min-free-gib",
        str(min_free),
    ]
    print(f"[split {date}] streaming the public day ...", file=sys.stderr, flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"itch_split {date}: exit {r.returncode}")
    return d


class Cmp:
    def __init__(self, tol: float):
        self.tol = tol
        self.n = 0
        self.bad: list[dict[str, Any]] = []
        self.max_abs = 0.0

    def num(self, where: str, want: Any, got: Any, exact: bool = False) -> None:
        self.n += 1
        if want is None or got is None:
            if want is not got:
                self.bad.append({"where": where, "published": want, "shards": got})
            return
        if exact:
            if want != got:
                self.bad.append({"where": where, "published": want, "shards": got})
            return
        d = abs(float(want) - float(got))
        self.max_abs = max(self.max_abs, d)
        if d > self.tol:
            self.bad.append({"where": where, "published": want, "shards": got, "abs_diff": d})


def compare(
    breadth: dict,
    by_group: list[dict],
    exact_t: Any,
    shadows_t: Any,
    shards: list[dict],
    plan: dict,
    tol: float,
    dates: list[str],
    tiers: list[str],
    pops: list[str],
    modes: list[str],
) -> dict[str, Any]:
    from aggregate import sum_exactness

    hz_lab = {h: lab for h, lab in K.HORIZONS.items()}
    days_iso = {DATES[d]: d for d in dates}
    # index the shard results
    cells: dict[tuple, dict] = {}
    for r in by_group:
        if r["metric"] != "fill_rate_bias" or r["horizon_ns"] not in hz_lab:
            continue
        cells[
            (
                r["session_date"],
                r["group"],
                r["shadow_pop"],
                r["mode"],
                r["model_id"],
                r["stratum_kind"],
                r["stratum"],
                hz_lab[r["horizon_ns"]],
            )
        ] = r
    ex_rows = exact_t.to_pylist()
    sh_rows = shadows_t.to_pylist()
    loc = {(u["date"], u["symbol"]): u["locate"] for u in plan["universe"]}
    report: dict[str, Any] = {"by_day_tier": {}}
    total = Cmp(tol)
    import datetime as dt

    for day_iso, dd in breadth["days"].items():
        if day_iso not in days_iso:
            continue
        date = days_iso[day_iso]
        sd = (dt.date.fromisoformat(day_iso) - dt.date(1970, 1, 1)).days
        for tier in tiers:
            td = dd["tiers"].get(tier)
            if td is None:
                continue
            c = Cmp(tol)
            # replay exactness of the tier = sum of its symbols' units
            ms = [
                m
                for m in shards
                if m["unit_id"].startswith(date)
                and m["leaf"]["inputs"]["partitions"]
                and m["leaf"]["inputs"]["partitions"][0]["symbol"] in td["symbols"]
            ]
            ex = sum_exactness([m["leaf"]["exactness"] for m in ms])
            rp = td["replay"]
            for k in ("records", "events", "trades"):
                c.num(f"replay.stats.{k}", rp["stats"][k], ex[k], exact=True)
            for k, v in rp["invariants"].items():
                c.num(f"replay.invariants.{k}", v, ex["invariants"][k], exact=True)
            c.num("replay.executions_checked", rp["executions_checked"], ex["executions_checked"], exact=True)
            c.num(
                "replay.price_priority_violations",
                rp["price_priority_violations"],
                ex["price_priority_violations"],
                exact=True,
            )
            c.num("replay.rank0_share", rp["rank0_share"], ex["rank0_share"])
            c.num(
                "replay.rank0_share_sweep_adjusted", rp["rank0_share_sweep_adjusted"], ex["rank0_share_sweep_adjusted"]
            )
            for k in ("trades_compared", "trades_mismatches", "bbo_compared", "bbo_mismatches"):
                if k in rp["parity"]:
                    c.num(f"replay.parity.{k}", rp["parity"][k], ex["parity"][k], exact=True)
            c.num(
                "ingest.records",
                td["ingest"]["records"],
                sum(p["rows"] for m in ms for p in m["leaf"]["inputs"]["partitions"]),
                exact=True,
            )
            for pop in pops:
                pr = td["populations"].get(pop) or {}
                if "error" in pr:
                    c.bad.append({"where": f"{pop}: published run has an error"})
                    continue
                for inst, n in (pr.get("sample", {}).get("by_instrument") or {}).items():
                    sym = next(s for s in td["symbols"] if loc.get((date, s)) == int(inst))
                    got = sum(
                        x["n_shadows"]
                        for x in sh_rows
                        if x["session_date"] == sd
                        and x["shadow_pop"] == pop
                        and x["mode"] == modes[0]
                        and x["symbol"] == sym
                    )
                    c.num(f"{pop}.sample.{sym}", n, got, exact=True)
                for mode in modes:
                    board = pr.get(mode)
                    if not board:
                        continue
                    seen = 0
                    for model, kinds in board["models"].items():
                        for kind, strata in kinds.items():
                            for stratum, hs in strata.items():
                                for lab, cell in hs.items():
                                    seen += 1
                                    r = cells.get((sd, tier, pop, mode, model, kind, stratum, lab))
                                    w = f"{pop}/{mode}/{model}/{kind}={stratum}/{lab}"
                                    if r is None:
                                        c.bad.append({"where": w, "problem": "cell missing in the shard aggregate"})
                                        continue
                                    c.num(w + ".bias", cell["bias"], r["value"])
                                    c.num(w + ".ci_lo", cell["ci_lo"], r["ci_lo"])
                                    c.num(w + ".ci_hi", cell["ci_hi"], r["ci_hi"])
                                    c.num(w + ".n", cell["n"], r["n"], exact=True)
                    mine = sum(1 for k in cells if k[:4] == (sd, tier, pop, mode))
                    c.num(f"{pop}/{mode}: number of fill_rate_bias cells", seen, mine, exact=True)
                    # exact fill rate (breadth: first model in sorted order, not censored, truth defined)
                    tier_syms = set(td["symbols"])
                    rows = [
                        x
                        for x in ex_rows
                        if x["session_date"] == sd
                        and x["mode"] == mode
                        and x["shadow_pop"] == pop
                        and x["symbol"] in tier_syms
                    ]
                    if rows:
                        m0 = min(x["model_id"] for x in rows)
                        for h, lab in hz_lab.items():
                            nd = sum(x["n_defined"] for x in rows if x["model_id"] == m0 and x["horizon_ns"] == h)
                            nf = sum(x["n_filled"] for x in rows if x["model_id"] == m0 and x["horizon_ns"] == h)
                            want = board["exact_fill_rate"].get(lab) or {}
                            c.num(
                                f"{pop}/{mode}/exact_fill_rate/{lab}.n_defined", want.get("n_defined"), nd, exact=True
                            )
                            c.num(f"{pop}/{mode}/exact_fill_rate/{lab}.n_filled", want.get("n_filled"), nf, exact=True)
                    aborts: dict[str, int] = defaultdict(int)
                    for m in ms:
                        for model, n in m["leaf"]["zoo"].get(f"{pop}/{mode}", {}).get("hbt_312_abort", {}).items():
                            aborts[model] += n
                    c.num(f"{pop}/{mode}/hbt_312_abort", board.get("hbt_312_abort", {}), dict(aborts), exact=True)
            report["by_day_tier"][f"{day_iso}/{tier}"] = {
                "compared": c.n,
                "mismatches": len(c.bad),
                "max_abs_diff": c.max_abs,
                "first": c.bad[:10],
            }
            total.n += c.n
            total.bad += c.bad
            total.max_abs = max(total.max_abs, c.max_abs)
            print(f"  {day_iso} {tier:<10} {c.n:>7,} compared, {len(c.bad)} mismatches, max |diff| {c.max_abs:.1e}")
    report["total"] = {
        "compared": total.n,
        "mismatches": len(total.bad),
        "max_abs_diff": total.max_abs,
        "tolerance": tol,
        "first": total.bad[:20],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    import plan as P

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=K.REPO / ".realdata" / "scale" / "proof")
    ap.add_argument("--breadth", type=Path, default=K.REPO / ".realdata" / "repro" / "breadth" / "breadth.json")
    ap.add_argument("--dates", default=",".join(DATES))
    ap.add_argument("--tiers", default=",".join(K.TIERS))
    ap.add_argument("--populations", default=",".join(K.POPULATIONS))
    ap.add_argument("--modes", default="faithful,corrected")
    ap.add_argument("--buckets", type=int, default=8)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--tol", type=float, default=1e-12)
    ap.add_argument("--min-free-gib", type=float, default=2.5)
    ap.add_argument("--compare-only", action="store_true", help="aggregate and compare existing --plan / --shards")
    ap.add_argument("--plan", type=Path, help="with --compare-only: the gate plan (e.g. downloaded from Modal)")
    ap.add_argument("--shards", type=Path, help="with --compare-only: its shard directories")
    a = ap.parse_args(argv)
    t_all = time.perf_counter()
    dates = [d for d in a.dates.split(",") if d]
    tiers = [t for t in a.tiers.split(",") if t]
    pops = [p for p in a.populations.split(",") if p]
    modes = [m for m in a.modes.split(",") if m]
    breadth = json.loads(a.breadth.read_text())
    symbols = [s for t in K.TIERS.values() for s in t]
    a.out.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    errors: list[dict[str, str]] = []
    if a.compare_only:
        if not (a.plan and a.shards):
            ap.error("--compare-only needs --plan and --shards")
        plan_path, shards_dir = a.plan, a.shards
        plan = json.loads(plan_path.read_text())
        return _finish(a, breadth, plan, plan_path, shards_dir, errors, timings, t_all, dates, tiers, pops, modes)
    t0 = time.perf_counter()
    splits = [split_day(d, a.out, symbols, a.buckets, a.min_free_gib) for d in dates]
    timings["split_s"] = round(time.perf_counter() - t0, 1)
    plan_path = a.out / "plan.json"
    excl = ",".join(s for t, names in K.TIERS.items() if t not in tiers for s in names)
    P.main(
        [
            *[x for s in splits for x in ("--split", str(s))],
            "--out",
            str(plan_path),
            "--quota-rule",
            "breadth",
            "--one-symbol-per-unit",
            "--populations",
            ",".join(pops),
            "--modes",
            ",".join(modes),
            *(["--exclude", excl] if excl else []),
        ]
    )
    plan = json.loads(plan_path.read_text())
    split_of = {sp.name: sp for sp in splits}
    shards_dir = a.out / "shards"
    todo = []
    for u in plan["units"]:
        sd = shards_dir / u["unit_id"]
        mp = sd / "shard_manifest.json"
        if mp.exists() and json.loads(mp.read_text())["leaf"]["unit_spec_sha256"] == K.sha256_hex(K.canonical_json(u)):
            continue
        todo.append((u, str(split_of[u["date"]]), str(sd), a.threads))
    print(
        f"[units] {len(todo)} to run of {len(plan['units'])} ({a.workers} workers x {a.threads} threads)",
        file=sys.stderr,
        flush=True,
    )
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(_run_unit, t) for t in todo]
        for i, f in enumerate(as_completed(futs), 1):
            uid, wall, err = f.result()
            print(
                f"  [{i}/{len(todo)}] {uid} {wall:.1f} s{' ERROR ' + err if err else ''}", file=sys.stderr, flush=True
            )
            if err:
                errors.append({"unit_id": uid, "error": err})
    timings["units_wall_s"] = round(time.perf_counter() - t0, 1)
    return _finish(a, breadth, plan, plan_path, shards_dir, errors, timings, t_all, dates, tiers, pops, modes)


def _finish(
    a: argparse.Namespace,
    breadth: dict,
    plan: dict,
    plan_path: Path,
    shards_dir: Path,
    errors: list,
    timings: dict,
    t_all: float,
    dates: list[str],
    tiers: list[str],
    pops: list[str],
    modes: list[str],
) -> int:
    import aggregate as A
    import pyarrow.parquet as pq

    groups = {s: t for t, names in K.TIERS.items() for s in names}
    (a.out / "tiers.json").write_text(json.dumps(groups, indent=1) + "\n")
    t0 = time.perf_counter()
    A.main(
        [
            "--plan",
            str(plan_path),
            "--shards",
            str(shards_dir),
            "--out",
            str(a.out / "atlas"),
            "--groups",
            str(a.out / "tiers.json"),
        ]
    )
    timings["aggregate_s"] = round(time.perf_counter() - t0, 1)
    shards, problems = A.load_shards(plan, shards_dir, deep=False)
    by_group = pq.read_table(a.out / "atlas" / "zoo_errors_by_group.parquet").to_pylist()
    exact_t = A.concat_outputs(shards, "exact", groups)
    shadows_t = A.concat_outputs(shards, "shadows", groups)
    print("comparison with the published breadth run:")
    rep = compare(breadth, by_group, exact_t, shadows_t, shards, plan, a.tol, dates, tiers, pops, modes)
    tel = [m["telemetry"] for m in shards]
    timings["total_s"] = round(time.perf_counter() - t_all, 1)
    atlas = json.loads((a.out / "atlas" / "atlas_manifest.json").read_text())
    proof = {
        "schema": "qt.scale.proof",
        "generator": "scripts/scale/prove_breadth.py",
        "claim": "the 36-symbol breadth result (docs/RESULTS.md 4b) is reproduced from one work unit per symbol-day "
        "aggregated from summable block statistics",
        "published": {
            "path": str(a.breadth),
            "sha256": K.sha256_file(a.breadth),
            "built_by": [r.get("build", {}).get("core_extension") for r in breadth.get("runs", [])],
        },
        "run_id": plan["run_id"],
        "merkle_root": atlas["merkle_root"],
        "units": len(plan["units"]),
        "unit_errors": errors,
        "shard_problems": problems,
        "comparison": rep,
        "telemetry_totals": {
            "records": sum(t["records"] for t in tel),
            "shadows": sum(t["shadows"] for t in tel),
            "cpu_s": round(sum(t["cpu_s"] for t in tel), 1),
            "wall_s": round(sum(t["wall_s"] for t in tel), 1),
            "max_peak_rss_mb": max((t["peak_rss_mb"] for t in tel), default=None),
            "by_step_cpu_s": {
                k: round(sum(t["steps"].get(k, {}).get("cpu_s", 0) for t in tel), 1)
                for k in sorted({k for t in tel for k in t["steps"]})
            },
        },
        "timings": timings,
        "build": K.build_info(),
        "ok": not errors and not problems and rep["total"]["mismatches"] == 0,
    }
    (a.out / "proof.json").write_text(json.dumps(proof, indent=1, default=str) + "\n")
    t = rep["total"]
    print(
        f"proof: {'OK' if proof['ok'] else 'FAILED'}: {t['compared']:,} values compared, {t['mismatches']} "
        f"mismatches, max |diff| {t['max_abs_diff']:.1e}; {len(errors)} unit errors -> {a.out / 'proof.json'}"
    )
    return 0 if proof["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
