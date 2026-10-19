"""Scoring-rule reference values -> tests/fixtures/reference/scoring_rules.json.

Sections:
- `zoo_metrics` (SPEC 5.6, `queuetruth.eval.evaluate_zoo`): Brier `mean((p - y)^2)`, log loss with `pc = clip(p, 1e-6,
  1 - 1e-6)`, `fill_rate_bias = mean(p) - mean(y)`, at h_max model filled = `p >= 0.5`, `b` truth-only, `c`
  model-only, `overfill_bias = (c - b) / N`, McNemar `p_exact`; censored rows excluded. Exact rationals where the
  formula is rational.
- `calibration` (SPEC 5.5.7 / 4.3 `spiegelhalter_z`, `calibration_slope_intercept`, `brier`, `log_loss`):
  Spiegelhalter `z = sum (y - p)(1 - 2p) / sqrt(sum (1 - 2p)^2 p (1 - p))` with two-sided normal p
  `2 * norm.sf(|z|)`; slope/intercept of the logistic regression of y on logit(p) from statsmodels `Logit`
  (Newton/IRLS, tol 1e-12) as the reference for the project's own IRLS. Seeded n = 300.
- `survival_scoring` (SPEC 5.5.7, O-MOD-5; values are hand-computable): a 6-order example with
  per-interval raw hazards; `rcll`, `ipcw_logloss(tau)`, IPCW Brier `BS(tau)` for the fill cause, `ibs`, and the
  censoring KM `G` (floored at 0.05). S, CIF, G and BS are exact rationals (also given as "p/q" strings).
  The example puts h_max = tau_J and one order censored exactly at the cap: the SPEC leaves this case open; see the
  literal reading used here and the alternative value recorded alongside.
"""

from __future__ import annotations

import math
from fractions import Fraction as Fr

import numpy as np
import statsmodels.api as sm
from _common import header, rng_for, write_fixture
from scipy.stats import norm


def fstr(x: Fr) -> str:
    return f"{x.numerator}/{x.denominator}"


def mcnemar_p_exact(b: int, c: int) -> float:
    n = b + c
    tail = sum((Fr(math.comb(n, i), 2**n) for i in range(min(b, c) + 1)), Fr(0))
    return float(min(Fr(1), 2 * tail))


def zoo_metrics() -> dict:
    rows = [  # (p, truth_filled_by_h_max, censored)
        (0.0, 0, False),
        (1.0, 1, False),
        (0.5, 0, False),
        (0.49, 1, False),
        (0.8, 1, False),
        (0.65, 0, False),
        (0.2, 0, False),
        (0.95, 0, False),
        (0.3, 1, True),
        (0.55, 1, False),
        (0.1, 1, False),
        (0.7, 0, True),
        (0.51, 0, False),
        (0.05, 0, False),
        (0.999999, 1, False),
    ]
    kept = [(Fr(str(p)), y) for p, y, cens in rows if not cens]
    n = len(kept)
    brier = sum((p - y) ** 2 for p, y in kept) / n
    eps = 1e-6
    ll = (
        -sum(
            y * math.log(min(max(float(p), eps), 1 - eps)) + (1 - y) * math.log(1 - min(max(float(p), eps), 1 - eps))
            for p, y in kept
        )
        / n
    )
    frb = sum(p for p, _ in kept) / n - Fr(sum(y for _, y in kept), n)
    model = [int(p >= Fr(1, 2)) for p, _ in kept]
    b = sum(1 for (p, y), m in zip(kept, model, strict=True) if y == 1 and m == 0)
    c = sum(1 for (p, y), m in zip(kept, model, strict=True) if y == 0 and m == 1)
    return {
        "input": [{"p": p, "y": y, "censored": cens} for p, y, cens in rows],
        "n_scored": n,
        "brier": float(brier),
        "brier_exact": fstr(brier),
        "logloss": ll,
        "fill_rate_bias": float(frb),
        "fill_rate_bias_exact": fstr(frb),
        "b_truth_only": b,
        "c_model_only": c,
        "overfill_bias": (c - b) / n,
        "mcnemar_p_exact": mcnemar_p_exact(b, c),
    }


