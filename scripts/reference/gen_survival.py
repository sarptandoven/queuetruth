"""Kaplan-Meier, Aalen-Johansen and log-rank reference values (SPEC 4.3 / 5.7.5, oracle O-STAT-2)
-> tests/fixtures/reference/survival.json.

- KM: `lifelines.KaplanMeierFitter().fit(time, event)`; `timeline`/`survival` are lifelines' own step points
  (timeline starts at 0 and contains every distinct observed time, censor-only times included). `query_times` /
  `survival_at_query` are the right-continuous step values at arbitrary times (value at t includes the jump at t),
  which is representation-independent. Datasets with and without ties.
- AJ: `lifelines.AalenJohansenFitter(calculate_variance=False).fit(time, cause, event_of_interest=k)` for k in
  (1, 2) on TIE-FREE data only (lifelines jitters tied times at random); cause 0 = censored. Same two views.
  For AJ with ties use `gray_cmprsk.json` (`cif_pooled`, `cif_by_group`, from cmprsk::cuminc).
- Log-rank: `lifelines.statistics.logrank_test(time_a, time_b, event_a, event_b)` (statistic, p), cross-checked
  against `statsmodels.duration.survfunc.survdiff` (asserted equal to 1e-10).
- Every KM/AJ value is also cross-checked by a plain product-limit / AJ implementation in this script (1e-12).
"""

from __future__ import annotations

import math

import numpy as np
from _common import header, rng_for, write_fixture
from lifelines import AalenJohansenFitter, KaplanMeierFitter
from lifelines.statistics import logrank_test
from statsmodels.duration.survfunc import survdiff


