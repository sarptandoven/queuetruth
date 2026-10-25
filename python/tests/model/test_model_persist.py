"""Safe model persistence (extension model-perf): `model.npz` is deterministic, opens as a plain npz without pickle,
reproduces the fitted model bit for bit, and every crafted or damaged file is refused with `QT-INPUT-002` before any
tree is walked. A legacy `model.pkl` is unpickled only with `allow_pickle=True` / `--allow-pickle` plus a trusted
sha256 (or `trusted=True`), with `LegacyPickleWarning`; `python -m queuetruth.model.migrate` converts it."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pickle
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from model.hazard_synth import make_features
from model.legacy_model import card_doc, write_v01_dir
from queuetruth.errors import QtError
from queuetruth.model import HazardModel, LegacyPickleWarning, TreeEnsemble, fit_hazard, model_sha256
from queuetruth.model import hazard as hazard_mod
from queuetruth.model.migrate import main as migrate_main
from queuetruth.model.persist import MAX_META_BYTES, META_MEMBER, ZIP_DATE, read_model_npz, write_model_npz
from queuetruth.schemas.io import sha256_file, write_table

ROOT = Path(__file__).resolve().parents[3]
FAST = {"max_iter": 30, "min_samples_leaf": 40, "max_leaf_nodes": 15}


def _spec(**kw: object) -> dict:
    return {"features": {"path": "features.parquet"}, "gbdt": FAST, **kw}


@pytest.fixture(scope="module")
def features() -> pa.Table:
    return make_features(41, orders_per_day=250)


@pytest.fixture(scope="module")
def fitted(features: pa.Table) -> HazardModel:
    return HazardModel(_spec()).fit(features)


@pytest.fixture()
def saved(fitted: HazardModel, tmp_path: Path) -> Path:
    out = tmp_path / "m"
    fitted.card = None
    fitted.save(str(out))
    return out


def _retarget_card(out: Path, name: str = "model.npz") -> str:
    """Rewrites the card's digest of `name` to the file's bytes (an attacker who can write both files)."""
    doc = card_doc(out)
    blob = (out / name).read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    for f in doc["files"]:
        if f["name"] == name:
            f["sha256"], f["bytes"] = sha, len(blob)
    (out / "model.json").write_text(json.dumps(doc), encoding="utf-8")
    return sha


def _refused(out: Path, needle: str) -> None:
    with pytest.raises(QtError) as err:
        HazardModel.load(str(out))
    assert err.value.code == "QT-INPUT-002", err.value
    assert needle in err.value.message, err.value.message


# ---- the format ------------------------------------------------------------------------------------------------


def test_npz_is_deterministic_and_a_plain_npz(fitted: HazardModel, tmp_path: Path) -> None:
    fitted.card = None
    fitted.save(str(tmp_path / "a"))
    fitted.card = None
    fitted.save(str(tmp_path / "b"))
    blob = (tmp_path / "a" / "model.npz").read_bytes()
    assert blob == (tmp_path / "b" / "model.npz").read_bytes()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        infos = z.infolist()
    names = [i.filename for i in infos]
    assert names[0] == META_MEMBER and names[1:] == sorted(names[1:]) and all(n.endswith(".npy") for n in names[1:])
    assert all(i.compress_type == zipfile.ZIP_STORED and i.date_time == ZIP_DATE for i in infos)
    with np.load(tmp_path / "a" / "model.npz", allow_pickle=False) as npz:  # numpy reads it without pickle
        assert npz["baseline"].shape == (1, 3)
        assert npz["tree_offsets"][-1] == npz["node.value"].size
    meta, arrays = read_model_npz(blob)
    assert meta["schema"] == "qt.hazard_model_state" and meta["format_version"] == 2
    assert meta["feature_names"] == fitted.feature_names and meta["isotonic"] == list(range(len(fitted.grid_ns)))
    assert meta["estimator"]["n_iter"] == FAST["max_iter"] and meta["estimator"]["classes"] == [0, 1, 2]
    assert not any(a.dtype.hasobject for a in arrays.values())


def test_loaded_model_is_the_fitted_model(fitted: HazardModel, saved: Path, features: pa.Table) -> None:
    m = HazardModel.load(str(saved))
    assert isinstance(m.estimator, TreeEnsemble) and m.sklearn_estimator is None
    est = fitted.estimator
    assert est is not None
    for a, b in zip(est.trees, m.estimator.trees, strict=True):
        assert a.tobytes() == b.tobytes()
    for iso_a, iso_b in zip(fitted.isotonic, m.isotonic, strict=True):
        assert iso_a is not None and iso_b is not None
        probe = np.linspace(-0.5, 1.5, 4001)  # inside and outside the fitted range (clipped)
        assert iso_a.predict(probe).tobytes() == iso_b.predict(probe).tobytes()
    sample = features.slice(0, 400)
    a, b = fitted.predict_arrays(sample), m.predict_arrays(sample)
    assert all(a[k].tobytes() == b[k].tobytes() for k in a)
    assert m.spec == fitted.spec and list(m.grid_ns) == list(fitted.grid_ns)


def test_recalibration_none_saves_no_isotonic_layers(features: pa.Table, tmp_path: Path) -> None:
    m = HazardModel(_spec(recalibration="none")).fit(features)
    m.save(str(tmp_path / "n"))
    meta, arrays = read_model_npz((tmp_path / "n" / "model.npz").read_bytes())
    assert meta["isotonic"] == [] and not any(k.startswith("iso.") for k in arrays)
    back = HazardModel.load(str(tmp_path / "n"))
    assert back.isotonic == [None] * len(back.grid_ns)
    sample = features.slice(0, 50)
    assert back.predict(sample).equals(m.predict(sample))


def _first_split(arrays: dict[str, np.ndarray]) -> int:
    return int(np.flatnonzero(arrays["node.is_leaf"] == 0)[0])


def _set(name: str, fn: Callable[[np.ndarray, dict[str, np.ndarray]], None]) -> Callable:
    def mutate(meta: dict, arrays: dict[str, np.ndarray]) -> None:
        arr = arrays[name].copy()
        fn(arr, arrays)
        arrays[name] = arr

    return mutate


STATE_MUTATIONS: dict[str, tuple[Callable[[dict, dict[str, np.ndarray]], None], str]] = {
    "left child outside its tree": (
        _set("node.left", lambda a, ar: a.__setitem__(_first_split(ar), 10**6)),
        "left child is outside its tree",
    ),
    "right child loops back to its parent": (
        _set("node.right", lambda a, ar: a.__setitem__(_first_split(ar), 0)),
        "right child is outside its tree",
    ),
    "split feature outside the design": (
        _set("node.feature_idx", lambda a, ar: a.__setitem__(_first_split(ar), 10**4)),
        "split feature is outside",
    ),
    "negative split feature": (
        _set("node.feature_idx", lambda a, ar: a.__setitem__(_first_split(ar), -1)),
        "split feature is outside",
    ),
    "categorical split": (_set("node.is_categorical", lambda a, ar: a.__setitem__(0, 1)), "categorical"),
    "NaN threshold": (
        _set("node.num_threshold", lambda a, ar: a.__setitem__(_first_split(ar), np.nan)),
        "threshold is NaN",
    ),
    "infinite leaf value": (
        _set("node.value", lambda a, ar: a.__setitem__(int(np.flatnonzero(ar["node.is_leaf"])[0]), np.inf)),
        "leaf value is not finite",
    ),
    "flag not 0 or 1": (_set("node.is_leaf", lambda a, ar: a.__setitem__(0, 7)), "flags not 0 or 1"),
    "node count disagrees with offsets": (
        _set("tree_offsets", lambda a, ar: a.__setitem__(-1, a[-1] + 1)),
        "entries, expected",
    ),
    "offsets not increasing": (_set("tree_offsets", lambda a, ar: a.__setitem__(1, 0)), "not increasing"),
    "wrong node dtype": (lambda m, ar: ar.__setitem__("node.left", ar["node.left"].astype("<i8")), "dtype"),
    "baseline shape": (lambda m, ar: ar.__setitem__("baseline", np.zeros((1, 2))), "baseline"),
    "extra array": (lambda m, ar: ar.__setitem__("extra", np.zeros(1)), "unexpected"),
    "missing array": (lambda m, ar: ar.pop("node.gain"), "missing or unexpected"),
    "wrong schema": (lambda m, ar: m.__setitem__("schema", "qt.other"), "expected 'qt.hazard_model_state'"),
    "pickle-era format version": (lambda m, ar: m.__setitem__("format_version", 1), "format 1"),
    "feature names differ from the card": (
        lambda m, ar: m.__setitem__("feature_names", m["feature_names"][::-1]),
        "differ from model.json",
    ),
    "unknown class label": (lambda m, ar: m["estimator"].__setitem__("classes", [0, 1, 5]), "classes"),
    "design width": (lambda m, ar: m["estimator"].__setitem__("n_features", 3), "n_features"),
    "trees per iteration": (lambda m, ar: m["estimator"].__setitem__("n_trees_per_iteration", 2), "trees per"),
    "isotonic layer index outside the grid": (lambda m, ar: m.__setitem__("isotonic", [0, 99]), "isotonic layers"),
    "isotonic thresholds not increasing": (
        _set("iso.0.x", lambda a, ar: a.__setitem__(slice(None), a[::-1].copy())),
        "not increasing",
    ),
    "isotonic values out of range": (_set("iso.0.y", lambda a, ar: a.__setitem__(-1, 2.0)), "out of range"),
    "isotonic clip range wider than its thresholds": (  # would clip to points outside the table: NaN predictions
        _set("iso.0.bounds", lambda a, ar: a.__setitem__(slice(None), [ar["iso.0.x"][0] - 1, ar["iso.0.x"][-1] + 1])),
        "clip range",
    ),
    "isotonic clip range narrower than its thresholds": (
        _set("iso.0.bounds", lambda a, ar: a.__setitem__(1, ar["iso.0.x"][-2])),
        "clip range",
    ),
    "fitted_with not a string": (lambda m, ar: m["estimator"].__setitem__("fitted_with", 1), "fitted_with"),
    "fitted_with missing": (lambda m, ar: m["estimator"].pop("fitted_with"), "invalid model state"),
    "isotonic layers with recalibration none": (
        lambda m, ar: m["spec"].__setitem__("recalibration", "none"),
        "recalibration",
    ),
    "spec not a HazardFitSpec": (lambda m, ar: m["spec"].__setitem__("seed", "zero"), "invalid model state"),
    "meta field of the wrong type": (lambda m, ar: m.__setitem__("isotonic", "0"), "isotonic layers"),
}


@pytest.mark.parametrize("case", sorted(STATE_MUTATIONS))
def test_crafted_model_state_is_refused(saved: Path, case: str) -> None:
    mutate, needle = STATE_MUTATIONS[case]
    meta, arrays = read_model_npz((saved / "model.npz").read_bytes())
    mutate(meta, arrays)
    write_model_npz(saved / "model.npz", meta, arrays)
    _retarget_card(saved)
    _refused(saved, needle)


def _npy(header: str, payload: bytes, version: bytes = b"\x01\x00") -> bytes:
    h = header.encode("latin1")
    pad = 64 - (10 + len(h) + 1) % 64
    h = h + b" " * pad + b"\n"
    return b"\x93NUMPY" + version + len(h).to_bytes(2, "little") + h + payload


class _Boom:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __reduce__(self) -> tuple:
        return (os.mkdir, (self.marker,))


def _rezip(saved: Path, members: dict[str, bytes], compress: int = zipfile.ZIP_STORED) -> None:
    with zipfile.ZipFile(saved / "model.npz", "w", compression=compress) as z:
        for name, data in members.items():
            z.writestr(name, data)
    _retarget_card(saved)


def _members(saved: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(saved / "model.npz") as z:
        return {n: z.read(n) for n in z.namelist()}


def test_damaged_or_hostile_archives_are_refused(saved: Path, tmp_path: Path) -> None:
    good = _members(saved)
    marker = tmp_path / "PWNED"
    evil = pickle.dumps(_Boom(str(marker)))
    cases = {
        "not an archive": None,
        "compressed member": dict(good),
        "unexpected member": {**good, "readme.txt": b"hi"},
        "member in a directory": {**good, "sub/x.npy": good["baseline.npy"]},
        "object dtype holding a pickle": {
            **good,
            "baseline.npy": _npy("{'descr': '|O', 'fortran_order': False, 'shape': (1,), }", evil),
        },
        "header promises more data than present": {
            **good,
            "baseline.npy": _npy("{'descr': '<f8', 'fortran_order': False, 'shape': (1000000000000,), }", b"\0" * 8),
        },
        "npy format 2.0": {
            **good,
            "baseline.npy": _npy(
                "{'descr': '<f8', 'fortran_order': False, 'shape': (1, 3), }", b"\0" * 24, b"\x02\x00"
            ),
        },
        "meta not JSON": {**good, META_MEMBER: b"\x80not json"},
        "meta not an object": {**good, META_MEMBER: b"[1, 2]"},
        "meta nested too deep": {**good, META_MEMBER: b"[" * 200_000},
        "meta over the size limit": {**good, META_MEMBER: b" " * (MAX_META_BYTES + 1) + b"{}"},
        "meta missing": {k: v for k, v in good.items() if k != META_MEMBER},
    }
    needles = {
        "not an archive": "not a model archive",
        "compressed member": "compressed",
        "unexpected member": "unexpected member",
        "member in a directory": "unexpected member",
        "object dtype holding a pickle": "object dtype refused",
        "header promises more data than present": "header promises",
        "npy format 2.0": "format 1.0",
        "meta not JSON": "unreadable member",
        "meta not an object": "not an object",
        "meta nested too deep": "RecursionError",
        "meta over the size limit": "limit",
        "meta missing": "meta.json missing",
    }
    for name, members in cases.items():
        if members is None:
            (saved / "model.npz").write_bytes(b"PK\x03\x04 not really a zip")
            _retarget_card(saved)
        elif name == "compressed member":
            _rezip(saved, members, zipfile.ZIP_DEFLATED)
        else:
            _rezip(saved, members)
        _refused(saved, needles[name])
    assert not marker.exists()


def test_isotonic_layers_round_trip_unchanged_bounds(fitted: HazardModel) -> None:
    """The clip range scikit-learn's own fit leaves is the first and last threshold (what loading requires)."""
    for iso in fitted.isotonic:
        assert iso is not None
        assert iso.X_min_ == iso.X_thresholds_[0] and iso.X_max_ == iso.X_thresholds_[-1]


