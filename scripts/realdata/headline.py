"""Headline real-data experiment ("how optimistic is each L2 queue model"): on each converted Nasdaq day, 3,000
sampled never-cancel shadows (planned life 300 s >= the 60 s horizon, audit A2) run through all 17 zoo models in
faithful and corrected mode; the zoo's own exact arm is the truth.

Writes `<out>/headline.json` with aggregates only: per day x mode x model, fill-rate bias (model mean p_fill minus
exact fill rate, with the zoo's day-block bootstrap 95% CI) at 1 s, 10 s and 60 s overall, by queue-ahead decile and
by liquidity bucket, plus the exact fill rate per horizon over every sampled shadow and over the leaderboard's
common population (0.1.1: shadows on which any faithful hftbacktest model hit its #312 abort are left out of every
model), with the abort counts. Zoo output directories are deleted after reading unless
`--keep`. Inputs are the datasets written by `validate_et.py` (`<ds-root>/nasdaq-<date>`)."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq

DAYS = ("2019-12-30", "2020-01-30")
HORIZONS = {1_000_000_000: "1s", 10_000_000_000: "10s", 60_000_000_000: "60s"}
STRATA = ("all", "queue_decile", "liquidity_bucket", "instrument")


def run_day(ds: Path, out: Path, date: str, n: int, seed: int, threads: int, keep: bool) -> dict[str, Any]:
    import queuetruth as qt

    out.mkdir(parents=True, exist_ok=True)
    sh = out / f"shadows-{date}.parquet"
    t0 = time.perf_counter()
    samp = qt.sample_shadows({"dataset": {"path": str(ds)}, "out": str(sh), "n": n, "seed": seed})
    res: dict[str, Any] = {"sample": {"n": samp.n if hasattr(samp, "n") else n, "seed": seed}, "modes": {}}
    for mode in ("faithful", "corrected"):
        zout = out / f"zoo-{date}-{mode}"
        t1 = time.perf_counter()
        summ = qt.run_zoo(
            {
                "dataset": {"path": str(ds)},
                "shadows": {"path": str(sh)},
                "out": str(zout),
                "models": ["all"],
                "mode": mode,
                "threads": threads,
                "seed": seed,
            }
        )
        wall = round(time.perf_counter() - t1, 2)
        err = pq.read_table(zout / "zoo_errors.parquet").to_pylist()
        pred = pq.read_table(
            zout / "zoo_predictions.parquet",
            columns=["model_id", "shadow_id", "horizon_ns", "truth_filled", "censored"],
        )
        first = sorted(set(pred.column("model_id").to_pylist()))[0]
        one = pred.filter(pc.equal(pred["model_id"], first))
        truth: dict[str, dict[str, Any]] = {}
        for h, lab in HORIZONS.items():
            rows = one.filter(pc.equal(one["horizon_ns"], h)).to_pylist()
            defined = [r for r in rows if not r["censored"] and r["truth_filled"] is not None]
            k = sum(bool(r["truth_filled"]) for r in defined)
            truth[lab] = {"n_defined": len(defined), "n_filled": k, "rate": (k / len(defined)) if defined else None}
        models: dict[str, Any] = defaultdict(lambda: defaultdict(dict))
        for r in err:
            if r["metric"] != "fill_rate_bias" or r["horizon_ns"] not in HORIZONS or r["stratum_kind"] not in STRATA:
                continue
            cell = {"bias": r["value"], "ci_lo": r["ci_lo"], "ci_hi": r["ci_hi"], "n": r["n"]}
            models[r["model_id"]][r["stratum_kind"]].setdefault(r["stratum"], {})[HORIZONS[r["horizon_ns"]]] = cell
        board = list(getattr(summ, "leaderboard", []) or [])
        res["modes"][mode] = {
            "wall_s": wall,
            "n_models": len(models),
            "exact_fill_rate": truth,
            # 0.1.1: every model is scored on one common population (shadows on which any faithful hbt model hit
            # #312 are left out of every model); the exact fill rate on that population is the leaderboard's
            "population": {
                "n_shadows": summ.n_shadows,
                "n_excluded_hbt_312": getattr(summ, "n_excluded_hbt_312", None),
                "n_leaderboard": getattr(summ, "n_leaderboard", None),
                "hbt_312_aborts": dict(getattr(summ, "hbt_312_aborts", {}) or {}),
                "exact_fill_rate": {
                    lab: board[0].exact_fill_rate.get(str(h)) if board else None for h, lab in HORIZONS.items()
                },
            },
            "models": json.loads(json.dumps(models)),
            "zoo_warnings": list(getattr(summ, "warnings", []) or []),
        }
        if not keep:
            shutil.rmtree(zout)
    res["wall_s"] = round(time.perf_counter() - t0, 1)
    if not keep:
        sh.unlink(missing_ok=True)
    return res


def ranking(day: dict[str, Any], mode: str, h: str) -> list[dict[str, Any]]:
    """Models sorted by absolute overall fill-rate bias at horizon `h`."""
    rows = []
    for mid, strata in day["modes"][mode]["models"].items():
        c = strata.get("all", {}).get("all", {}).get(h)
        if c is not None and c["bias"] is not None:
            rows.append({"model_id": mid, **c})
    return sorted(rows, key=lambda r: -abs(r["bias"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ds-root", type=Path, required=True, help="directory holding nasdaq-<date> datasets")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    doc: dict[str, Any] = {
        "schema": "qtdev.headline",
        "population": "sampled never-cancel shadows (ShadowSampleSpec defaults: touch-relative, lifetime 300 s)",
        "truth": "zoo exact arm (fill_rule exact)",
        "bias_definition": "mean model p_fill - exact fill rate at the horizon, A2-defined rows, day-block bootstrap CI",  # noqa: E501
        "days": {},
    }
    for date in DAYS:
        ds = a.ds_root / f"nasdaq-{date}"
        if not (ds / "manifest.json").exists():
            print(f"missing dataset {ds}", file=sys.stderr)
            return 2
        print(f"[headline] {date} ...", file=sys.stderr, flush=True)
        day = run_day(ds, a.out, date, a.n, a.seed, a.threads, a.keep)
        day["rank_60s"] = {m: ranking(day, m, "60s") for m in ("faithful", "corrected")}
        doc["days"][date] = day
        (a.out / "headline.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(json.dumps({d: {m: v["rank_60s"][m][:3] for m in v["rank_60s"]} for d, v in doc["days"].items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
