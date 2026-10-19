"""Multiple-testing reference values (SPEC 5.7.5, oracle O-STAT-4) -> tests/fixtures/reference/multitest.json.

`statsmodels.stats.multitest.multipletests(p, alpha=q, method=m)` for m in {fdr_bh, fdr_by, holm} and
q in {0.05, 0.10}: adjusted p-values (`pvals_corrected`, independent of q) and the reject vector.
`queuetruth.stats.benjamini_hochberg(p, q)`, `benjamini_yekutieli(p, q)`, `holm(p)` return the adjusted p-values;
`reject = p_adj <= q` must equal statsmodels' reject. Vectors include ties, exact 0 and 1, unsorted order,
values on the BH boundary, a single p-value and an all-null vector.
"""

from __future__ import annotations

import numpy as np
from _common import header, rng_for, write_fixture
from statsmodels.stats.multitest import multipletests


def run(p: list[float]) -> dict:
    out = {}
    for m in ("fdr_bh", "fdr_by", "holm"):
        res = {}
        for q in (0.05, 0.10):
            reject, adj, _, _ = multipletests(np.asarray(p), alpha=q, method=m)
            res[f"q={q}"] = {"reject": reject.astype(bool), "p_adj": adj}
        assert np.array_equal(res["q=0.05"]["p_adj"], res["q=0.1"]["p_adj"])
        out[m] = {
            "p_adj": res["q=0.05"]["p_adj"],
            "reject_q0.05": res["q=0.05"]["reject"],
            "reject_q0.10": res["q=0.1"]["reject"],
        }
    return out


def main() -> None:
    vectors = {
        "single": [0.03],
        "hand_unsorted_ties": [0.04, 0.001, 0.04, 0.2, 0.0, 1.0, 0.012, 0.04, 0.5, 0.03],
        "bh_boundary_m5": [0.01, 0.02, 0.03, 0.04, 0.05],  # p_(i) = i q / m exactly at q = 0.05
        "all_null": [0.31, 0.77, 0.52, 0.94, 0.68, 0.45],
        "all_tiny": [1e-12, 3e-10, 2e-8, 5e-7],
    }
    for i, m in enumerate((8, 20, 57)):
        r = rng_for(f"multitest/{i}")
        p = []
        for _ in range(m):
            u = r.next_f64()
            # mixture: a third of the tests are "signals" with small p
            p.append(round(u**4 if r.next_f64() < 1 / 3 else u, 6))
        vectors[f"seeded_{i}_m{m}"] = p
    cases = [{"name": k, "p": v, "statsmodels": run(v)} for k, v in vectors.items()]
    doc = header(["O-STAT-4"], "gen_multitest.py", __doc__.strip(), ["numpy", "statsmodels"])
    doc["tolerance"] = {"p_adj": "abs 1e-15"}
    doc["cases"] = cases
    print(write_fixture("multitest.json", doc))


if __name__ == "__main__":
    main()
