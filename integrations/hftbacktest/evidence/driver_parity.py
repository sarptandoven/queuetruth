#!/usr/bin/env python3
"""`hbt_driver.py` vs the per-row O-ZOO-3 driver (hftbacktest venv): same fills per (order, timestamp) and same stop
code on every run of a `hbt_crosscheck.py` output. Prints {"runs", "identical", "differ": [first 10]} as JSON.

Usage: <hbt venv>/bin/python -I driver_parity.py NPZ_DIR CROSSCHECK_RUNS.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hbt_config
import hbt_driver


def main(argv: list[str]) -> int:
    npz_dir, runs_path = Path(argv[0]), Path(argv[1])
    expected = json.loads(runs_path.read_text())["runs"]
    hftbacktest, njit = hbt_driver._import_hbt()
    loop = hbt_driver.make_loop(njit)
    by: dict[str, list] = {}
    for r in expected:
        by.setdefault(r["scenario"], []).append(r)
    n_runs = n_same = 0
    differ = []
    for scen, runs in sorted(by.items()):
        data = np.ascontiguousarray(np.load(npz_dir / f"{scen}.npz")["data"].astype(hftbacktest.event_dtype))
        meta = json.loads((npz_dir / f"{scen}.orders.json").read_text())
        acts = hbt_driver.load_actions(meta)
        for r in runs:
            e = hbt_config.make_entry(r["family"][4:], r["exch"], [] if r["n"] is None else [r["n"]], "fixed")
            out = hbt_driver.run_model(hftbacktest, loop, data, meta, acts, e)
            exp: dict[tuple[int, int], float] = {}
            for f in r["fills"]:
                k = (f["order_id"], f["ts"])
                exp[k] = exp.get(k, 0.0) + f["fill_qty"]
            got: dict[tuple[int, int], float] = {}
            for f in out["fills"]:
                k = (f["order_id"], f["ts"])
                got[k] = got.get(k, 0.0) + f["qty"]
            exp_err = None if r["error"] is None else r["error"]["error_code"]
            got_err = None if out["error"] is None else out["error"]["code"]
            n_runs += 1
            if exp == got and exp_err == got_err:
                n_same += 1
            elif len(differ) < 10:
                differ.append({"scenario": scen, "model_id": e["model_id"], "per_row_error": exp_err, "error": got_err})
    print(json.dumps({"runs": n_runs, "identical": n_same, "differ": differ}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
