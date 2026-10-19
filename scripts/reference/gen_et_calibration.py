"""et calibration reference values (SPEC 6.6 oracle O-STAT-5) -> tests/fixtures/reference/et_calibration.json.

Runs et's `python/exectruth/analytics/calibration.py` SOURCE, loaded read-only from its path (no package import,
no bytecode written into et), on seeded synthetic inputs, and records `calibrate(p, y, n_bins, n_boot, seed)` and
`reliability_table(p, y, n_bins)`. `queuetruth.stats.brier`, `murphy` (reliability, resolution, uncertainty,
within_bin), `hosmer_lemeshow` (statistic, p on n_bins - 2 df) and `reliability_table` (Wilson 95% CIs, equal-count
bins by rank with tied forecasts sharing a bin) must reproduce these values.

    QT_ET_ROOT=<et checkout>  python scripts/reference/gen_et_calibration.py

QT_ET_ROOT is required: a local checkout of the author's earlier private et prototype (SPEC 0: et HEAD
06b2396); the file's sha256 is recorded.
Inputs are drawn from the SPEC 3.1 RNG mirror and echoed into the fixture.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import sys
from pathlib import Path

from _common import header, rng_for, write_fixture

sys.dont_write_bytecode = True
ET_ROOT = Path(os.environ["QT_ET_ROOT"]) if os.environ.get("QT_ET_ROOT") else None
ET_FILE = ET_ROOT / "python" / "exectruth" / "analytics" / "calibration.py" if ET_ROOT else None


def load_et():
    if ET_FILE is None:
        raise SystemExit("QT_ET_ROOT must point at a local checkout of the et prototype")
    spec = importlib.util.spec_from_file_location("et_calibration_readonly", ET_FILE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def inputs(name: str, n: int, kind: str):
    r = rng_for(f"et_calibration/{name}")
    p, y = [], []
    for _ in range(n):
        u = r.next_f64()
        if kind == "continuous":
            q = round(u, 6)
            true = 0.03 + 0.92 * q
        elif kind == "grid":
            q = round(round(u * 20) / 20, 2)
            true = 0.05 + 0.88 * q
        elif kind == "constant":
            q = 0.3
            true = 0.3
        else:  # "skewed": most forecasts small
            q = round(u**3, 6)
            true = min(1.0, 1.1 * q + 0.01)
        p.append(q)
        y.append(int(r.next_f64() < true))
    return p, y


def table_records(t):
    return {c: [v.item() if hasattr(v, "item") else v for v in t[c].tolist()] for c in t.columns}


def main() -> None:
    et = load_et()
    sha = hashlib.sha256(ET_FILE.read_bytes()).hexdigest()
    cases = []
    for name, n, kind, n_bins, n_boot, seed in [
        ("continuous_n500", 500, "continuous", 10, 1000, 0),
        ("grid_ties_n400", 400, "grid", 10, 500, 1),
        ("constant_n50", 50, "constant", 10, 200, 2),
        ("skewed_n37", 37, "skewed", 10, 300, 3),
        ("continuous_n200_bins5", 200, "continuous", 5, 400, 4),
    ]:
        p, y = inputs(name, n, kind)
        res = et.calibrate(p, y, n_bins=n_bins, n_boot=n_boot, seed=seed)
        rt = et.reliability_table(p, y, n_bins)
        summary = {k: v for k, v in res.items() if k != "table"}
        summary = {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in summary.items()}
        cases.append(
            {
                "name": name,
                "n_bins": n_bins,
                "n_boot": n_boot,
                "seed": seed,
                "input": {"p": p, "y": y},
                "calibrate": summary,
                "reliability_table": table_records(rt),
            }
        )
    doc = header(["O-STAT-5"], "gen_et_calibration.py", __doc__.strip(), ["numpy", "scipy", "pandas"])
    doc["et_source"] = {"path": "python/exectruth/analytics/calibration.py", "repo_head": "06b2396", "sha256": sha}
    doc["tolerance"] = {"all": "rel 1e-12 (abs 1e-15 near 0)"}
    doc["cases"] = cases
    print(write_fixture("et_calibration.json", doc))


if __name__ == "__main__":
    main()
