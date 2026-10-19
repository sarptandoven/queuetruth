"""Isotonic recalibration reference values (SPEC 5.5.5) -> tests/fixtures/reference/isotonic.json.

Reference: `sklearn.isotonic.IsotonicRegression(y_min=0, y_max=1, increasing=True, out_of_bounds="clip")` fitted
with sample weights; recorded `X_thresholds_`, `y_thresholds_` and `predict(query_x)`. Cross-checked against
`scipy.optimize.isotonic_regression` (PAV) after sklearn's own tie handling (weighted mean of y per distinct x)
followed by linear interpolation between thresholds (np.interp; outside the fitted range: clip).
Cases: unweighted with ties in x, IPCW-weighted as in SPEC 5.5.5 (weights `1 / max(G(min(e, tau)), 0.05)`, rows
censored before tau excluded), already-monotone input, and an all-decreasing input (collapses to one level).
Also `monotone_across_horizons`: per row `cif_j = max(cif_j, cif_{j-1})` then clip to [0, 1].
"""

from __future__ import annotations

import numpy as np
import sklearn
from _common import header, rng_for, write_fixture
from scipy.optimize import isotonic_regression
from sklearn.isotonic import IsotonicRegression


def scipy_path(x, y, w, q):
    xs, inv = np.unique(x, return_inverse=True)
    sw = np.bincount(inv, weights=w)
    sy = np.bincount(inv, weights=w * y) / sw
    fit = isotonic_regression(sy, weights=sw, increasing=True).x
    fit = np.clip(fit, 0.0, 1.0)
    return np.interp(np.clip(q, xs[0], xs[-1]), xs, fit)


def case(name, x, y, w, note):
    x, y = np.asarray(x, float), np.asarray(y, float)
    w = np.ones_like(x) if w is None else np.asarray(w, float)
    ir = IsotonicRegression(y_min=0, y_max=1, increasing=True, out_of_bounds="clip").fit(x, y, sample_weight=w)
    q = np.unique(np.concatenate([[-0.1, 0.0, 1.0, 1.2], x, np.linspace(0, 1, 11), (x[:-1] + x[1:]) / 2]))
    pred = ir.predict(q)
    alt = scipy_path(x, y, w, q)
    assert np.allclose(pred, alt, rtol=0, atol=1e-12), name
    return {
        "name": name,
        "note": note,
        "x": x,
        "y": y,
        "sample_weight": w,
        "X_thresholds": ir.X_thresholds_,
        "y_thresholds": ir.y_thresholds_,
        "query_x": q,
        "predict": pred,
    }


def main() -> None:
    cases = [
        case(
            "hand_ties",
            [0.1, 0.2, 0.2, 0.3, 0.5, 0.5, 0.7, 0.9],
            [0, 1, 0, 0, 1, 0, 1, 1],
            None,
            "ties in x at 0.2 and 0.5; a violation at 0.2 -> 0.3",
        ),
        case("monotone_already", [0.05, 0.15, 0.4, 0.6, 0.85], [0, 0, 0, 1, 1], None, "no pooling needed"),
        case("decreasing", [0.1, 0.3, 0.5, 0.7], [1, 1, 0, 0], None, "collapses to a single level 0.5"),
    ]
    # IPCW-weighted case, SPEC 5.5.5 at one horizon tau: x = CIF_fill_raw(tau), y = 1{e <= tau, fill}
    r = rng_for("isotonic/ipcw")
    tau = 2.0
    x, e_all, c_all = [], [], []
    for _ in range(40):
        cif = round(r.next_f64() * 0.9, 4)
        e = round(-np.log(1 - r.next_f64()) * 3.0, 3)
        u = r.next_f64()
        cause = "censor" if u < 0.25 else ("fill" if u < 0.25 + 0.75 * (0.3 + 0.6 * cif) else "cancel")
        e_all.append(e)
        c_all.append(cause)
        x.append(cif)
    e_arr, c_arr = np.array(e_all), np.array(c_all)
    # censoring KM on these rows (event = censor), right-continuous, floored at 0.05
    order = np.unique(e_arr)
    g_vals, g = [], 1.0
    for t in order:
        at = np.sum(e_arr >= t)
        d = np.sum((e_arr == t) & (c_arr == "censor"))
        g *= 1 - d / at
        g_vals.append(g)
    g_vals = np.array(g_vals)

    def G(t):
        i = np.searchsorted(order, t, side="right") - 1
        return 1.0 if i < 0 else float(g_vals[i])

    keep = ~((e_arr <= tau) & (c_arr == "censor"))
    yy = ((e_arr <= tau) & (c_arr == "fill")).astype(float)
    ww = np.array([1.0 / max(G(min(ei, tau)), 0.05) for ei in e_arr])
    ipcw = case(
        "ipcw_weighted_tau2s",
        np.array(x)[keep],
        yy[keep],
        ww[keep],
        "SPEC 5.5.5 weights 1/max(G(min(e, tau)), 0.05); rows censored before tau excluded",
    )
    ipcw["source_rows"] = {"cif_fill_raw": x, "event_s": e_all, "cause": c_all, "tau_s": tau}
    cases.append(ipcw)
    raw = np.array([[0.1, 0.3, 0.25, 0.6, 1.02], [0.0, 0.0, 0.05, 0.04, 0.2], [-0.01, 0.2, 0.2, 0.9, 0.85]])
    mono = np.clip(np.maximum.accumulate(raw, axis=1), 0, 1)
    doc = header(["O-MOD-2"], "gen_isotonic.py", __doc__.strip(), ["numpy", "scipy", "scikit-learn"])
    doc["sklearn_version_note"] = f"generated with scikit-learn {sklearn.__version__}"
    doc["tolerance"] = {"predict": "abs 1e-12"}
    doc["cases"] = cases
    doc["monotone_across_horizons"] = {"cif_after_isotonic": raw, "expected": mono}
    print(write_fixture("isotonic.json", doc))


if __name__ == "__main__":
    main()
