"""The held-out driver `repro/heldout.py` on hand-made inputs (needs pyarrow and pandas; the harness from the
queuetruth checkout; the fill-model swap also needs nautilus_trader)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("pandas")

REPRO = Path(__file__).resolve().parents[1] / "repro" / "heldout.py"
HARNESS = Path(__file__).resolve().parents[3] / "scripts" / "nt_crosscheck" / "run.py"
if not HARNESS.exists():
    pytest.skip("the queuetruth checkout's NautilusTrader harness is not present", allow_module_level=True)

spec = importlib.util.spec_from_file_location("qt_heldout_driver", REPRO)
heldout = importlib.util.module_from_spec(spec)
sys.modules["qt_heldout_driver"] = heldout
spec.loader.exec_module(heldout)

S = 1_000_000_000
H = [100_000_000, S, 10 * S, 60 * S]
T_MODEL = "nt_prob_touch[mode=faithful;p=1]"


def test_test_dates(touch_doc):
    assert heldout.test_dates(touch_doc) == ["2020-01-30"]
    d = dict(touch_doc, test={"dates": None, "blocks": {"index": [4], "n_blocks": 5}})
    with pytest.raises(SystemExit, match="blocks only"):
        heldout.test_dates(d)


def test_window_text():
    assert heldout._window({"dates": ["2019-12-30"], "blocks": {"index": [0, 1, 2, 3], "n_blocks": 5}}) == (
        "2019-12-30 blocks 0-3/5"
    )
    assert heldout._window({"dates": ["2020-01-30"], "blocks": None}) == "2020-01-30"
    assert heldout._window(None) == "all data"


FITTED = "nt_prob_touch[mode=faithful;p=0.28662597114317423]"
#: the zoo's per-order prediction for the fitted model, at 0.1 / 1 / 10 / 60 s (c: the replica says a certain fill
#: from 10 s, which the calibrated run never makes: a deterministic mismatch)
ZOO_FIT = {"a": [0, 0.5, 0.5, 0.5], "b": [0, 0, 0, 0.75], "c": [0, 0, 1, 1], "d": [0, 0, 0, 0]}


def _work(tmp_path: Path, tl: int = 1_000 * S) -> Path:
    """4 orders; exact truth: order a fills at 0.5 s, b at 20 s, c and d never. Default NT fills a at 0.2 s, c at
    5 s, and d after its cancel time; the calibrated model (2 seeds) fills a in seed 0 only, and b in both at 30 s."""
    w = tmp_path / "work"
    (w / "zoo").mkdir(parents=True)
    (w / "nt-9.9.9").mkdir()
    (w / "held_out").mkdir()
    ids = ["a", "b", "c", "d"]
    pq.write_table(
        pa.Table.from_pylist(
            [{"shadow_id": s, "price": 100, "crosses_at_live": False, "ts_live": tl, "instrument_id": 1} for s in ids]
        ),
        w / "shadows.parquet",
    )
    truth_ts = {"a": 0.5 * S, "b": 20 * S, "c": None, "d": None}
    zoo_fill = {"a": 0.2 * S, "b": None, "c": 5 * S, "d": None}
    rows = []
    for s in ids:
        for h in H:
            t, z = truth_ts[s], zoo_fill[s]
            rows.append(
                {
                    "model_id": T_MODEL,
                    "shadow_id": s,
                    "horizon_ns": h,
                    "truth_filled": t is not None and t <= h,
                    "p_fill": 1.0 if z is not None and z <= h else 0.0,
                }
            )
    pq.write_table(pa.Table.from_pylist(rows), w / "zoo" / "zoo_predictions.parquet")
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"model_id": FITTED, "shadow_id": sid, "horizon_ns": h, "p_fill": float(ZOO_FIT[sid][i])}
                for sid in ids
                for i, h in enumerate(H)
            ]
        ),
        w / "held_out" / "zoo_predictions.parquet",
    )

    def out(config, seed, s, dt):
        return {
            "config": config,
            "seed_index": seed,
            "shadow_id": s,
            "ts_live": tl,
            "first_fill_ts": None if dt is None else int(tl + dt),
        }

    nt = [
        out("nt_default", 0, "a", 0.2 * S),
        out("nt_default", 0, "b", None),
        out("nt_default", 0, "c", 5 * S),
        out("nt_default", 0, "d", 61 * S),  # after ts_live + 60 s + 1 ns: the harness's cancel did not happen
        out("qt_calibrated", 0, "a", 0.2 * S),
        out("qt_calibrated", 1, "a", None),
        out("qt_calibrated", 0, "b", 30 * S),
        out("qt_calibrated", 1, "b", 30 * S),
        out("qt_calibrated", 0, "c", None),
        out("qt_calibrated", 1, "c", None),
    ]  # d is missing for the calibrated config (as if not submitted): unfilled
    pq.write_table(pa.Table.from_pylist(nt), w / "nt-9.9.9" / "heldout_outcomes.parquet")
    return w


def test_score_numbers(tmp_path, touch_doc):
    w = _work(tmp_path)
    rep = heldout.score(w, "nt-9.9.9", touch_doc, zoo_held_out=w / "held_out" / "zoo_predictions.parquet")
    assert rep["n_shadows"] == 4 and rep["n_submitted"] == 4 and rep["seeds"] == 2
    assert rep["fills_after_cancel_time"] == {"nt_default": 1, "qt_calibrated": 0}
    assert rep["zoo_default_model"] == T_MODEL and rep["zoo_fitted_model"] == FITTED
    r1 = rep["horizons"]["1s"]
    assert r1["exact_rate"] == 0.25  # a
    assert r1["default"]["fill_rate"] == 0.25  # a
    assert r1["default"]["bias_pp"] == 0.0
    assert r1["default"]["agree_with_zoo_p1"] == 1.0
    assert r1["calibrated"]["fill_rate"] == pytest.approx(0.125)  # a in one seed of two
    assert r1["calibrated"]["bias_pp"] == pytest.approx(-12.5)
    assert r1["calibrated"]["bias_pp_per_seed"] == {"min": -25.0, "max": 0.0, "sd": pytest.approx(17.6776695)}
    assert r1["calibrated"]["abs_bias_pp_single_run_mean"] == pytest.approx(12.5)
    assert r1["calibrated"]["brier_seed_mean"] == pytest.approx(0.25 / 4)  # (0.5 - 1)^2 on a
    assert r1["calibrated"]["brier_single_seed_mean"] == pytest.approx(0.125)  # a wrong in one of 8 cells
    pr = r1["paired"]
    # the default is exact here and the calibrated model underfills: every paired test says "worse"
    assert pr["abs_bias_reduction_pp"]["value"] == pytest.approx(-12.5)
    assert pr["abs_bias_reduction_single_run_pp"]["value"] == pytest.approx(-12.5)
    assert pr["brier_gain_seed_mean"]["value"] == pytest.approx(-0.0625)
    assert pr["brier_gain_single_run"]["value"] == pytest.approx(-0.125)
    for st in pr.values():
        assert st["ci"][0] <= st["value"] <= st["ci"][1]
    rp1 = r1["reproduction"]
    assert rp1["nt_minus_zoo_pp"]["value"] == pytest.approx(0.0)
    assert (rp1["deterministic_orders"], rp1["deterministic_mismatches"], rp1["random_orders"]) == (3, 0, 1)
    assert rp1["dispersion_ratio"] == 0.0
    r10 = rep["horizons"]["10s"]
    assert r10["default"]["fill_rate"] == 0.5 and r10["default"]["bias_pp"] == 25.0  # c filled, not in truth
    assert r10["reproduction"]["deterministic_mismatches"] == 1  # c
    r60 = rep["horizons"]["60s"]
    assert r60["exact_rate"] == 0.5  # a, b
    assert r60["calibrated"]["fill_rate"] == pytest.approx(0.375)  # (a: 0.5 + b: 1) / 4 orders
    assert r60["zoo_held_out"]["default_bias_pp"] == pytest.approx(0.9333333333)
    rp = r60["reproduction"]
    assert rp["nt_minus_zoo_pp"]["value"] == pytest.approx(100 * (0.25 - 1) / 4)  # b +0.25, c -1
    # random orders a (diff 0) and b (diff 0.25, variance 0.75 * 0.25 / 2 seeds)
    assert rp["dispersion_ratio"] == pytest.approx((0 + 0.0625 / (0.75 * 0.25 / 2)) / 2)
    for hz in rep["horizons"].values():
        lo, hi = hz["default"]["bias_ci_pp"]
        assert lo <= hz["default"]["bias_pp"] <= hi
    text = heldout.render(rep)
    assert "default 1, calibrated 0" in text and "| 1s | 25.00% |" in text
    assert json.loads(json.dumps(rep)) == rep


def test_score_without_zoo_fitted_predictions(tmp_path, touch_doc):
    rep = heldout.score(_work(tmp_path), "nt-9.9.9", touch_doc)
    assert rep["zoo_fitted_model"] is None
    assert all("reproduction" not in r for r in rep["horizons"].values())
    assert "| - |" in heldout.render(rep)


def test_fill_times_compared_in_exact_int64(tmp_path, touch_doc):
    """At epoch nanoseconds float64 is 256 ns coarse: a fill 100 ns after the 1 s boundary must not count at 1 s."""
    tl = 1_580_000_000_123_456_700
    assert float(tl + S + 100) <= float(tl + S)  # the trap: in float64 the late fill looks on time
    w = _work(tmp_path, tl=tl)
    t = pq.read_table(w / "nt-9.9.9" / "heldout_outcomes.parquet").to_pylist()
    for r in t:
        if r["config"] == "nt_default" and r["shadow_id"] == "b":
            r["first_fill_ts"] = tl + S + 100
    pq.write_table(pa.Table.from_pylist(t), w / "nt-9.9.9" / "heldout_outcomes.parquet")
    rep = heldout.score(w, "nt-9.9.9", touch_doc)
    assert rep["horizons"]["1s"]["default"]["fill_rate"] == 0.25  # a only; b is 100 ns late
    assert rep["horizons"]["10s"]["default"]["fill_rate"] == 0.75  # a, b, c
    assert rep["fills_after_cancel_time"]["nt_default"] == 1


def _bulk_work(tmp_path: Path, n: int, truth: list[bool], dflt: list[bool], cal: list[list[bool]]) -> Path:
    """`n` orders filled (at 0.5 s) per the given indicators: exact truth, the default, and each calibrated seed."""
    w = tmp_path / "bulk"
    (w / "zoo").mkdir(parents=True)
    (w / "nt-9.9.9").mkdir()
    tl = 1_000 * S
    ids = [f"o{i:04d}" for i in range(n)]
    pq.write_table(
        pa.Table.from_pylist(
            [{"shadow_id": s, "price": 100, "crosses_at_live": False, "ts_live": tl, "instrument_id": 1} for s in ids]
        ),
        w / "shadows.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "model_id": T_MODEL,
                    "shadow_id": s,
                    "horizon_ns": h,
                    "truth_filled": truth[i],
                    "p_fill": float(dflt[i]),
                }
                for i, s in enumerate(ids)
                for h in H
            ]
        ),
        w / "zoo" / "zoo_predictions.parquet",
    )
    rows = [
        {
            "config": "nt_default",
            "seed_index": 0,
            "shadow_id": s,
            "ts_live": tl,
            "first_fill_ts": tl + S // 2 if dflt[i] else None,
        }
        for i, s in enumerate(ids)
    ]
    rows += [
        {
            "config": "qt_calibrated",
            "seed_index": k,
            "shadow_id": s,
            "ts_live": tl,
            "first_fill_ts": tl + S // 2 if c[i] else None,
        }
        for k, c in enumerate(cal)
        for i, s in enumerate(ids)
    ]
    pq.write_table(pa.Table.from_pylist(rows), w / "nt-9.9.9" / "heldout_outcomes.parquet")
    return w


def test_paired_test_does_not_certify_an_overcorrection(tmp_path, touch_doc):
    """A p far below the fit removes most fills: the default overfills by 10 pp, the 'calibrated' model underfills by
    30 pp. Its fills are a subset of the default's, so 'default minus calibrated fills' is positive on every order
    and its CI excludes zero; the paired |bias| and Brier tests correctly say it is worse."""
    import numpy as np

    n = 400
    truth = [i < 200 for i in range(n)]  # 50%
    dflt = [i < 240 for i in range(n)]  # 60%: overfills by 10 pp
    cal = [[i < 80 for i in range(n)], [i < 70 for i in range(n)]]  # 20% and 17.5%: underfills by about 31 pp
    w = _bulk_work(tmp_path, n, truth, dflt, cal)
    r = heldout.score(w, "nt-9.9.9", touch_doc)["horizons"]["1s"]
    assert r["default"]["bias_pp"] == pytest.approx(10.0)
    assert r["calibrated"]["bias_pp"] == pytest.approx(-31.25)
    fd = np.array(dflt, float)
    pc = np.array(cal, float).mean(axis=0)
    assert (fd - pc >= 0).all() and (fd - pc).mean() > 0  # the old 'overfill cut': positive whatever p is
    for k in (
        "abs_bias_reduction_pp",
        "abs_bias_reduction_single_run_pp",
        "brier_gain_seed_mean",
        "brier_gain_single_run",
    ):
        assert r["paired"][k]["ci"][1] < 0, (k, r["paired"][k])


def test_paired_test_passes_a_real_improvement(tmp_path, touch_doc):
    """The same data with a calibrated model that removes the overfill (fills exactly the true orders)."""
    n = 400
    truth = [i < 200 for i in range(n)]
    dflt = [i < 240 for i in range(n)]
    w = _bulk_work(tmp_path, n, truth, dflt, [truth, truth])
    r = heldout.score(w, "nt-9.9.9", touch_doc)["horizons"]["1s"]
    assert r["paired"]["abs_bias_reduction_pp"]["value"] == pytest.approx(10.0)
    assert r["paired"]["abs_bias_reduction_pp"]["ci"][0] > 0
    assert r["paired"]["brier_gain_single_run"]["ci"][0] > 0


def test_fill_model_params():
    assert heldout.fill_model_params("FillModel(prob_fill_on_limit=0.28662597114317423, prob_slippage=0.0)") == {
        "class": "FillModel",
        "prob_fill_on_limit": 0.28662597114317423,
        "prob_slippage": 0.0,
    }
    two = (
        "DefaultFillModel { state: ProbabilisticFillState { prob_fill_on_limit: 1.0, prob_slippage: 0.0, "
        "random_seed: Some(55), rng: StdRng(ChaCha12Rng { ... }) } }"
    )
    assert heldout.fill_model_params(two) == {
        "class": "DefaultFillModel",
        "prob_fill_on_limit": 1.0,
        "prob_slippage": 0.0,
    }
    assert heldout.fill_model_params(None) is None


def test_harness_fill_model_swap(touch_doc):
    pytest.importorskip("nautilus_trader")
    import importlib

    import queuetruth_nautilus as qtn
    from queuetruth_nautilus import compat

    cal = qtn.from_document(touch_doc)
    mod_name, attr = compat.fill_model_path()
    mod = importlib.import_module(mod_name)
    original = getattr(mod, attr)
    p = cal.prob_fill_on_limit
    with heldout.harness_fill_model("qt_calibrated", cal, p) as built:
        factory = getattr(mod, attr)
        assert factory is not original
        obj = factory(prob_fill_on_limit=p, prob_slippage=0.0, random_seed=5)
        assert type(obj) is original
        assert getattr(mod, attr) is factory  # re-armed after building with the real class
        with pytest.raises(AssertionError, match="harness asked for p"):
            factory(prob_fill_on_limit=0.29, prob_slippage=0.0, random_seed=5)
        with pytest.raises(AssertionError, match="prob_slippage"):
            factory(prob_fill_on_limit=p, prob_slippage=0.5, random_seed=5)
    assert getattr(mod, attr) is original
    assert built == [obj]
    with pytest.raises(RuntimeError), heldout.harness_fill_model("nt_default", cal, 1.0) as built:
        d = getattr(mod, attr)(prob_fill_on_limit=1.0, prob_slippage=0.0, random_seed=1)
        assert type(d) is original
        raise RuntimeError("boom")
    assert getattr(mod, attr) is original  # restored on error


def test_harness_fill_model_refused_needs_force(queue_doc):
    pytest.importorskip("nautilus_trader")
    import importlib

    import queuetruth_nautilus as qtn
    from queuetruth_nautilus import compat

    cal = qtn.from_document(queue_doc)
    mod = importlib.import_module(compat.fill_model_path()[0])
    attr = compat.fill_model_path()[1]
    p = cal.prob_fill_on_limit
    with heldout.harness_fill_model("qt_calibrated", cal, p), pytest.raises(qtn.CalibrationRefused):
        getattr(mod, attr)(prob_fill_on_limit=p, prob_slippage=0.0, random_seed=1)
    with heldout.harness_fill_model("qt_calibrated", cal, p, force=True) as built, pytest.warns(qtn.CalibrationWarning):
        getattr(mod, attr)(prob_fill_on_limit=p, prob_slippage=0.0, random_seed=1)
    assert len(built) == 1
