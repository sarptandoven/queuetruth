#!/usr/bin/env python3
"""What the #312 fix changes on the O-ZOO-3 synthetic scenarios, and whether `hbt_driver.py` matches the per-row
O-ZOO-3 driver (needs numpy only; hftbacktest runs in the venvs given).

1. Writes the 83 scenarios of `tests/fixtures/zoo/hbt_real/scenarios.json` (the O-ZOO-3 replay contract) as SPEC
   5.3.5 npz feeds plus orders sidecars, as the scenario generator does.
2. Runs `python/tests/zoo/hbt_crosscheck.py` (per-row stepping, 18 queue models x 2 exchanges) with the released
   build (`--stock`) and the #312-patched build (`--patched`).
3. Compares them: runs stopped by an error per build, non-stopped runs whose fills, order events and first fills are
   identical, stopped runs whose fills are a prefix of the patched run's fills.
4. Runs `driver_parity.py` with each build: `hbt_driver.py` vs the per-row driver on every run.

Prints and (`--out`) writes `qtint.hbt_synthetic_regression` v1 (aggregate counts only).

Usage: python integrations/hftbacktest/evidence/synthetic_regression.py --stock .scratch/hbt-venv/bin/python \\
         --patched .scratch/hbt-venv-312/bin/python --work .scratch/hbt-synth --out .scratch/hbt-synth/summary.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SCENARIOS = REPO / "tests" / "fixtures" / "zoo" / "hbt_real" / "scenarios.json"
CROSSCHECK = REPO / "python" / "tests" / "zoo" / "hbt_crosscheck.py"
EVENT_DTYPE = np.dtype(
    [("ev", "<u8"), ("exch_ts", "<i8"), ("local_ts", "<i8"), ("px", "<f8"), ("qty", "<f8"), ("order_id", "<u8"),
     ("ival", "<i8"), ("fval", "<f8")]
)  # fmt: skip


def write_npz(scenarios: Path, npz_dir: Path) -> int:
    """The scenarios as npz feeds and orders sidecars (byte layout of the generator's `write_all`)."""
    doc = json.loads(scenarios.read_text())
    npz_dir.mkdir(parents=True, exist_ok=True)
    for s in doc["scenarios"]:
        arr = np.zeros(len(s["events"]), dtype=EVENT_DTYPE)
        for j, e in enumerate(s["events"]):
            px = e.get("px")
            arr[j] = (e["ev"], e["ts"], e["ts"], np.nan if px is None else px, e.get("qty", 0.0), 0, 0, 0.0)
        np.savez_compressed(npz_dir / f"{s['id']}.npz", data=arr)
        meta = {"id": s["id"], "tick_size": s["tick_size"], "lot_size": s["lot_size"], "actions": s["actions"]}
        (npz_dir / f"{s['id']}.orders.json").write_text(json.dumps(meta, indent=1) + "\n")
    return len(doc["scenarios"])


def compare_builds(stock: dict, patched: dict) -> dict:
    """Counts of what changes between the two builds' runs (keyed by scenario and model)."""
    a = {(r["scenario"], r["model_id"]): r for r in stock["runs"]}
    b = {(r["scenario"], r["model_id"]): r for r in patched["runs"]}
    if a.keys() != b.keys():
        raise SystemExit("QT-INPUT-002: the two builds ran different scenario x model sets")
    codes: dict[str, int] = {}
    same = changed = prefix = 0
    for k, ra in a.items():
        rb = b[k]
        if ra["error"]:
            c = str(ra["error"]["error_code"])
            codes[c] = codes.get(c, 0) + 1
            fa = [(f["order_id"], f["ts"], f["fill_qty"]) for f in ra["fills"]]
            fb = [(f["order_id"], f["ts"], f["fill_qty"]) for f in rb["fills"]]
            prefix += fb[: len(fa)] == fa
        elif (ra["fills"], ra["order_events"], ra["first_fill"]) == (rb["fills"], rb["order_events"], rb["first_fill"]):
            same += 1
        else:
            changed += 1
    return {
        "runs": len(a),
        "stock_stopped": sum(1 for r in a.values() if r["error"]),
        "stock_error_codes": dict(sorted(codes.items())),
        "patched_stopped": sum(1 for r in b.values() if r["error"]),
        "unstopped_runs_identical": same,
        "unstopped_runs_changed": changed,
        "stopped_runs_with_fills_a_prefix_of_patched": prefix,
    }


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"QT-ENGINE-001: {' '.join(cmd[:3])} failed:\n{r.stderr[-2000:]}")
    return r.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stock", required=True, help="python of the released hftbacktest venv")
    ap.add_argument("--patched", required=True, help="python of the #312-patched hftbacktest venv")
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--scenarios", type=Path, default=SCENARIOS)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    npz = args.work / "npz"
    n = write_npz(args.scenarios, npz)
    runs = {}
    parity = {}
    for label, py in (("stock", args.stock), ("patched", args.patched)):
        out = args.work / f"crosscheck-{label}.json"
        _run([py, "-I", str(CROSSCHECK), str(npz), "--out", str(out)])
        runs[label] = json.loads(out.read_text())
        parity[label] = json.loads(_run([py, "-I", str(HERE / "driver_parity.py"), str(npz), str(out)]))
    doc = {
        "schema": "qtint.hbt_synthetic_regression",
        "schema_version": 1,
        "scenarios": n,
        "builds": compare_builds(runs["stock"], runs["patched"]),
        "hbt_driver_vs_per_row_driver": parity,
    }
    text = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