def test_oversized_model_file_is_refused_without_reading_it_whole(saved: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    size = (saved / "model.npz").stat().st_size
    monkeypatch.setattr(hazard_mod, "MAX_MODEL_BYTES", size - 1)
    _refused(saved, f"over {size - 1} bytes")
    monkeypatch.setattr(hazard_mod, "MAX_MODEL_BYTES", size)
    HazardModel.load(str(saved))


def test_load_reports_authenticity_and_the_fitting_version(saved: Path, tmp_path: Path) -> None:
    import sklearn

    digest = model_sha256(card_doc(saved))
    assert HazardModel.load(str(saved), sha256=digest).warnings == []
    assert HazardModel.load(str(saved)).warnings == [
        "model.npz not authenticated: loaded without a trusted sha256 (its digest matches only the model.json in "
        "the same directory)"
    ]
    meta, arrays = read_model_npz((saved / "model.npz").read_bytes())
    assert meta["estimator"]["fitted_with"] == f"scikit-learn {sklearn.__version__}"
    meta["estimator"]["fitted_with"] = "scikit-learn 1.6.0"
    write_model_npz(saved / "model.npz", meta, arrays)
    digest = _retarget_card(saved)
    m = HazardModel.load(str(saved), sha256=digest)
    assert m.fitted_with == "scikit-learn 1.6.0"
    assert m.warnings == [
        f"fitted with scikit-learn 1.6.0, loaded with scikit-learn {sklearn.__version__}: predictions are tested only "
        "under the fitting version"
    ]
    m.save(str(tmp_path / "again"))  # a re-save keeps the version that fitted the model
    again, _ = read_model_npz((tmp_path / "again" / "model.npz").read_bytes())
    assert again["estimator"]["fitted_with"] == "scikit-learn 1.6.0"


def test_node_record_fields_must_match_on_save_and_load(fitted: HazardModel, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scikit-learn whose node record gains or loses a field is refused on save (no file this environment could not
    load again) and on load (no field silently dropped)."""
    est = fitted.estimator
    assert est is not None
    arrays = est.to_arrays()
    record = TreeEnsemble.trees_dtype()
    wider = np.dtype([*((n, record.fields[n][0]) for n in record.names), ("future_field", "<f8")])
    narrower = np.dtype([(n, record.fields[n][0]) for n in record.names if n != "gain"])
    for dt in (wider, narrower):
        clone = TreeEnsemble.__new__(TreeEnsemble)
        clone.__dict__.update(est.__dict__)
        clone.trees = [np.zeros(t.size, dtype=dt) for t in est.trees]
        with pytest.raises(QtError) as err:
            clone.to_arrays()
        assert err.value.code == "QT-ENGINE-001" and "node record" in err.value.message
        monkeypatch.setattr(TreeEnsemble, "trees_dtype", staticmethod(lambda dt=dt: dt))
        with pytest.raises(QtError) as err:
            TreeEnsemble.from_arrays([0, 1, 2], arrays, len(est.trees), est.n_features)
        assert err.value.code == "QT-ENGINE-001" and "node record" in err.value.message
        monkeypatch.undo()


def test_load_needs_a_card_that_lists_a_model(saved: Path) -> None:
    doc = card_doc(saved)
    doc["files"] = []
    (saved / "model.json").write_text(json.dumps(doc), encoding="utf-8")
    _refused(saved, "lists neither model.npz nor model.pkl")
    (saved / "model.json").write_text("{}", encoding="utf-8")
    _refused(saved, "invalid model.json")
    (saved / "model.json").write_text("[" * 200_000, encoding="utf-8")
    _refused(saved, "RecursionError")


def test_model_sha256_prefers_the_safe_file() -> None:
    npz, pkl = "a" * 64, "b" * 64
    assert model_sha256({"files": [{"name": "model.pkl", "sha256": pkl}]}) == pkl
    assert model_sha256({"files": [{"name": "model.pkl", "sha256": pkl}, {"name": "model.npz", "sha256": npz}]}) == npz
    assert model_sha256({"files": [{"name": "predictions.parquet", "sha256": npz}]}) is None
    assert model_sha256({}) is None


# ---- legacy pickles ----------------------------------------------------------------------------------------------


def test_legacy_pickle_needs_allow_pickle_and_a_trusted_digest(fitted: HazardModel, tmp_path: Path) -> None:
    d = tmp_path / "v01"
    fitted.card = None
    sha = write_v01_dir(fitted, d)
    for kw in ({}, {"sha256": sha}, {"trusted": True}):
        with pytest.raises(QtError) as err:
            HazardModel.load(str(d), **kw)  # type: ignore[arg-type]
        assert err.value.code == "QT-CONFIG-001" and err.value.details["field"] == "allow_pickle"
        assert "allow_pickle=True" in err.value.message and "sha256" in err.value.message
        assert "python -m queuetruth.model.migrate" in err.value.message
    with pytest.raises(QtError) as err:
        HazardModel.load(str(d), allow_pickle=True)
    assert err.value.code == "QT-CONFIG-001" and "without a trusted sha256" in err.value.message
    with pytest.raises(QtError) as err:
        HazardModel.load(str(d), allow_pickle=True, sha256="1" * 64)
    assert err.value.code == "QT-INPUT-002" and "trusted digest" in err.value.message
    for kw in ({"sha256": sha}, {"trusted": True}):
        with pytest.warns(LegacyPickleWarning, match="migrate"):
            m = HazardModel.load(str(d), allow_pickle=True, **kw)  # type: ignore[arg-type]
        assert isinstance(m.estimator, TreeEnsemble)
        assert any("legacy model.pkl" in w for w in m.warnings)
        assert fitted.split_features is not None
        sample = fitted.split_features.slice(0, 300)
        assert m.predict(sample).equals(fitted.predict(sample))


def test_legacy_pickle_keeps_the_version_that_fitted_it(
    fitted: HazardModel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sklearn
    import sklearn.base
    from sklearn.exceptions import InconsistentVersionWarning

    from model.legacy_model import v01_state

    d = tmp_path / "v01"
    fitted.card = None
    sha = write_v01_dir(fitted, d)
    with pytest.warns(LegacyPickleWarning):
        m = HazardModel.load(str(d), allow_pickle=True, sha256=sha)
    assert m.fitted_with == f"scikit-learn {sklearn.__version__}"
    monkeypatch.setattr(sklearn.base, "__version__", "1.6.0")  # pickled as if by scikit-learn 1.6.0
    blob = pickle.dumps(v01_state(fitted), protocol=5)
    monkeypatch.undo()
    sha = write_v01_dir(fitted, tmp_path / "old", blob=blob)
    with pytest.warns(LegacyPickleWarning), pytest.warns(InconsistentVersionWarning):
        m = HazardModel.load(str(tmp_path / "old"), allow_pickle=True, sha256=sha)
    assert m.fitted_with == "scikit-learn 1.6.0"
    assert any(w.startswith("fitted with scikit-learn 1.6.0, loaded with") for w in m.warnings)
    m.save(str(tmp_path / "npz"))
    meta, _ = read_model_npz((tmp_path / "npz" / "model.npz").read_bytes())
    assert meta["estimator"]["fitted_with"] == "scikit-learn 1.6.0"


def test_hostile_pickle_is_never_unpickled_without_the_opt_in_and_digest(fitted: HazardModel, tmp_path: Path) -> None:
    d = tmp_path / "evil"
    marker = tmp_path / "PWNED"
    fitted.card = None
    sha = write_v01_dir(fitted, d, blob=pickle.dumps(_Boom(str(marker))))  # the attacker writes both files
    attempts: list[dict] = [{}, {"sha256": sha}, {"trusted": True}, {"allow_pickle": True}]
    attempts.append({"allow_pickle": True, "sha256": "0" * 64})
    for kw in attempts:
        with pytest.raises(QtError):
            HazardModel.load(str(d), **kw)
        assert not marker.exists(), kw
    for argv in ([str(d)], [str(d), "--sha256", sha], [str(d), "--allow-pickle"]):
        assert migrate_main(argv) == 2
        assert not marker.exists(), argv
    assert not (d / "model.npz").exists()


def test_unsupported_legacy_state_is_refused(fitted: HazardModel, tmp_path: Path) -> None:
    d = tmp_path / "odd"
    fitted.card = None
    sha = write_v01_dir(fitted, d, blob=pickle.dumps({"format_version": 7}))
    with pytest.warns(LegacyPickleWarning), pytest.raises(QtError) as err:
        HazardModel.load(str(d), allow_pickle=True, sha256=sha)
    assert err.value.code == "QT-INPUT-002" and "unsupported format" in err.value.message


# ---- migrate -----------------------------------------------------------------------------------------------------


@pytest.fixture()
def v01_fit_dir(features: pa.Table, tmp_path: Path) -> tuple[Path, str, dict]:
    """A `fit_hazard` output directory turned into its v0.1 form; returns it, the pickle digest and the v0.2 card."""
    feat = tmp_path / "features.parquet"
    write_table(features, feat)
    spec = {"features": {"path": str(feat)}, "out": str(tmp_path / "fit"), "gbdt": FAST, "bootstrap_b": 50}
    card = fit_hazard(spec)
    new_doc = card_doc(tmp_path / "fit")
    m = HazardModel(spec).fit(pq.read_table(feat))
    m.card = card
    sha = write_v01_dir(m, tmp_path / "fit")
    assert [f["name"] for f in card_doc(tmp_path / "fit")["files"]] == ["predictions.parquet", "model.pkl"]
    return tmp_path / "fit", sha, new_doc


def _err(capsys: pytest.CaptureFixture[str]) -> dict:
    """The `qt.error` document of a `--format json` run: exactly one JSON document on stdout."""
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1, out
    return json.loads(out)


J = ["--format", "json"]


def test_migrate_converts_a_v01_directory(
    v01_fit_dir: tuple[Path, str, dict], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d, sha, new_doc = v01_fit_dir
    assert migrate_main([str(d)]) == 2  # text: the message and hint on stderr, nothing on stdout
    io_ = capsys.readouterr()
    assert io_.out == "" and io_.err.startswith("QT-CONFIG-001: migrate: refusing to unpickle")
    assert migrate_main([str(d), *J]) == 2
    assert _err(capsys)["code"] == "QT-CONFIG-001"
    assert migrate_main([str(d), "--allow-pickle", *J]) == 2
    doc = _err(capsys)
    assert doc["code"] == "QT-CONFIG-001" and doc["details"]["field"] == "sha256"
    assert migrate_main([str(d), "--allow-pickle", "--sha256", "f" * 64, *J]) == 2
    assert _err(capsys)["code"] == "QT-INPUT-002"
    assert not (d / "model.npz").exists()
    # To another directory: the card and model.npz are exactly what fit_hazard writes today.
    out = tmp_path / "converted"
    with pytest.warns(LegacyPickleWarning):
        assert migrate_main([str(d), "--allow-pickle", "--sha256", sha, "--out", str(out)]) == 0
    digest = capsys.readouterr().out.strip()
    assert digest == sha256_file(out / "model.npz") == model_sha256(new_doc)
    assert card_doc(out) == new_doc
    assert sorted(p.name for p in out.iterdir()) == ["model.json", "model.npz", "predictions.parquet"]
    assert HazardModel.load(str(out), sha256=digest).card is not None
    # In place: model.pkl stays on disk but is no longer listed; a second run has nothing to do.
    with pytest.warns(LegacyPickleWarning):
        assert migrate_main([str(d), "--allow-pickle", "--trusted"]) == 0
    assert capsys.readouterr().out.strip() == digest
    assert card_doc(d) == new_doc and (d / "model.pkl").exists()
    assert migrate_main([str(d)]) == 0
    io_ = capsys.readouterr()
    assert io_.out.strip() == digest and "model.pkl left in place" in io_.err
    assert migrate_main([str(d), *J]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"model": str(d / "model.npz"), "sha256": digest, "legacy_pickle_left": True}


def test_migrate_checks_the_files_it_copies(
    v01_fit_dir: tuple[Path, str, dict], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d, sha, _ = v01_fit_dir
    with open(d / "predictions.parquet", "ab") as f:
        f.write(b"tampered")
    assert migrate_main([str(d), "--allow-pickle", "--sha256", sha, "--out", str(tmp_path / "o"), *J]) == 2
    assert _err(capsys)["code"] == "QT-INPUT-002"
    assert not (tmp_path / "o").exists()  # checked before anything is created, copied or unpickled
    for missing in (tmp_path / "nowhere", d / "typo"):  # a mistyped subdirectory never migrates its parent
        assert migrate_main([str(missing), "--allow-pickle", "--sha256", sha, *J]) == 2
        doc = _err(capsys)
        assert doc["code"] == "QT-INPUT-001" and doc["message"] == f"migrate: {missing} does not exist"
    assert not (d / "model.npz").exists()


def test_migrate_module_entry_point(v01_fit_dir: tuple[Path, str, dict]) -> None:
    d, sha, new_doc = v01_fit_dir
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "python"), os.environ.get("PYTHONPATH", "")]))
    cmd = [sys.executable, "-m", "queuetruth.model.migrate", str(d)]
    r = subprocess.run([*cmd, *J], capture_output=True, text=True, env=env, check=False, timeout=300)
    assert r.returncode == 2 and json.loads(r.stdout)["code"] == "QT-CONFIG-001"
    r = subprocess.run([*cmd, "--allow-pickle", "--sha256", sha], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 0, r.stderr
    assert "LegacyPickleWarning" in r.stderr and r.stdout.strip() == model_sha256(new_doc)


# ---- real data (opt-in) ------------------------------------------------------------------------------------------

REALDATA = Path(os.environ.get("QT_REALDATA_DIR", str(ROOT / ".realdata")))


@pytest.mark.realdata
def test_real_v01_pickle_migrates_to_identical_predictions(tmp_path: Path) -> None:
    """The v0.1 model fitted on the two Nasdaq days (RD-10) converts to `model.npz` and predicts exactly what the
    pickle does and what its `predictions.parquet` holds."""
    src = REALDATA / "hazard" / "2d-40000-l2"
    feat = REALDATA / "features" / "2d-40000" / "features.parquet"
    if not (src / "model.pkl").exists() or not feat.exists():
        pytest.skip("needs .realdata/hazard/2d-40000-l2 and .realdata/features/2d-40000 (set QT_REALDATA_DIR)")
    sha = model_sha256(card_doc(src))
    assert sha is not None
    with pytest.warns(LegacyPickleWarning):
        legacy = HazardModel.load(str(src), allow_pickle=True, sha256=sha)
    with pytest.warns(LegacyPickleWarning):
        assert migrate_main([str(src), "--allow-pickle", "--sha256", sha, "--out", str(tmp_path / "npz")]) == 0
    safe = HazardModel.load(str(tmp_path / "npz"))
    features = pq.read_table(feat)
    a, b = legacy.predict(features), safe.predict(features)
    assert a.equals(b)
    stored = pq.read_table(src / "predictions.parquet")
    key = ["order_ref", "landmark_ns", "horizon_ns"]
    joined = stored.select([*key, "cif_fill", "cif_cancel"]).join(
        b.select([*key, "cif_fill", "cif_cancel"]), keys=key, right_suffix="_npz"
    )
    assert joined.num_rows == stored.num_rows
    for col in ("cif_fill", "cif_cancel"):
        x = joined.column(col).to_numpy()
        y = joined.column(f"{col}_npz").to_numpy()
        assert x.tobytes() == y.tobytes()


def test_failed_save_leaves_no_partial_file(
    fitted: HazardModel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from queuetruth.model import hazard as hazard_mod

    def boom(path: Path, meta: dict, arrays: dict) -> None:
        Path(path).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(hazard_mod, "write_model_npz", boom)
    with pytest.raises(OSError):
        fitted.save(str(tmp_path / "f"))
    assert list((tmp_path / "f").iterdir()) == []


def test_migrate_copies_only_plain_file_names(
    v01_fit_dir: tuple[Path, str, dict], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d, sha, _ = v01_fit_dir
    doc = card_doc(d)
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"x")
    doc["files"].append({"name": "../outside.parquet", "rows": None, "bytes": 1, "sha256": sha256_file(outside)})
    (d / "model.json").write_text(json.dumps(doc), encoding="utf-8")
    assert migrate_main([str(d), "--allow-pickle", "--sha256", sha, "--out", str(tmp_path / "o" / "deep"), *J]) == 2
    assert _err(capsys)["code"] == "QT-INPUT-002"
    assert not (tmp_path / "o").exists()
    doc["files"][-1]["name"] = "gone.parquet"
    (d / "model.json").write_text(json.dumps(doc), encoding="utf-8")
    assert migrate_main([str(d), "--allow-pickle", "--sha256", sha, "--out", str(tmp_path / "o2"), *J]) == 2
    assert _err(capsys)["code"] == "QT-INPUT-001"
    assert not (tmp_path / "o2").exists()
