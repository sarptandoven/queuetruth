"""McNemar reference values (SPEC 5.7.5, oracle O-STAT-1) -> tests/fixtures/reference/mcnemar.json.

For each 2x2 table (rows live filled/not, columns sim filled/not; b = live_only, c = sim_only):
- SPEC formulas computed here independently: `chi2_cc = (|b - c| - 1)^2 / (b + c)` (0 if b + c = 0),
  `p_chi2 = chi2.sf(chi2_cc, 1)`, `p_exact = min(1, 2 * sum_{i<=min(b,c)} C(b+c, i) / 2^(b+c))` in exact rational
  arithmetic, `odds_ratio = c / b`, Wald CI `exp(ln(c/b) +- 1.96 sqrt(1/b + 1/c))` (null if b or c is 0).
- `statsmodels.stats.contingency_tables.mcnemar(exact=True)` and `(exact=False, correction=True)`, and
  `scipy.stats.binomtest(min(b, c), b + c, 0.5).pvalue`; the generator asserts they agree with the SPEC values.
Tables: the SPEC golden (3, 12), hand edge cases, and 50 random tables from the SPEC 3.1 RNG.
"""

from __future__ import annotations

import math
import warnings
from fractions import Fraction

import numpy as np
from _common import header, rng_for, write_fixture
from scipy import stats
from statsmodels.stats.contingency_tables import mcnemar as sm_mcnemar


def spec_values(b: int, c: int) -> dict:
    n = b + c
    chi2_cc = 0.0 if n == 0 else (abs(b - c) - 1) ** 2 / n
    p_chi2 = float(stats.chi2.sf(chi2_cc, 1))
    tail = sum((Fraction(math.comb(n, i), 2**n) for i in range(min(b, c) + 1)), Fraction(0))
    p_exact = float(min(Fraction(1), 2 * tail))
    if b == 0 or c == 0:
        orr, lo, hi = None, None, None
    else:
        orr = c / b
        half = 1.96 * math.sqrt(1 / b + 1 / c)
        lo, hi = math.exp(math.log(c / b) - half), math.exp(math.log(c / b) + half)
    return {"chi2_cc": chi2_cc, "p_chi2": p_chi2, "p_exact": p_exact, "odds_ratio": orr, "or_ci_lo": lo, "or_ci_hi": hi}


def library_values(n11: int, b: int, c: int, n00: int) -> dict:
    table = np.array([[n11, b], [c, n00]])
    ex = sm_mcnemar(table, exact=True)
    out = {
        "statsmodels_exact": {"statistic": float(ex.statistic), "pvalue": float(ex.pvalue)},
        "scipy_binomtest_pvalue": float(stats.binomtest(min(b, c), b + c, 0.5).pvalue) if b + c > 0 else 1.0,
    }
    if b + c > 0:
        ch = sm_mcnemar(table, exact=False, correction=True)
        out["statsmodels_chi2_corrected"] = {"statistic": float(ch.statistic), "pvalue": float(ch.pvalue)}
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ch = sm_mcnemar(table, exact=False, correction=True)
        out["statsmodels_chi2_corrected"] = {"statistic": None, "pvalue": None, "note": "0/0 in statsmodels"}
    return out


def case(name: str, n11: int, b: int, c: int, n00: int) -> dict:
    s = spec_values(b, c)
    lib = library_values(n11, b, c, n00)
    assert math.isclose(s["p_exact"], lib["statsmodels_exact"]["pvalue"], rel_tol=1e-13, abs_tol=0), name
    assert math.isclose(s["p_exact"], lib["scipy_binomtest_pvalue"], rel_tol=1e-13, abs_tol=0), name
    if b + c > 0:
        assert math.isclose(s["chi2_cc"], lib["statsmodels_chi2_corrected"]["statistic"], rel_tol=1e-14), name
        assert math.isclose(s["p_chi2"], lib["statsmodels_chi2_corrected"]["pvalue"], rel_tol=1e-12), name
    return {"name": name, "n11": n11, "b": b, "c": c, "n00": n00, "spec": s, **lib}


def main() -> None:
    cases = [
        case("golden_b3_c12", 40, 3, 12, 45),
        case("zero_discordant", 10, 0, 0, 10),
        case("b0_c5", 7, 0, 5, 9),
        case("b5_c0", 7, 5, 0, 9),
        case("b1_c1", 3, 1, 1, 3),
        case("b4_c4_equal", 20, 4, 4, 20),
        case("b2_c3", 0, 2, 3, 0),
        case("large_b250_c300", 1000, 250, 300, 2000),
        case("large_b1000_c1100", 5000, 1000, 1100, 5000),
    ]
    r = rng_for("mcnemar")
    for i in range(50):
        hi = (10, 40, 200)[i % 3]
        b, c = r.range_inclusive(0, hi), r.range_inclusive(0, hi)
        n11, n00 = r.range_inclusive(0, 500), r.range_inclusive(0, 500)
        cases.append(case(f"random_{i:02d}", n11, b, c, n00))
    golden = cases[0]["spec"]
    assert golden["p_exact"] == 0.03515625
    assert golden["chi2_cc"] == 4.266666666666667
    assert golden["p_chi2"] == 0.038867103812417245, golden["p_chi2"]
    doc = header(["O-STAT-1"], "gen_mcnemar.py", __doc__.strip(), ["numpy", "scipy", "statsmodels"])
    doc["tolerance"] = {"p_exact": "rel 1e-13", "chi2_cc": "rel 1e-14", "p_chi2": "rel 1e-12", "or_ci": "rel 1e-12"}
    doc["cases"] = cases
    print(write_fixture("mcnemar.json", doc))


if __name__ == "__main__":
    main()