def calibration() -> dict:
    r = rng_for("scoring/calibration")
    p, y = [], []
    for _ in range(300):
        q = 0.02 + 0.96 * r.next_f64()
        true = 1 / (1 + math.exp(-(0.3 + 1.4 * math.log(q / (1 - q)))))  # miscalibrated truth
        p.append(round(q, 6))
        y.append(int(r.next_f64() < true))
    pa, ya = np.array(p), np.array(y, dtype=float)
    num = float(np.sum((ya - pa) * (1 - 2 * pa)))
    den = math.sqrt(float(np.sum((1 - 2 * pa) ** 2 * pa * (1 - pa))))
    z = num / den
    x = sm.add_constant(np.log(pa / (1 - pa)))
    fit = sm.Logit(ya, x).fit(method="newton", tol=1e-12, maxiter=100, disp=0)
    eps = 1e-6
    pc = np.clip(pa, eps, 1 - eps)
    return {
        "input": {"p": p, "y": y},
        "brier": float(np.mean((pa - ya) ** 2)),
        "log_loss_eps1e-6": float(-np.mean(ya * np.log(pc) + (1 - ya) * np.log(1 - pc))),
        "spiegelhalter_z": z,
        "spiegelhalter_p_two_sided": float(2 * norm.sf(abs(z))),
        "calibration_intercept": float(fit.params[0]),
        "calibration_slope": float(fit.params[1]),
        "calibration_converged": bool(fit.mle_retvals["converged"]),
    }


