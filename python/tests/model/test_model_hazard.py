"""`HazardModel`: O-MOD-2 (CIFs monotone in horizon, `CIF_fill + CIF_cancel <= 1`), prediction table contract,
save / load of `model.npz` with sha256 verification, privileged columns, and determinism across `OMP_NUM_THREADS`
(SPEC 7.2). The safe format and the legacy pickle path in depth: `test_model_persist.py`."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pytest

from model.hazard_synth import make_features
from queuetruth.errors import QtError
from queuetruth.model import HazardModel, cif_from_hazards, model_feature_names
from queuetruth.model.hazard import model_sha256, pickle_sha256
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.models import HazardModelCard

ROOT = Path(__file__).resolve().parents[3]
FAST = {"max_iter": 40, "min_samples_leaf": 40, "max_leaf_nodes": 15}


def _spec(**kw: object) -> dict:
    return {"features": {"path": "features.parquet"}, "gbdt": FAST, **kw}


@pytest.fixture(scope="module")
def features() -> pa.Table:
    return make_features(11, orders_per_day=250)


@pytest.fixture(scope="module")
def fitted(features: pa.Table) -> HazardModel:
    return HazardModel(_spec()).fit(features)


def test_o_mod_2_cifs_monotone_and_bounded(fitted: HazardModel) -> None:
    assert fitted.split_features is not None
    arr = fitted.predict_arrays(fitted.split_features)
    assert np.all(np.diff(arr["cif_fill_raw"], axis=1) >= -1e-15)
    rf, rc, rs = cif_from_hazards(arr["h_fill"], arr["h_cancel"])
    assert np.all(np.diff(rc, axis=1) >= -1e-15)
    assert np.all(rf + rc <= 1 + 1e-12)
    assert np.allclose(rf + rc + rs, 1.0)
    for name in ("cif_fill", "cif_cancel"):
        assert np.all(np.diff(arr[name], axis=1) >= 0), name
        assert np.all((arr[name] >= 0) & (arr[name] <= 1))
    assert np.all(arr["cif_fill"] + arr["cif_cancel"] <= 1 + 1e-12)
    assert np.allclose(arr["cif_fill"] + arr["cif_cancel"] + arr["survival"], 1.0)
    assert np.all(arr["survival"] >= 0)
    # Isotonic layers were fitted for every horizon on the calib split.
    assert all(iso is not None for iso in fitted.isotonic)


def test_o_mod_2_cancel_scaling_keeps_monotone(fitted: HazardModel) -> None:
    """When the recalibrated fill CIF plus the raw cancel CIF exceeds 1 the cancel CIF is scaled by one constant."""
    raw_f = np.array([[0.1, 0.5, 0.6], [0.0, 0.0, 0.1]])
    raw_c = np.array([[0.2, 0.3, 0.35], [0.1, 0.2, 0.3]])
    fitted_iso = fitted.isotonic
    try:
        fitted.isotonic = [None, None, None]
        cif, cc, _ = fitted._calibrated(raw_f + np.array([[0.2, 0.2, 0.2], [0, 0, 0]]), raw_c)
    finally:
        fitted.isotonic = fitted_iso
    assert cc[0] == pytest.approx(raw_c[0] * (1 - 0.8) / 0.35)
    assert np.all(np.diff(cc, axis=1) >= 0)
    assert np.all(cif + cc <= 1 + 1e-12)
    assert cc[1] == pytest.approx(raw_c[1])


def test_predictions_table_contract(fitted: HazardModel, features: pa.Table) -> None:
    sf = fitted.split_features
    assert sf is not None
    pred = fitted.predict(sf)
    assert pred.schema.equals(SCHEMAS["qt.hazard_predictions"], check_metadata=True)
    j = len(fitted.grid_ns)
    assert pred.num_rows == sf.num_rows * j
    assert pred.column("horizon_ns").to_pylist()[:j] == [int(t) for t in fitted.grid_ns]
    assert pred.column("order_ref").to_pylist()[:j] == [sf.column("order_ref")[0].as_py()] * j
    # y_fill null exactly where censored before the horizon; split copied from the input.
    assert pc.sum(pred.column("censored")).as_py() == pred.column("y_fill").null_count
    assert set(pred.column("split").cast(pa.utf8()).to_pylist()) == {"train", "calib", "test"}
    # Without outcome / split columns: y_fill null, censored false, split test.
    bare = sf.drop_columns(["event_ns", "cause", "split"])
    p2 = fitted.predict(bare)
    assert p2.column("y_fill").null_count == p2.num_rows
    assert not any(p2.column("censored").to_pylist())
    assert set(p2.column("split").cast(pa.utf8()).to_pylist()) == {"test"}
    assert p2.column("cif_fill").equals(pred.column("cif_fill"))
    with pytest.raises(QtError) as err:
        fitted.predict(sf.drop_columns(["imb_l1"]))
    assert err.value.code == "QT-INPUT-002"


def test_feature_names_and_privileged() -> None:
    f = make_features(12, orders_per_day=40, privileged=True)
    names = model_feature_names(f, privileged=False)
    assert names[0] == "side_sign" and "p_f_exact" not in names and "event_ns" not in names
    assert "p_f_exact" in model_feature_names(f, privileged=True)
    with pytest.raises(QtError) as err:
        HazardModel(_spec(privileged=True)).fit(make_features(12, orders_per_day=40))
    assert err.value.code == "QT-INPUT-002"


def test_save_load_round_trip_and_tamper(fitted: HazardModel, tmp_path: Path) -> None:
    out = tmp_path / "m"
    fitted.card = None
    fitted.save(str(out))
    card = HazardModelCard.model_validate(json.loads((out / "model.json").read_text()))
    assert [f.name for f in card.files] == ["model.npz"]
    assert sorted(p.name for p in out.iterdir()) == ["model.json", "model.npz"]
    assert card.feature_names == fitted.feature_names
    assert fitted.split_features is not None
    digest = model_sha256(card)
    assert digest == pickle_sha256(card) == hashlib.sha256((out / "model.npz").read_bytes()).hexdigest()
    loaded = HazardModel.load(str(out))  # no code runs: the card's digest is an integrity check, no trust needed
    for p in (str(out / "model.json"), str(out / "model.npz")):
        assert HazardModel.load(p, sha256=digest).warnings == []  # authenticated, fitted by this scikit-learn
    HazardModel.load(str(out), sha256=digest.upper(), trusted=True, allow_pickle=True)  # flags do not apply
    a = fitted.predict(fitted.split_features)
    b = loaded.predict(fitted.split_features)
    assert a.equals(b)
    assert loaded.card == card and loaded.n_rows_train == fitted.n_rows_train
    assert len(loaded.warnings) == 1 and "model.npz not authenticated" in loaded.warnings[0]
    # Authenticity: a trusted digest that differs is refused; a malformed one is a config error.
    with pytest.raises(QtError) as err:
        HazardModel.load(str(out), sha256="0" * 64)
    assert err.value.code == "QT-INPUT-002" and "trusted digest" in err.value.message
    with pytest.raises(QtError) as err:
        HazardModel.load(str(out), sha256="not-a-digest")
    assert err.value.code == "QT-CONFIG-001"
    # Integrity: changed bytes no longer match the card.
    blob = bytearray((out / "model.npz").read_bytes())
    blob[-30] ^= 0xFF
    (out / "model.npz").write_bytes(bytes(blob))
    for kw in ({}, {"sha256": digest}):
        with pytest.raises(QtError) as err:
            HazardModel.load(str(out), **kw)  # type: ignore[arg-type]
        assert err.value.code == "QT-INPUT-002" and "refusing" in err.value.message
    (out / "model.json").unlink()
    with pytest.raises(QtError) as err:
        HazardModel.load(str(out), sha256=digest)
    assert err.value.code == "QT-INPUT-001"


def test_recalibration_none_and_no_cancels() -> None:
    f = make_features(13, orders_per_day=150, cancel_rate=1e-9)
    m = HazardModel(_spec(recalibration="none")).fit(f)
    assert m.split_features is not None
    arr = m.predict_arrays(m.split_features)
    assert np.array_equal(arr["cif_fill"], np.clip(np.maximum.accumulate(arr["cif_fill_raw"], axis=1), 0, 1))
    assert any("no cancel events" in w for w in m.warnings)
    assert np.all(arr["cif_cancel"] == 0)


_DET_SCRIPT = """
import hashlib, sys
sys.path[:0] = [sys.argv[2], sys.argv[3]]
import pyarrow.parquet as pq
from queuetruth.model import HazardModel
f = pq.read_table(sys.argv[1] + "/features.parquet")
m = HazardModel({"features": {"path": "features.parquet"}, "seed": 5,
                 "gbdt": {"max_iter": 40, "min_samples_leaf": 40, "max_leaf_nodes": 15}}).fit(f)