def step_at(times: np.ndarray, values: np.ndarray, q: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(times, q, side="right") - 1
    return np.where(idx >= 0, values[np.clip(idx, 0, None)], np.nan)


def plain_km(time: np.ndarray, event: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ts = np.unique(time)
    s, out = 1.0, []
    for t in ts:
        n = np.sum(time >= t)
        d = np.sum((time == t) & event)
        s *= 1.0 - d / n
        out.append(s)
    return np.concatenate([[0.0], ts]), np.concatenate([[1.0], out])


def plain_aj(time: np.ndarray, cause: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    ts = np.unique(time)
    s_prev, f, out = 1.0, 0.0, []
    for t in ts:
        n = np.sum(time >= t)
        dk = np.sum((time == t) & (cause == k))
        dall = np.sum((time == t) & (cause != 0))
        f += s_prev * dk / n
        s_prev *= 1.0 - dall / n
        out.append(f)
    return np.concatenate([[0.0], ts]), np.concatenate([[0.0], out])


def gen_times(name: str, n: int, tie_free: bool, cens_frac: float, n_causes: int = 1):
    r = rng_for(name)
    time, label = [], []
    for _ in range(n):
        if tie_free:
            t = -math.log(1.0 - r.next_f64()) * 10.0
        else:
            t = float(1 + r.range_inclusive(0, 14))
        u = r.next_f64()
        if u < cens_frac:
            lab = 0
        else:
            lab = 1 + min(int((u - cens_frac) / (1 - cens_frac) * n_causes), n_causes - 1)
        time.append(t)
        label.append(lab)
    t = np.array(time)
    if tie_free:
        assert len(np.unique(t)) == len(t)
    return t, np.array(label)


def query_grid(t: np.ndarray) -> np.ndarray:
    hi = float(t.max())
    return np.unique(np.concatenate([[0.0, 0.5], np.linspace(0, hi * 1.1, 23), np.unique(t)[::3]]))


def km_case(name: str, time: np.ndarray, event: np.ndarray, note: str) -> dict:
    kmf = KaplanMeierFitter().fit(time, event_observed=event)
    tl = kmf.survival_function_.index.to_numpy(dtype=float)
    sv = kmf.survival_function_.iloc[:, 0].to_numpy(dtype=float)
    pt, pv = plain_km(time, event.astype(bool))
    assert np.array_equal(tl, pt) and np.allclose(sv, pv, rtol=0, atol=1e-12), name
    q = query_grid(time)
    return {
        "name": name,
        "note": note,
        "time": time,
        "event": event.astype(bool),
        "timeline": tl,
        "survival": sv,
        "query_times": q,
        "survival_at_query": step_at(tl, sv, q),
    }


def aj_case(name: str, time: np.ndarray, cause: np.ndarray, note: str) -> dict:
    out = {"name": name, "note": note, "time": time, "cause": cause, "causes": {}}
    q = query_grid(time)
    out["query_times"] = q
    for k in (1, 2):
        ajf = AalenJohansenFitter(calculate_variance=False).fit(time, cause, event_of_interest=k)
        cd = ajf.cumulative_density_
        tl = cd.index.to_numpy(dtype=float)
        v = cd.iloc[:, 0].to_numpy(dtype=float)
        pt, pv = plain_aj(time, cause, k)
        assert np.array_equal(tl, pt) and np.allclose(v, pv, rtol=0, atol=1e-12), (name, k)
        out["causes"][str(k)] = {"timeline": tl, "cif": v, "cif_at_query": step_at(tl, v, q)}
    return out


def logrank_case(name: str, ta, ea, tb, eb, note: str) -> dict:
    res = logrank_test(ta, tb, event_observed_A=ea, event_observed_B=eb)
    chisq, pv = survdiff(
        np.concatenate([ta, tb]),
        np.concatenate([ea, eb]).astype(float),
        np.concatenate([np.zeros(len(ta)), np.ones(len(tb))]),
    )
    assert math.isclose(res.test_statistic, chisq, rel_tol=1e-10), name
    assert math.isclose(res.p_value, pv, rel_tol=1e-9), name
    return {
        "name": name,
        "note": note,
        "time_a": ta,
        "event_a": ea.astype(bool),
        "time_b": tb,
        "event_b": eb.astype(bool),
        "lifelines": {"statistic": float(res.test_statistic), "p_value": float(res.p_value)},
        "statsmodels_survdiff": {"statistic": float(chisq), "p_value": float(pv)},
    }


def main() -> None:
    km = []
    km.append(
        km_case(
            "hand_n10",
            np.array([1, 2, 2, 3, 4, 4, 4, 6, 8, 9], float),
            np.array([1, 1, 0, 1, 1, 0, 1, 0, 1, 0]),
            "hand-written; event and censor tied at t = 2 and 4",
        )
    )
    km.append(km_case("all_events_n6", np.array([5, 1, 3, 3, 2, 8], float), np.ones(6, int), "no censoring"))
    km.append(
        km_case(
            "last_obs_event_n5",
            np.array([1, 2, 3, 4, 5], float),
            np.array([0, 1, 0, 1, 1]),
            "S reaches 0 at the last time",
        )
    )
    for i, (n, tf, cf) in enumerate([(40, False, 0.3), (120, False, 0.5), (60, True, 0.25), (200, True, 0.6)]):
        t, lab = gen_times(f"km/{i}", n, tf, cf)
        km.append(
            km_case(
                f"seeded_{i}_n{n}_{'tiefree' if tf else 'ties'}",
                t,
                (lab > 0).astype(int),
                f"seeded; censoring fraction about {cf}",
            )
        )
    aj = []
    aj.append(
        aj_case(
            "hand_n8_tiefree",
            np.array([0.5, 1.2, 1.9, 2.4, 3.1, 3.3, 4.0, 5.5]),
            np.array([1, 2, 0, 1, 1, 0, 2, 1]),
            "hand-written, tie-free",
        )
    )
    for i, (n, cf) in enumerate([(30, 0.2), (100, 0.4), (250, 0.3)]):
        t, lab = gen_times(f"aj/{i}", n, True, cf, n_causes=2)
        aj.append(aj_case(f"seeded_{i}_n{n}_tiefree", t, lab, f"seeded tie-free; censoring fraction about {cf}"))
    lr = []
    lr.append(
        logrank_case(
            "hand_ties",
            np.array([1, 2, 2, 3, 5, 6], float),
            np.array([1, 1, 0, 1, 1, 0]),
            np.array([2, 3, 3, 4, 7, 8, 9], float),
            np.array([1, 0, 1, 1, 1, 0, 1]),
            "hand-written, ties across arms",
        )
    )
    for i, (na, nb, tf) in enumerate([(30, 35, False), (80, 60, False), (50, 50, True), (150, 140, False)]):
        ta, la = gen_times(f"lr/{i}/a", na, tf, 0.3)
        tb, lb = gen_times(f"lr/{i}/b", nb, tf, 0.3)
        if i % 2 == 1:
            tb = tb * 1.3 if tf else tb + 2.0  # make arm b slower
        lr.append(
            logrank_case(
                f"seeded_{i}",
                ta,
                (la > 0).astype(int),
                tb,
                (lb > 0).astype(int),
                "seeded" + (" tie-free" if tf else " with ties"),
            )
        )
    doc = header(["O-STAT-2"], "gen_survival.py", __doc__.strip(), ["numpy", "lifelines", "statsmodels", "pandas"])
    doc["tolerance"] = {"kaplan_meier": "abs 1e-12", "aalen_johansen": "abs 1e-12", "logrank": "abs/rel 1e-9"}
    doc["kaplan_meier"] = km
    doc["aalen_johansen"] = aj
    doc["logrank"] = lr
    print(write_fixture("survival.json", doc))


if __name__ == "__main__":
    main()
