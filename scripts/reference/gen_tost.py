"""TOST equivalence reference values (SPEC 5.7.5, oracle O-STAT-4) -> tests/fixtures/reference/tost.json.

- `tost_p(estimate, se, margin) = max(1 - Phi((est + margin) / se), Phi((est - margin) / se))` with
  `Phi = scipy.stats.norm.cdf`, written literally (`1 - cdf`). `p_sf_variant` uses `norm.sf` for the first term;
  the two agree to 1e-15 on every case here (they diverge only far in the tail, where both are ~0).
- `tost_from_ci(ci90, margin)`: equivalent iff the 90% CI lies inside `[-margin, margin]`, read as the CLOSED
  interval (`-margin <= lo and hi <= margin`); boundary cases are included and flagged.
"""

from __future__ import annotations

from _common import header, write_fixture
from scipy.stats import norm


def tost_p(est: float, se: float, m: float) -> tuple[float, float]:
    lit = max(1.0 - float(norm.cdf((est + m) / se)), float(norm.cdf((est - m) / se)))
    sfv = max(float(norm.sf((est + m) / se)), float(norm.cdf((est - m) / se)))
    return lit, sfv


def main() -> None:
    p_cases = []
    for est, se, m in [
        (0.0, 0.01, 0.02),
        (0.005, 0.01, 0.02),
        (-0.012, 0.008, 0.02),
        (0.019, 0.004, 0.02),
        (0.03, 0.01, 0.02),
        (-0.025, 0.02, 0.02),
        (0.0, 0.05, 0.02),
        (0.25, 0.4, 1.0),
        (-1.5, 0.3, 1.0),
        (0.0, 1e-4, 0.01),
    ]:
        lit, sfv = tost_p(est, se, m)
        assert abs(lit - sfv) <= 1e-15
        p_cases.append({"estimate": est, "se": se, "margin": m, "tost_p": lit, "p_sf_variant": sfv})
    ci_cases = [
        {"ci90": [-0.01, 0.015], "margin": 0.02, "equivalent": True, "boundary": False},
        {"ci90": [-0.025, 0.01], "margin": 0.02, "equivalent": False, "boundary": False},
        {"ci90": [0.005, 0.021], "margin": 0.02, "equivalent": False, "boundary": False},
        {"ci90": [-0.02, 0.02], "margin": 0.02, "equivalent": True, "boundary": True},
        {"ci90": [-0.02, 0.0], "margin": 0.02, "equivalent": True, "boundary": True},
        {"ci90": [-0.03, 0.03], "margin": 0.02, "equivalent": False, "boundary": False},
        {"ci90": [0.0, 0.0], "margin": 0.0, "equivalent": True, "boundary": True},
        {"ci90": [-0.5, 0.75], "margin": 1.0, "equivalent": True, "boundary": False},
    ]
    doc = header(["O-STAT-4"], "gen_tost.py", __doc__.strip(), ["scipy"])
    doc["tolerance"] = {"tost_p": "abs 1e-15"}
    doc["tost_p"] = p_cases
    doc["tost_from_ci"] = ci_cases
    print(write_fixture("tost.json", doc))


if __name__ == "__main__":
    main()