def survival_scoring() -> dict:
    tau = [Fr(1), Fr(2), Fr(5)]  # seconds; tau_0 = 0; h_max = tau_J = 5 s
    rows = [  # (e seconds, cause, h_fill per interval, h_cancel per interval)
        (Fr(0), "fill", [Fr(3, 10), Fr(1, 5), Fr(1, 10)], [Fr(1, 10), Fr(1, 10), Fr(1, 5)]),
        (Fr(1), "cancel", [Fr(1, 10), Fr(1, 10), Fr(1, 10)], [Fr(1, 4), Fr(1, 5), Fr(1, 5)]),
        (Fr(3, 2), "fill", [Fr(1, 5), Fr(2, 5), Fr(1, 5)], [Fr(1, 20), Fr(1, 10), Fr(1, 10)]),
        (Fr(3), "censor", [Fr(1, 20), Fr(1, 20), Fr(1, 10)], [Fr(1, 20), Fr(1, 10), Fr(1, 10)]),
        (Fr(7, 10), "censor", [Fr(1, 10), Fr(1, 5), Fr(3, 10)], [Fr(1, 10), Fr(1, 10), Fr(1, 10)]),
        (Fr(5), "censor", [Fr(1, 25), Fr(1, 25), Fr(1, 20)], [Fr(1, 50), Fr(1, 50), Fr(1, 25)]),
    ]
    J = len(tau)

    def interval(e: Fr) -> int:  # 1-based K with tau_{K-1} < e <= tau_K; e = 0 -> 1; e > tau_J -> J + 1
        if e == 0:
            return 1
        for j, t in enumerate(tau, start=1):
            if e <= t:
                return j
        return J + 1

    per_row = []
    for e, c, hf, hc in rows:
        s, cf, cc = [Fr(1)], [Fr(0)], [Fr(0)]
        for j in range(J):
            cf.append(cf[-1] + hf[j] * s[-1])
            cc.append(cc[-1] + hc[j] * s[-1])
            s.append(s[-1] * (1 - hf[j] - hc[j]))
        per_row.append(
            {"e": e, "cause": c, "hf": hf, "hc": hc, "S": s, "CIF_fill": cf, "CIF_cancel": cc, "K": interval(e)}
        )

    # rcll (literal SPEC): event cause c in K -> h_c(K) S(tau_{K-1}); censored in K -> S(tau_{K-1});
    # e > tau_J -> S(tau_J); floor 1e-12. Alternative: censoring exactly at tau_J (the h_max cap) -> S(tau_J).
    def rcll(cap_is_survival: bool) -> tuple[float, list[float]]:
        terms = []
        for r in per_row:
            k = r["K"]
            if k == J + 1:
                prob = r["S"][J]
            elif r["cause"] == "censor":
                prob = r["S"][J] if (cap_is_survival and r["e"] == tau[-1]) else r["S"][k - 1]
            else:
                h = r["hf"] if r["cause"] == "fill" else r["hc"]
                prob = h[k - 1] * r["S"][k - 1]
            terms.append(-math.log(max(float(prob), 1e-12)) + 0.0)
        return sum(terms) / len(terms), terms

    # Censoring KM G (event = cause censor), right-continuous, floored at 0.05.
    times = sorted({r["e"] for r in per_row})

    def g_raw(t: Fr) -> Fr:
        g = Fr(1)
        for u in times:
            if u > t:
                break
            at_risk = sum(1 for r in per_row if r["e"] >= u)
            d = sum(1 for r in per_row if r["e"] == u and r["cause"] == "censor")
            g *= 1 - Fr(d, at_risk)
        return g

    def G(t: Fr) -> Fr:
        return max(g_raw(t), Fr(1, 20))

    n = len(per_row)
    ipcw_ll, bs = {}, {}
    for j, t in enumerate(tau, start=1):
        ll_terms, bs_sum = [], Fr(0)
        for r in per_row:
            e, c = r["e"], r["cause"]
            ff, fc = r["CIF_fill"][j], r["CIF_cancel"][j]
            s = max(1 - ff - fc, Fr(0))
            if e <= t and c == "fill":
                ll_terms.append(math.log(float(ff)) / float(G(e)))
                bs_sum += (1 - ff) ** 2 / G(e)
            elif e <= t and c == "cancel":
                ll_terms.append(math.log(float(fc)) / float(G(e)))
                bs_sum += (0 - ff) ** 2 / G(e)
            elif e > t:
                ll_terms.append(math.log(max(float(s), 1e-12)) / float(G(t)))
                bs_sum += (0 - ff) ** 2 / G(t)
            else:
                ll_terms.append(0.0)  # censored at or before tau: weight 0
        ipcw_ll[str(float(t))] = -sum(ll_terms) / n
        bs[str(float(t))] = bs_sum / n
    ibs = sum(bs[str(float(tau[j]))] * (tau[j] - (tau[j - 1] if j else 0)) for j in range(J)) / tau[-1]
    rc, rc_terms = rcll(False)
    rc_alt, _ = rcll(True)
    return {
        "grid_s": [float(t) for t in tau],
        "h_max_s": 5.0,
        "rows": [
            {
                "event_s": float(r["e"]),
                "cause": r["cause"],
                "interval_K": r["K"],
                "h_fill": [float(x) for x in r["hf"]],
                "h_cancel": [float(x) for x in r["hc"]],
                "S": [float(x) for x in r["S"]],
                "S_exact": [fstr(x) for x in r["S"]],
                "CIF_fill": [float(x) for x in r["CIF_fill"]],
                "CIF_cancel": [float(x) for x in r["CIF_cancel"]],
                "G_at_event": float(G(r["e"])),
                "G_at_event_exact": fstr(G(r["e"])),
            }
            for r in per_row
        ],
        "G_at_tau": {str(float(t)): float(G(t)) for t in tau},
        "rcll": rc,
        "rcll_terms": rc_terms,
        "rcll_alt_cap_censor_counts_as_survival": rc_alt,
        "ipcw_logloss": ipcw_ll,
        "ipcw_brier_fill": {k: float(v) for k, v in bs.items()},
        "ipcw_brier_fill_exact": {k: fstr(v) for k, v in bs.items()},
        "ibs": float(ibs),
        "ibs_exact": fstr(ibs),
        "notes": [
            "F_fill/F_cancel at tau_j are the raw CIFs (recalibration taken as identity for this example).",
            "S(tau) in ipcw_logloss is max(1 - F_fill - F_cancel, 1e-12).",
            "Rows censored at or before tau (including exactly at tau) get weight 0, as 1{e_i > tau} is false.",
            "No censoring time coincides with an event time, so G(t) vs G(t-) does not matter here.",
            "BS and ipcw_logloss average over all n = 6 rows (zero-weight rows included in the denominator).",
        ],
    }


def main() -> None:
    doc = header(["O-STAT-5", "O-MOD-5"], "gen_scoring.py", __doc__.strip(), ["numpy", "scipy", "statsmodels"])
    doc["tolerance"] = {"rational_values": "abs 1e-15", "log_values": "rel 1e-12", "logistic_fit": "abs 1e-8"}
    doc["zoo_metrics"] = zoo_metrics()
    doc["calibration"] = calibration()
    doc["survival_scoring"] = survival_scoring()
    print(write_fixture("scoring_rules.json", doc))


if __name__ == "__main__":
    main()