m.save(sys.argv[4])
a = m.predict_arrays(m.split_features)
h = hashlib.sha256()
for k in sorted(a):
    h.update(a[k].tobytes())
npz = hashlib.sha256(open(sys.argv[4] + "/model.npz", "rb").read()).hexdigest()
card = hashlib.sha256(open(sys.argv[4] + "/model.json", "rb").read()).hexdigest()
print(h.hexdigest(), npz, card)
"""


def test_fit_deterministic_across_omp_threads(features: pa.Table, tmp_path: Path) -> None:
    """Same features + spec + seed -> identical hazards, CIFs, model.npz and model.json with OMP_NUM_THREADS 1 and 4."""
    import pyarrow.parquet as pq

    pq.write_table(features, tmp_path / "features.parquet")
    outs = []
    for threads in ("1", "4", "4"):
        env = dict(os.environ, OMP_NUM_THREADS=threads)
        r = subprocess.run(
            [
                sys.executable,
                "-c",
                _DET_SCRIPT,
                str(tmp_path),
                str(ROOT / "python"),
                str(ROOT / "python" / "tests"),
                str(tmp_path / f"out{len(outs)}"),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout.split())
    assert outs[0] == outs[1] == outs[2]
    assert hashlib.sha256((tmp_path / "out0" / "model.npz").read_bytes()).hexdigest() == outs[0][1]


def test_check_spec_rejects_n_blocks_with_days_scheme(features: pa.Table) -> None:
    """E40: a non-default field that cannot take effect raises QT-CONFIG-001 naming it (before any fitting)."""
    from queuetruth.model.hazard import check_spec
    from queuetruth.schemas.specs import HazardFitSpec

    bad = HazardFitSpec.model_validate(_spec(split={"scheme": "days", "n_blocks": 7}))
    with pytest.raises(QtError) as err:
        check_spec(bad)
    assert err.value.code == "QT-CONFIG-001" and err.value.details["field"] == "split.n_blocks"
    with pytest.raises(QtError) as err:
        HazardModel(bad).fit(features)
    assert err.value.code == "QT-CONFIG-001"
    for ok in ({"scheme": "days"}, {"scheme": "days", "n_blocks": 5}, {"scheme": "blocks", "n_blocks": 7}, {}):
        check_spec(HazardFitSpec.model_validate(_spec(split=ok)))


def test_n_blocks_honoured_by_blocks_and_flagged_when_auto_resolves_to_days(features: pa.Table) -> None:
    one_day = features.filter(pc.equal(features.column("session_date"), features.column("session_date")[0]))
    m = HazardModel(_spec(split={"scheme": "blocks", "n_blocks": 6})).fit(one_day)
    assert m.split_result is not None and m.split_result.echo["test"]["n_blocks"] == 6
    assert m.split_result.echo["test"]["blocks"] == [5]
    m5 = HazardModel(_spec(split={"scheme": "auto", "n_blocks": 6})).fit(features)  # 5 dates -> days
    assert any(
        w.startswith("split.n_blocks = 6 has no effect: split.scheme auto resolved to days") for w in m5.warnings
    )
    m_default = HazardModel(_spec()).fit(features)
    assert not any("has no effect" in w for w in m_default.warnings)
