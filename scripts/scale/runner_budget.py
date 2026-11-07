#!/usr/bin/env python3
"""Measure this machine on a synthetic ITCH day and size the symbol sample of a Nasdaq day that must fit on it.

    uv run --frozen --no-sync python scripts/scale/runner_budget.py --workers 4 --unit-hours 3.5

A full Nasdaq TotalView-ITCH sample day predicts about 8 to 12 core-hours of units (`plan.py`'s cost model), more
than one GitHub-hosted runner (4 cores, 16 GB, a 6 h job) can run next to the stream and split of the 5.6 GB file.
`.github/workflows/atlas.yml` therefore runs such a day with `local_day.py --sample-budget-core-h B`: in every
liquidity tier, the same number of symbols, chosen by a hash, up to `B` predicted core-hours (`plan.py`).

This script measures what `B` and `--unit-mem-gib` should be on the machine it runs on. It writes two synthetic ITCH
days (`python/tests/scale/scale_itch.py`, no market data): a wide one (many small symbols, so shadows dominate the
cost, as on a real day) and a deep one (a few symbols with many messages, as the most traded names). It splits and
plans them as `local_day.py` does (`scale` quotas, all populations and both modes, P&L on), runs every unit in its
own process, `--workers` at a time with one thread each, and reports:

- `speed`: measured unit CPU seconds per predicted core-second of the cost model on the wide day (the prediction
  includes its x1.5 safety, so a machine as fast as the M1 Pro it was fitted on reads about 0.67);
- `utilisation`: unit CPU seconds over (wall seconds x workers) while the wide day's units ran;
- `rss`: a least-squares fit of each unit's peak RSS on its records and shadows over both days, and the largest
  unit seen, to size `--unit-mem-gib` for `--unit-core-s`;
- `budget_core_h`: `unit_hours x workers x utilisation / (speed x margin)`, the predicted core-hours that fit in
  `--unit-hours` of wall time. `--margin` (default 2) covers the gap between synthetic and real order flow: a real
  shadow sees more book events than a synthetic one.

Prints one JSON document (also to `--out` when given). Treat the result as a sizing estimate: the workflow also
keeps `--unit-hours` well below the job's limit for the stream, the split and the reduce.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "python" / "tests" / "scale"))


def _unit(args: tuple[dict, str, str]) -> dict[str, Any]:
    from unit import run_unit

    unit, split, out = args
    t0 = time.perf_counter()
    m = run_unit(unit, Path(split), Path(out), 1)
    t = m["telemetry"]
    # a fresh process per unit (max_tasks_per_child=1), so the process peak is this unit's
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 if sys.platform != "darwin" else 1024 * 1024)
    return {
        "unit_id": unit["unit_id"],
        "predicted_core_s": unit["predicted_core_s"],
        "records": t["records"],
        "shadows": t["shadows"],
        "cpu_s": t["cpu_s"],
        "wall_s": round(time.perf_counter() - t0, 3),
        "peak_rss_mb": round(max(peak, t["peak_rss_mb"] or 0), 1),
    }


def fit_rss(units: list[dict[str, Any]]) -> tuple[float, float, float] | None:
    """Least-squares `peak_rss_mb = c + a x records + b x shadows`: (c, a, b); None when the units cannot separate
    records from shadows."""
    import numpy as np

    x = np.array([[1.0, u["records"], u["shadows"]] for u in units])
    y = np.array([u["peak_rss_mb"] for u in units])
    if len(units) < 3 or np.linalg.matrix_rank(x) < 3:
        return None
    c, a, b = np.linalg.lstsq(x, y, rcond=None)[0]
    return float(c), float(a), float(b)


def measure(work: Path, symbols: int, orders: int, workers: int, unit_core_s: float) -> dict[str, Any]:
    """Split, plan and run one synthetic day; every unit's telemetry and the units' wall time."""
    import itch_split
    import plan as P
    from scale_itch import write_itch_day

    date = "01022020"
    work.mkdir(parents=True, exist_ok=True)
    src = work / f"{date}.NASDAQ_ITCH50.gz"
    names = [f"S{i:03d}" for i in range(symbols)]
    # messages spread over the regular session, so shadows live their whole 300 s window
    n_msgs = 2 * orders * symbols
    write_itch_day(src, names, orders_per_symbol=orders, seed=7, max_step_ns=max(2, 2 * 23_000 * 10**9 // n_msgs))
    a = argparse.Namespace(
        date=date,
        url=f"file://{src}",
        buckets=4,
        out=work / "split",
        symbols=None,
        allow_missing=False,
        level=1,
        min_free_gib=0.0,
    )
    if itch_split.run_split(a) != 0:
        raise SystemExit("runner_budget: the synthetic split failed")
    plan_path = work / "plan.json"
    P.main(["--split", str(work / "split" / date), "--out", str(plan_path), "--unit-core-s", str(unit_core_s)])
    plan = json.loads(plan_path.read_text())
    todo = [(u, str(work / "split" / date), str(work / "shards" / u["unit_id"])) for u in plan["units"]]
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=1) as ex:
        units = list(ex.map(_unit, todo))
    wall = time.perf_counter() - t0
    return {"units": units, "wall_s": wall}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=4, help="units at a time, one thread each (as the workflow runs)")
    ap.add_argument("--unit-hours", type=float, default=3.5, help="wall hours the units of a day may take")
    ap.add_argument("--margin", type=float, default=2.0, help="real over synthetic CPU per predicted core-second")
    ap.add_argument("--wide", default="96x3000", help="the wide day: SYMBOLSxORDERS per symbol")
    ap.add_argument("--deep", default="4x150000", help="the deep day: SYMBOLSxORDERS per symbol")
    ap.add_argument("--unit-core-s", type=float, default=100.0, help="plan.py --unit-core-s, as the workflow uses")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    days = {k: tuple(int(x) for x in v.split("x")) for k, v in (("wide", a.wide), ("deep", a.deep))}
    runs = {}
    with tempfile.TemporaryDirectory(prefix="qt-runner-budget-") as tmp:
        for name, (symbols, orders) in days.items():
            runs[name] = measure(Path(tmp) / name, symbols, orders, a.workers, a.unit_core_s)
    wide = runs["wide"]["units"]
    cpu = sum(u["cpu_s"] for u in wide)
    speed = cpu / sum(u["predicted_core_s"] for u in wide)
    util = min(1.0, cpu / (runs["wide"]["wall_s"] * a.workers))
    every = [u for r in runs.values() for u in r["units"]]
    fit = fit_rss(every)
    doc = {
        "schema": "qt.scale.runner_budget",
        "machine": {"workers": a.workers, "threads": 1, "cpus": os.cpu_count()},
        "days": {
            name: {
                "symbols": days[name][0],
                "orders_per_symbol": days[name][1],
                "units": len(r["units"]),
                "records": sum(u["records"] for u in r["units"]),
                "shadows": sum(u["shadows"] for u in r["units"]),
                "units_wall_s": round(r["wall_s"], 1),
                "unit_cpu_s": round(sum(u["cpu_s"] for u in r["units"]), 1),
                "predicted_core_s": round(sum(u["predicted_core_s"] for u in r["units"]), 1),
            }
            for name, r in runs.items()
        },
        "unit_core_s": a.unit_core_s,
        "speed": round(speed, 3),
        "utilisation": round(util, 3),
        "rss": {
            "max_peak_mb": max(u["peak_rss_mb"] for u in every),
            **(
                {
                    "intercept_mb": round(fit[0], 1),
                    "mb_per_million_records": round(fit[1] * 1e6, 1),
                    "mb_per_thousand_shadows": round(fit[2] * 1e3, 1),
                }
                if fit
                else {}
            ),
        },
        "unit_hours": a.unit_hours,
        "margin": a.margin,
        "budget_core_h": round(a.unit_hours * a.workers * util / (speed * a.margin), 2),
        "units_measured": every,
    }
    text = json.dumps(doc, indent=1)
    if a.out:
        a.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
