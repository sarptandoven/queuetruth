"""`fit-hazard` speed-ups change no result (extension model-perf): every check is bit-for-bit equality.

- `TreeEnsemble` (block-parallel prediction through scikit-learn's own tree predictors and loss link) equals
  `HistGradientBoostingClassifier.predict_proba` for multiclass and binary fits, with missing values, any block
  split and any thread count.
- The hazards cached at `fit` equal the v0.1 prediction path (`predict_proba` in 20,000-row chunks); the isotonic
  layers fitted from them equal the v0.1 layers; `split_arrays` / `predict_split` equal predicting again, each row
  is predicted at most once, and train rows only when the predictions table is asked for.
- A fit that raises leaves the model unfitted (no stale cache from an earlier fit).
- The GBDT fit does not depend on the OpenMP thread count `fit_threads` picks; the policy itself is tested.
- The vectorised `zoo_comparison` equals the v0.1 per-row loop (`zoo_reference.py`) on randomised tables with
  duplicates, missing and null values, off-grid horizons and timestamps near the int64 limit.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.utils._openmp_helpers import _openmp_effective_n_threads

from model.hazard_synth import make_features
from model.legacy_model import v01_hazards
from model.zoo_reference import reference_zoo_comparison
from queuetruth.errors import QtError
from queuetruth.model import HazardModel, TreeEnsemble, cif_from_hazards, persist
from queuetruth.model import hazard as hazard_mod
from queuetruth.model._common import FILL, known_status, outcome_arrays
from queuetruth.model.card import fit_hazard, zoo_comparison
from queuetruth.model.scoring import censoring_km
from queuetruth.model.split import CALIB, TEST, TRAIN
from queuetruth.schemas.io import write_table

FAST = {"max_iter": 40, "min_samples_leaf": 40, "max_leaf_nodes": 15}


def _spec(**kw: object) -> dict:
    return {"features": {"path": "features.parquet"}, "gbdt": FAST, **kw}


@pytest.fixture(scope="module")
def features() -> pa.Table:
    return make_features(31, orders_per_day=300)


@pytest.fixture(scope="module")
def fitted(features: pa.Table) -> HazardModel:
    return HazardModel(_spec()).fit(features)


def _classifier(n_classes: int, seed: int = 0) -> tuple[HistGradientBoostingClassifier, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(3000, 6))
    x[rng.random(x.shape) < 0.05] = np.nan  # missing values take the learned missing-value branch
    score = x[:, 0] - 0.5 * np.nan_to_num(x[:, 1]) + rng.normal(scale=0.5, size=len(x))
    y = np.digitize(score, [-0.5, 0.7][: n_classes - 1])
    est = HistGradientBoostingClassifier(max_iter=25, max_leaf_nodes=11, random_state=0, early_stopping=False)
    return est.fit(x, y), x


@pytest.mark.parametrize("n_classes", [2, 3])
def test_tree_ensemble_equals_sklearn_predict_proba(n_classes: int) -> None:
    est, x = _classifier(n_classes)
    ens = TreeEnsemble.from_sklearn(est)
    assert ens.n_trees_per_iteration == (1 if n_classes == 2 else n_classes)
    rng = np.random.default_rng(1)
    big = rng.normal(size=(persist.BLOCK_ROWS * 2 + 37, x.shape[1]))  # three blocks, the last one short
    big[rng.random(big.shape) < 0.1] = np.nan
    for data in (x, big, x[:1]):
        want = est.predict_proba(data)
        for threads in (1, 3):
            got = ens.predict_proba(data, n_threads=threads)
            assert got.shape == want.shape and got.tobytes() == want.tobytes()
    assert ens.predict_proba(np.zeros((0, x.shape[1]))).shape == (0, n_classes)
    with pytest.raises(Exception) as err:
        ens.predict_proba(np.zeros((3, x.shape[1] + 1)))
    assert getattr(err.value, "code", None) == "QT-INPUT-002"
    # The array form round-trips every node field and the predictions.
    back = TreeEnsemble.from_arrays([int(k) for k in est.classes_], ens.to_arrays(), len(ens.trees), ens.n_features)
    for a, b in zip(ens.trees, back.trees, strict=True):
        assert a.tobytes() == b.tobytes()
    assert back.predict_proba(big).tobytes() == est.predict_proba(big).tobytes()


def test_tree_ensemble_refuses_categorical_fits() -> None:
    rng = np.random.default_rng(2)
    x = np.column_stack([rng.integers(0, 4, 500), rng.normal(size=500)]).astype(float)
    y = (x[:, 0] > 1).astype(int)
    est = HistGradientBoostingClassifier(max_iter=5, categorical_features=[0], random_state=0).fit(x, y)
    with pytest.raises(Exception) as err:
        TreeEnsemble.from_sklearn(est)
    assert getattr(err.value, "code", None) == "QT-ENGINE-001"


def test_cached_hazards_equal_v01_prediction_and_isotonic_layers(fitted: HazardModel) -> None:
    sf, res = fitted.split_features, fitted.split_result
    assert sf is not None and res is not None
    x = fitted.matrix(sf)
    want_f, want_c = v01_hazards(fitted, x)
    cached = fitted.split_arrays()
    assert cached["h_fill"].tobytes() == want_f.tobytes() and cached["h_cancel"].tobytes() == want_c.tobytes()
    # v0.1 fitted the isotonic layers from a fresh prediction of the calib rows: same thresholds bit for bit.
    label = res.label[res.keep]
    calib = np.flatnonzero(label == CALIB)
    e, c = outcome_arrays(sf)
    hf, hc = v01_hazards(fitted, x[calib])
    cif_raw, _, _ = cif_from_hazards(hf, hc)
    g = censoring_km(e[calib], c[calib])
    for j, tau in enumerate(fitted.grid_ns):
        use = ~known_status(e[calib], c[calib], int(tau))[2]
        y = ((c[calib] == FILL) & (e[calib] <= int(tau)))[use].astype(np.float64)
        w = g.weight_left(np.minimum(e[calib][use], int(tau)))
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
        iso.fit(cif_raw[use, j], y, sample_weight=w)
        mine = fitted.isotonic[j]
        assert mine is not None
        assert mine.X_thresholds_.tobytes() == iso.X_thresholds_.tobytes()
        assert mine.y_thresholds_.tobytes() == iso.y_thresholds_.tobytes()


def test_split_arrays_and_predict_split_equal_fresh_prediction(fitted: HazardModel) -> None:
    sf = fitted.split_features
    assert sf is not None
    mask = np.random.default_rng(3).random(sf.num_rows) < 0.4
    fresh = fitted.predict_arrays(sf.filter(pa.array(mask)))
    cached = fitted.split_arrays(mask)
    assert set(fresh) == set(cached)
    for k in fresh:
        assert fresh[k].tobytes() == cached[k].tobytes(), k
    assert fitted.predict_split().equals(fitted.predict(sf))
    loaded = HazardModel(_spec())
    for call in (loaded.split_arrays, loaded.predict_split):
        with pytest.raises(Exception) as err:
            call()
        assert getattr(err.value, "code", None) == "QT-USAGE-001"


def _counting_hazards(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Records the number of rows of every `HazardModel.hazards` call."""
    calls: list[int] = []
    real = HazardModel.hazards

    def hazards(self: HazardModel, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        calls.append(x.shape[0])
        return real(self, x)

    monkeypatch.setattr(HazardModel, "hazards", hazards)
    return calls


def test_each_row_is_predicted_once_and_train_rows_only_on_demand(
    features: pa.Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _counting_hazards(monkeypatch)
    m = HazardModel(_spec()).fit(features)
    assert m.split_result is not None and m.split_features is not None
    label = m.split_result.label[m.split_result.keep]
    train, scored = label == TRAIN, (label == CALIB) | (label == TEST)
    assert train.any() and calls == [int(scored.sum())]  # fit: the calib and test rows, once
    mask = np.random.default_rng(5).random(len(label)) < 0.5  # mixes cached and uncached (train) rows
    fresh = m.predict_arrays(m.split_features.filter(pa.array(mask)))
    calls.clear()
    cached = m.split_arrays(mask)
    assert calls == [int((mask & train).sum())]  # only the train rows not predicted yet
    assert all(fresh[k].tobytes() == cached[k].tobytes() for k in fresh)
    calls.clear()
    assert m.predict_split().equals(m.predict(m.split_features))
    assert calls[0] == int((train & ~mask).sum())  # predict_split: the remaining train rows (then predict's own)
    calls.clear()
    m.split_arrays(), m.predict_split()
    assert calls == []
    with pytest.raises(QtError) as err:
        m.split_arrays(mask[:-1])
    assert err.value.code == "QT-USAGE-001"


def test_fit_hazard_without_out_never_predicts_train_rows(
    features: pa.Table, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    feat = tmp_path / "features.parquet"
    write_table(features, feat)
    spec = {"features": {"path": str(feat)}, "gbdt": FAST, "bootstrap_b": 20}
    m = HazardModel(spec).fit(features)
    assert m.split_result is not None
    label = m.split_result.label[m.split_result.keep]
    assert (label == TRAIN).any()
    calls = _counting_hazards(monkeypatch)
    fit_hazard(spec)
    assert calls == [int(((label == CALIB) | (label == TEST)).sum())]


def test_a_failed_fit_leaves_the_model_unfitted(features: pa.Table) -> None:
    m = HazardModel(_spec()).fit(features)
    assert m.estimator is not None and m.split_features is not None
    no_fill = features.set_column(
        features.schema.get_field_index("cause"), "cause", pa.array(["cancel"] * features.num_rows).dictionary_encode()
    )
    with pytest.raises(QtError):
        m.fit(no_fill)
    assert m.estimator is None and m.sklearn_estimator is None and m.split_features is None and m.isotonic == []
    for call in (m.split_arrays, m.predict_split):
        with pytest.raises(QtError) as err:
            call()
        assert err.value.code == "QT-USAGE-001"


def test_fit_does_not_depend_on_fit_threads(features: pa.Table, fitted: HazardModel, tmp_path: Path) -> None:
    for n in (1, 4):
        m = HazardModel(_spec()).fit(features, threads=n)
        assert m.fit_threads_used == n and m.sklearn_estimator is not None
        # the limit reached scikit-learn's OpenMP code. scikit-learn calls _openmp_effective_n_threads() with no
        # argument, which takes the OpenMP limit (n, set by fit) and caps it at the physical cores the process may
        # use (CI runners have 2 to 4), so the expected count is that same call under the same limit
        with hazard_mod._openmp_threads(n):
            expected = _openmp_effective_n_threads()
        assert m.sklearn_estimator._bin_mapper.n_threads == expected <= n
        meta_a, arr_a = fitted.state()
        meta_b, arr_b = m.state()
        assert meta_a == meta_b and set(arr_a) == set(arr_b)
        assert all(arr_a[k].tobytes() == arr_b[k].tobytes() for k in arr_a)
        m.save(str(tmp_path / f"t{n}"))
    assert (tmp_path / "t1" / "model.npz").read_bytes() == (tmp_path / "t4" / "model.npz").read_bytes()


def test_fit_threads_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hazard_mod, "effective_threads", lambda: int(os.environ.get("OMP_NUM_THREADS", "8")))
    monkeypatch.setattr(hazard_mod, "allotted_cpus", lambda: 8)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    for load, want in ((0.0, 8), (2.4, 5), (7.9, 1), (250.0, 1)):
        monkeypatch.setattr(os, "getloadavg", lambda load=load: (load, load, load))
        assert hazard_mod.fit_threads() == want
        assert hazard_mod.fit_threads(3) == 3  # an explicit count wins over the load
    monkeypatch.setenv("OMP_NUM_THREADS", "3")
    assert hazard_mod.fit_threads() == 3  # the OpenMP runtime's own count, whatever the load
    monkeypatch.delenv("OMP_NUM_THREADS")
    # Confined to fewer CPUs than the host (affinity, cgroup quota): the host-wide load says nothing about them.
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(hazard_mod, "effective_threads", lambda: 4)
    monkeypatch.setattr(hazard_mod, "allotted_cpus", lambda: 4)
    assert hazard_mod.fit_threads() == 4
    monkeypatch.setattr(hazard_mod, "allotted_cpus", lambda: 64)
    assert hazard_mod.fit_threads() == 1  # the whole host and it is busy

    def no_load() -> tuple[float, float, float]:
        raise OSError("no load average")

    monkeypatch.setattr(os, "getloadavg", no_load)
    assert hazard_mod.fit_threads() == 4
    monkeypatch.delattr(os, "getloadavg")  # a platform without it
    assert hazard_mod.fit_threads() == 4
    for bad in (0, -2, True, 2.0):
        with pytest.raises(QtError) as err:
            hazard_mod.fit_threads(bad)  # type: ignore[arg-type]
        assert err.value.code == "QT-CONFIG-001" and err.value.details["field"] == "threads"


# ---- zoo_comparison: vectorised vs the v0.1 loop -----------------------------------------------------------------

GRID = np.array([1_000_000_000, 5_000_000_000, 10_000_000_000])
TS = pa.timestamp("ns", tz="UTC")


def _zoo_world(seed: int, *, n_test: int = 60, n_models: int = 3, huge: bool = False) -> tuple:
    rng = np.random.default_rng(seed)
    refs = [f"s{i}" for i in range(n_test)]
    refs[5] = refs[4]  # a repeated order_ref: the last landmark-0 shadow row counts
    lm = np.where(rng.random(n_test) < 0.15, 1_000_000_000, 0)
    pop = np.where(rng.random(n_test) < 0.1, "live", "shadows")
    test_tbl = pa.table(
        {
            "order_ref": pa.array(refs, pa.utf8()),
            "landmark_ns": pa.array(lm, pa.int64()),
            "population": pa.array(pop.tolist()).dictionary_encode(),
        }
    )
    e = rng.integers(0, 20_000_000_000, n_test)
    c = rng.integers(0, 3, n_test).astype(np.int8)
    cif = np.sort(rng.random((n_test, len(GRID))), axis=1)
    test = {"table": test_tbl, "e": e, "c": c, "cif_fill": cif, "g": censoring_km(e, c)}
    base = (1 << 62) + (1 << 61) if huge else 1_700_000_000_000_000_000
    rows: dict[str, list] = {
        k: [] for k in ("model_id", "shadow_id", "horizon_ns", "p_fill", "truth_filled", "ts_live")
    }
    horizons = [*GRID.tolist(), 2_000_000_000]  # one off-grid horizon
    sids = [*refs, "unknown_shadow", None]
    for m in range(n_models):
        for _ in range(n_test * 3):
            rows["model_id"].append(f"model_{m}" if m < n_models - 1 or rng.random() < 0.7 else "zz_partial")
            rows["shadow_id"].append(sids[int(rng.integers(0, len(sids)))])
            rows["horizon_ns"].append(int(horizons[int(rng.integers(0, len(horizons)))]))
            u = rng.random()
            rows["p_fill"].append(None if u < 0.03 else (float("nan") if u < 0.06 else float(rng.random())))
            v = rng.random()
            rows["truth_filled"].append(None if v < 0.05 else bool(v < 0.5))
            rows["ts_live"].append(base + int(rng.integers(0, 1_000_000_000)))
    zoo = pa.table(
        {
            "model_id": pa.array(rows["model_id"], pa.utf8()),
            "shadow_id": pa.array(rows["shadow_id"], pa.utf8()),
            "horizon_ns": pa.array(rows["horizon_ns"], pa.int64()),
            "p_fill": pa.array(rows["p_fill"], pa.float64()),
            "truth_filled": pa.array(rows["truth_filled"], pa.bool_()),
            "ts_live": pa.array(rows["ts_live"], pa.int64()).cast(TS),
        }
    )
    life_ids = [*refs, refs[0], None]  # refs[0] twice (the first row counts) and a null id
    n_life = len(life_ids)
    adm = []
    for _ in range(n_life):
        r = rng.random()
        adm.append(None if r < 0.4 else base + int(rng.integers(0, 12_000_000_000)))
    never = [bool(rng.random() < 0.85) for _ in range(n_life)]
    never[-2] = not never[0]
    if huge:
        adm[1] = (1 << 63) - 1  # INT64_MAX: ts_live + h exceeds int64 here
    life = pa.table(
        {
            "shadow_id": pa.array(life_ids[:5] + life_ids[6:], pa.utf8()),  # s5 has no planned-life row
            "ts_admin_end": pa.array(adm[:5] + adm[6:], pa.int64()).cast(TS),
            "never_cancel": pa.array(never[:5] + never[6:], pa.bool_()),
        }
    )
    return zoo, life, test


class _Model:
    grid_ns = GRID


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("huge", [False, True])
def test_zoo_comparison_equals_v01_loop(seed: int, huge: bool) -> None:
    zoo, life, test = _zoo_world(seed, huge=huge)
    notes_old: list[str] = []
    notes_new: list[str] = []
    old = reference_zoo_comparison(_Model(), "hazard", zoo, test, planned_life=life, warnings=notes_old)
    new = zoo_comparison(_Model(), "hazard", zoo, test, planned_life=life, warnings=notes_new)  # type: ignore[arg-type]
    assert old and new == old and list(new) == list(old)
    assert notes_new == notes_old


def test_zoo_comparison_edge_tables_equal_v01_loop() -> None:
    zoo, life, test = _zoo_world(9)
    cases = {
        "empty zoo": zoo.slice(0, 0),
        "nothing comparable": zoo.set_column(4, "truth_filled", pa.nulls(zoo.num_rows, pa.bool_())),
        "one model": zoo.filter(pa.compute.equal(zoo.column("model_id"), "model_0")),
        "model id equal to the hazard key": zoo.set_column(
            0, "model_id", pa.array(["hazard"] * zoo.num_rows, pa.utf8())
        ),
        "aborted rows": zoo.append_column(
            "status", pa.array([None if i % 3 else "hbt_312_abort" for i in range(zoo.num_rows)], pa.utf8())
        ),
    }
    for name, z in cases.items():
        n_old: list[str] = []
        n_new: list[str] = []
        old = reference_zoo_comparison(_Model(), "hazard", z, test, planned_life=life, warnings=n_old)
        new = zoo_comparison(_Model(), "hazard", z, test, planned_life=life, warnings=n_new)  # type: ignore[arg-type]
        assert new == old and list(new) == list(old), name
        assert n_new == n_old, name
    assert zoo_comparison(_Model(), "hazard", zoo, {}, planned_life=life) == {}  # type: ignore[arg-type]


def test_negative_zero_thresholds_are_stored_as_positive_zero() -> None:
    """scikit-learn can store a split threshold of -0.0 or +0.0 for the same fit (`np.unique` keeps either signed
    zero); `x <= -0.0` and `x <= +0.0` agree for every x, so the ensemble writes +0.0 and predicts the same."""
    est, x = _classifier(3)
    split = next((it, k, int(np.flatnonzero(p.nodes["is_leaf"] == 0)[0])) for it, ps in enumerate(est._predictors)
                 for k, p in enumerate(ps))  # fmt: skip
    it, k, node = split
    est._predictors[it][k].nodes["num_threshold"][node] = -0.0
    probe = np.vstack([x, np.full((4, x.shape[1]), -0.0), np.zeros((4, x.shape[1])), np.full((1, x.shape[1]), np.nan)])
    want = est.predict_proba(probe)
    ens = TreeEnsemble.from_sklearn(est)
    t = ens.trees[it * ens.n_trees_per_iteration + k]["num_threshold"][node]
    assert t == 0.0 and not np.signbit(t)
    assert ens.predict_proba(probe).tobytes() == want.tobytes()
    assert np.signbit(est._predictors[it][k].nodes["num_threshold"][node])  # the estimator itself is not modified
    est._predictors[it][k].nodes["num_threshold"][node] = 0.0
    assert est.predict_proba(probe).tobytes() == want.tobytes()
