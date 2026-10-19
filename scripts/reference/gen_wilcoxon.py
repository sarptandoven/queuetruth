"""Wilcoxon signed-rank reference values (SPEC 5.7.5, oracle O-STAT-2) -> tests/fixtures/reference/wilcoxon.json.

20 seeded samples of paired log-ratio differences `d` (sizes 5..80) with exact zeros and ties (values rounded to a
grid), plus hand edge cases. For each sample: `scipy.stats.wilcoxon(d, zero_method=z, alternative="two-sided",
method="auto")` for z in {wilcox, pratt, zsplit}; SPEC 5.7.5 uses `zero_method="wilcox"`, `method="auto"`.
scipy's `method="auto"` picks exact or asymptotic (and handles zeros/ties) version-dependently, so the fixture is
only valid for the scipy version in `software`; a mismatch after a scipy upgrade means regenerate, not a bug.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
import scipy
from _common import header, rng_for, write_fixture
from scipy import stats


def normal(r) -> float:
    u1, u2 = r.next_f64(), r.next_f64()
    return math.sqrt(-2.0 * math.log(1.0 - u1)) * math.cos(2.0 * math.pi * u2)


def run(d: list[float]) -> dict:
    out = {}
    for z in ("wilcox", "pratt", "zsplit"):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            res = stats.wilcoxon(np.asarray(d), zero_method=z, alternative="two-sided", method="auto")
        out[z] = {
            "statistic": float(res.statistic),
            "pvalue": float(res.pvalue),
            "warnings": sorted({str(x.message).split(".")[0] for x in w}),
        }
    return out


def main() -> None:
    cases = []
    sizes = [5, 6, 8, 10, 12, 15, 18, 20, 24, 25, 30, 35, 40, 45, 49, 50, 51, 60, 70, 80]
    for i, n in enumerate(sizes):
        r = rng_for(f"wilcoxon/{i}")
        shift = (-0.3, 0.0, 0.25, 0.6)[i % 4]
        grid = (0.1, 0.25, 0.05, 0.5)[i % 4]
        d = []
        for _ in range(n):
            v = round((normal(r) + shift) / grid) * grid
            d.append(float(round(v, 10)))
        if i % 5 == 0:  # make sure some samples carry explicit zeros
            d[0] = 0.0
            d[-1] = 0.0
        cases.append({"name": f"seeded_{i:02d}_n{n}", "d": d, "scipy": run(d)})
    hand = {
        "no_ties_n7": [0.5, -1.2, 2.3, 0.7, -0.1, 1.9, 3.3],
        "ties_n9": [1.0, 1.0, -1.0, 2.0, 2.0, 2.0, -3.0, 0.5, 0.5],
        "zeros_n10": [0.0, 0.0, 1.0, -2.0, 3.0, 1.0, 4.0, -1.0, 2.0, 5.0],
        "all_positive_n12": [float(k) for k in range(1, 13)],
    }
    for name, d in hand.items():
        cases.append({"name": name, "d": d, "scipy": run(d)})
    doc = header(["O-STAT-2"], "gen_wilcoxon.py", __doc__.strip(), ["numpy", "scipy"])
    doc["scipy_version_note"] = f"generated with scipy {scipy.__version__}"
    doc["tolerance"] = {"statistic": "abs 1e-12", "pvalue": "rel 1e-12"}
    doc["cases"] = cases
    print(write_fixture("wilcoxon.json", doc))


if __name__ == "__main__":
    main()
