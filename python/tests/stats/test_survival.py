"""O-STAT-2 (Kaplan-Meier, Aalen-Johansen, log-rank vs lifelines) and O-STAT-3 (Gray vs R cmprsk)."""

from __future__ import annotations

import numpy as np
import pytest
from lifelines import AalenJohansenFitter, KaplanMeierFitter
from lifelines.statistics import logrank_test

from queuetruth import stats


def test_o_stat_2_kaplan_meier_reference(reference) -> None:
    doc = reference("survival.json")
    for case in doc["kaplan_meier"]:
        km = stats.kaplan_meier(np.array(case["time"], float), np.array(case["event"], bool))
        got = km.at(np.array(case["query_times"], float), before=1.0)
        np.testing.assert_allclose(got, case["survival_at_query"], rtol=0, atol=1e-12, err_msg=case["name"])
        np.testing.assert_allclose(km.at(np.array(case["timeline"], float)), case["survival"], rtol=0, atol=1e-12)


def test_o_stat_2_aalen_johansen_reference(reference) -> None:
    doc = reference("survival.json")
    for case in doc["aalen_johansen"]:
        aj = stats.aalen_johansen(np.array(case["time"], float), np.array(case["cause"], int), causes=(1, 2))
        q = np.array(case["query_times"], float)
        for k, ref in case["causes"].items():
            got = aj[int(k)].at(q, before=0.0)
            np.testing.assert_allclose(got, ref["cif_at_query"], rtol=0, atol=1e-12, err_msg=f"{case['name']}:{k}")


def test_o_stat_2_logrank_reference(reference) -> None:
    doc = reference("survival.json")
    for case in doc["logrank"]:
        r = stats.logrank(
            np.array(case["time_a"], float),
            np.array(case["event_a"], bool),
            np.array(case["time_b"], float),
            np.array(case["event_b"], bool),
        )
        assert r.statistic == pytest.approx(case["lifelines"]["statistic"], rel=1e-9), case["name"]
        assert r.p_value == pytest.approx(case["lifelines"]["p_value"], rel=1e-9), case["name"]


def test_o_stat_2_lifelines_live_tie_free() -> None:
    rng = np.random.default_rng(stats.stable_seed(0, [b"test", b"survival"]))
    for _ in range(5):
        n = int(rng.integers(30, 200))
        t = rng.exponential(3.0, n)
        cause = rng.choice([0, 1, 2], n, p=[0.3, 0.4, 0.3])
        km = stats.kaplan_meier(t, cause > 0)
        kmf = KaplanMeierFitter().fit(t, cause > 0)
        tl = kmf.survival_function_.index.to_numpy()
        np.testing.assert_allclose(km.at(tl), kmf.survival_function_.iloc[:, 0].to_numpy(), atol=1e-12)
        aj = stats.aalen_johansen(t, cause)
        ajf = AalenJohansenFitter(calculate_variance=False).fit(t, cause, event_of_interest=1)
        tl = ajf.cumulative_density_.index.to_numpy()
        np.testing.assert_allclose(aj[1].at(tl), ajf.cumulative_density_.iloc[:, 0].to_numpy(), atol=1e-12)
        g = rng.integers(0, 2, n).astype(bool)
        r = stats.logrank(t[g], cause[g] > 0, t[~g], cause[~g] > 0)
        ref = logrank_test(t[g], t[~g], cause[g] > 0, cause[~g] > 0)
        assert r.statistic == pytest.approx(ref.test_statistic, rel=1e-9)
        assert r.p_value == pytest.approx(ref.p_value, rel=1e-9)


def test_kaplan_meier_step_function_conventions() -> None:
    km = stats.kaplan_meier(np.array([2.0, 2.0, 3.0, 5.0]), np.array([True, False, True, False]))
    assert list(km.time) == [0.0, 2.0, 3.0, 5.0]
    np.testing.assert_allclose(km.value, [1.0, 0.75, 0.375, 0.375])
    assert km.at(2.0) == 0.75 and km.left(2.0) == 1.0 and km.at(2.5) == 0.75
    with pytest.raises(ValueError):
        stats.kaplan_meier(np.array([-1.0]), np.array([True]))


@pytest.mark.requires_gray_reference
def test_o_stat_3_gray_matches_cmprsk(reference) -> None:
    doc = reference("gray_cmprsk.json")
    assert len(doc["cases"]) >= 6
    for case in doc["cases"]:
        t = np.array(case["input"]["time"], float)
        c = np.array(case["input"]["cause"], int)
        g = np.array(case["input"]["group"], int)
        for row in case["gray_tests"]:
            r = stats.gray_test(t, c, g, cause_of_interest=row["cause"])
            assert r.statistic == pytest.approx(row["stat"], rel=1e-9), (case["name"], row["cause"])
            assert r.p_value == pytest.approx(row["pv"], rel=1e-9, abs=1e-15), (case["name"], row["cause"])


@pytest.mark.requires_gray_reference
def test_o_stat_2_aalen_johansen_with_ties_matches_cmprsk(reference) -> None:
    doc = reference("gray_cmprsk.json")
    for case in doc["cases"]:
        t = np.array(case["input"]["time"], float)
        c = np.array(case["input"]["cause"], int)
        g = np.array(case["input"]["group"], int)
        grid = np.array(case["cif_grid"], float)
        for cur in case["cif_pooled"]:
            got = stats.aalen_johansen(t, c, causes=(cur["cause"],))[cur["cause"]].at(grid, before=0.0)
            np.testing.assert_allclose(got, cur["est"], atol=1e-12, err_msg=case["name"])
        for cur in case["cif_by_group"]:
            m = g == cur["group"]
            est = np.array([np.nan if v is None else v for v in cur["est"]], float)
            got = stats.aalen_johansen(t[m], c[m], causes=(cur["cause"],))[cur["cause"]].at(grid, before=0.0)
            ok = ~np.isnan(est)
            np.testing.assert_allclose(got[ok], est[ok], atol=1e-12, err_msg=case["name"])


def test_gray_invariances() -> None:
    rng = np.random.default_rng(9)
    t = np.round(rng.exponential(2.0, 150) * 4) / 4
    c = rng.choice([0, 1, 2, 3], 150)
    g = rng.integers(0, 3, 150)
    base = stats.gray_test(t, c, g)
    relabeled = stats.gray_test(t, c, np.array(["z", "a", "m"])[g])  # a different group is dropped from the score
    assert relabeled.statistic == pytest.approx(base.statistic, rel=1e-10)
    lumped = stats.gray_test(t, np.where(c == 3, 2, c), g)  # causes other than the one of interest are pooled
    assert lumped.statistic == pytest.approx(base.statistic, rel=1e-14)
    with pytest.raises(ValueError):
        stats.gray_test(t, c, np.zeros(150))
