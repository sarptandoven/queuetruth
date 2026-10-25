"""Safe persistence of the learned hazard model (SPEC 5.5.8, extension `model-perf`): `model.npz`, no pickle.

`model.npz` is a ZIP archive of uncompressed (`ZIP_STORED`) members with a fixed timestamp, so the same model gives
the same bytes on every platform (SPEC 7.2):

- `meta.json`: `qt.hazard_model_state` (UTF-8 JSON): `format_version` 2, the spec echo, feature names, grid,
  `n_rows_train`, the estimator header (`classes`, `n_iter`, `n_trees_per_iteration`, `n_features`, the
  scikit-learn version that fitted it) and the isotonic layers present.
- `.npy` members written with `allow_pickle=False` and read back with `allow_pickle=False`: `baseline` (float64,
  1 x K), `tree_offsets` (int64, n_trees + 1; tree `t` = nodes `tree_offsets[t]:tree_offsets[t + 1]`, iteration-major
  then class), one `node.<field>` array per field of scikit-learn's tree node record (values, thresholds, children,
  ...), and per recalibrated grid point `j` `iso.<j>.x` / `iso.<j>.y` (the isotonic thresholds) and `iso.<j>.bounds`
  (the clip range).

Reading never executes code: members are parsed by `numpy.lib.format.read_array(allow_pickle=False)` and `json`,
only stored (uncompressed) members with the expected names, dtypes and shapes are accepted, and every tree is
checked before use (children strictly after their parent and inside the tree, split features inside the design, no
categorical splits, finite leaf values), because scikit-learn's tree walker does not bound-check node indices.
A file that fails any check is refused with `QT-INPUT-002`.

`TreeEnsemble` is the prediction form of a fitted `HistGradientBoostingClassifier`: scikit-learn's own
`TreePredictor` per tree and its own loss link, so `TreeEnsemble.from_sklearn(est).predict_proba(X)` equals
`est.predict_proba(X)` bit for bit (tested), whether the ensemble was just fitted or read back from `model.npz`.
Prediction runs in row blocks on a thread pool (one OpenMP thread per call): every row is the same sum over the
trees in the same order, so the result does not depend on the block size or the number of threads.

Legacy `model.pkl` (format 1, v0.1) is read only by `load_legacy_pickle`, which callers reach only with an explicit
`allow_pickle=True` (`HazardModel.load`) or `--allow-pickle` (`python -m queuetruth.model.migrate`), after the
pickle's sha256 is authenticated; it emits `LegacyPickleWarning`.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from queuetruth.errors import QtError
from queuetruth.model._common import input_error

MODEL_NAME = "model.npz"
LEGACY_PICKLE_NAME = "model.pkl"
STATE_SCHEMA = "qt.hazard_model_state"
STATE_FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1
META_MEMBER = "meta.json"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
#: Refuse model files above this size (a fitted default model is about 3 MB; 300 iterations x 3 trees of the
#: largest `max_leaf_nodes` the spec allows stay far below it).
MAX_MODEL_BYTES = 256 << 20
#: Refuse a `meta.json` member above this size before decoding it (a default header is about 2 KB).
MAX_META_BYTES = 1 << 20
#: Rows per prediction block (each block runs every tree on its rows; about 1 MB of design per block).
BLOCK_ROWS = 4096

#: Node record fields in file order, with the portable dtype each is stored as.
NODE_FIELDS: dict[str, str] = {
    "value": "<f8",
    "count": "<u4",
    "feature_idx": "<i8",
    "num_threshold": "<f8",
    "missing_go_to_left": "|u1",
    "left": "<u4",
    "right": "<u4",
    "gain": "<f8",
    "depth": "<u4",
    "is_leaf": "|u1",
    "bin_threshold": "|u1",
    "is_categorical": "|u1",
    "bitset_idx": "<u4",
}


class LegacyPickleWarning(UserWarning):
    """A legacy `model.pkl` was unpickled (`allow_pickle=True`): migrate it to `model.npz`."""


def _engine_error(message: str) -> QtError:
    return QtError("QT-ENGINE-001", message)


def _sklearn_parts() -> tuple[Any, Any, Any, Any, Any]:
    """scikit-learn's tree node dtype, tree predictor, the two classification losses and the OpenMP thread count.

    These are private scikit-learn names (the declared range is `scikit-learn>=1.6,<2`): the node record's field set
    is checked on save and on load (`to_arrays`, `from_arrays`), and a model loaded under another scikit-learn version
    than the one that fitted it says so in its warnings (`HazardModel.load`)."""
    from sklearn._loss.loss import HalfBinomialLoss, HalfMultinomialLoss
    from sklearn.ensemble._hist_gradient_boosting.common import PREDICTOR_RECORD_DTYPE
    from sklearn.ensemble._hist_gradient_boosting.predictor import TreePredictor
    from sklearn.utils._openmp_helpers import _openmp_effective_n_threads

    return PREDICTOR_RECORD_DTYPE, TreePredictor, HalfBinomialLoss, HalfMultinomialLoss, _openmp_effective_n_threads


def effective_threads() -> int:
    """Worker threads for prediction: scikit-learn's OpenMP thread count (honours `OMP_NUM_THREADS`, physical cores
    within the process's CPU affinity and cgroup quota)."""
    return max(1, int(_sklearn_parts()[4]()))


def allotted_cpus() -> int:
    """Logical CPUs this process may run on: scikit-learn's count without the physical-core rule (CPU affinity and
    cgroup quota taken into account, unlike `os.cpu_count()`)."""
    return max(1, int(_sklearn_parts()[4](only_physical_cores=False)))


def _canonical_zero(nodes: np.ndarray) -> np.ndarray:
    """`nodes` with every split threshold of -0.0 written as +0.0 (a copy when one is changed).

    scikit-learn's bin thresholds come from `np.unique` of the feature values, which keeps either -0.0 or +0.0 when
    both occur (they compare equal and the sort is not stable), so the same fit can store either sign from run to
    run. The walk compares `x <= threshold`, for which the two are the same for every x, so predictions do not
    change and the saved bytes become deterministic (SPEC 7.2)."""
    thr = nodes["num_threshold"]
    neg_zero = (thr == 0.0) & np.signbit(thr)
    if not neg_zero.any():
        return nodes
    out = nodes.copy()
    out["num_threshold"][neg_zero] = 0.0
    return out


class TreeEnsemble:
    """Prediction form of a fitted `HistGradientBoostingClassifier` (module docstring).

    `classes_` are the class labels, `baseline` the (1, K) intercept, `trees` the node arrays (scikit-learn's
    `PREDICTOR_RECORD_DTYPE`) in iteration-major then class order, `n_features` the design width."""

    def __init__(self, classes: np.ndarray, baseline: np.ndarray, trees: list[np.ndarray], n_features: int) -> None:
        record, predictor, binomial, multinomial, _ = _sklearn_parts()
        self.classes_ = np.asarray(classes, dtype=np.int64)
        self.baseline = np.asarray(baseline, dtype=np.float64).reshape(1, -1)
        k = self.baseline.shape[1]
        if len(trees) % k:
            raise _engine_error(f"TreeEnsemble: {len(trees)} trees is not a multiple of {k} trees per iteration")
        if self.classes_.size != (2 if k == 1 else k):
            raise _engine_error(f"TreeEnsemble: {self.classes_.size} classes with {k} trees per iteration")
        self.trees = [_canonical_zero(np.asarray(t, dtype=record)) for t in trees]
        self.n_features = int(n_features)
        self.n_trees_per_iteration = k
        self.n_iter = len(trees) // k
        empty = np.zeros((0, 8), dtype=np.uint32)
        self._predictors = [predictor(t, empty, empty) for t in self.trees]
        self._loss = binomial() if k == 1 else multinomial(n_classes=k)
        self._known_cat = np.zeros((0, 8), dtype=np.uint32)
        self._f_idx_map = np.zeros(self.n_features, dtype=np.uint32)

    @classmethod
    def from_sklearn(cls, est: Any) -> TreeEnsemble:
        """The ensemble of a fitted `HistGradientBoostingClassifier` without categorical features."""
        bitsets = [(p.raw_left_cat_bitsets, p.binned_left_cat_bitsets) for it in est._predictors for p in it]
        is_cat = getattr(est, "is_categorical_", None)
        if (is_cat is not None and np.any(is_cat)) or any(a.size or b.size for a, b in bitsets):
            raise _engine_error("TreeEnsemble: categorical splits are not supported")
        trees = [p.nodes.copy() for it in est._predictors for p in it]
        return cls(est.classes_, est._baseline_prediction, trees, est.n_features_in_)

    def raw_predict(self, x: np.ndarray, *, n_threads: int | None = None) -> np.ndarray:
        """Raw scores (rows, K): baseline plus the leaf values of every tree, summed in tree order."""
        x = np.ascontiguousarray(x, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != self.n_features:
            raise input_error(f"TreeEnsemble: design has shape {x.shape}, expected (rows, {self.n_features})")
        n = x.shape[0]
        raw = np.zeros((n, self.n_trees_per_iteration), dtype=np.float64, order="F")
        if n == 0:
            return raw
        threads = effective_threads() if n_threads is None else max(1, int(n_threads))
        starts = list(range(0, n, BLOCK_ROWS))

        def run(lo: int) -> None:
            hi = min(n, lo + BLOCK_ROWS)
            xb = x[lo:hi]
            rb = np.zeros((hi - lo, self.n_trees_per_iteration), dtype=np.float64, order="F")
            rb += self.baseline
            k_count = self.n_trees_per_iteration
            for t, p in enumerate(self._predictors):
                rb[:, t % k_count] += p.predict(xb, self._known_cat, self._f_idx_map, 1)
            raw[lo:hi] = rb

        if threads == 1 or len(starts) == 1:
            for lo in starts:
                run(lo)
        else:
            with ThreadPoolExecutor(max_workers=min(threads, len(starts))) as pool:
                list(pool.map(run, starts))
        return raw

    def predict_proba(self, x: np.ndarray, *, n_threads: int | None = None) -> np.ndarray:
        """Class probabilities (rows, len(classes_)), equal to scikit-learn's `predict_proba`."""
        return np.asarray(self._loss.predict_proba(self.raw_predict(x, n_threads=n_threads)))

    # ---- arrays ------------------------------------------------------------------------------------------------

    def to_arrays(self) -> dict[str, np.ndarray]:
        """`baseline`, `tree_offsets` and `node.<field>` (module docstring)."""
        sizes = np.array([t.size for t in self.trees], dtype=np.int64)
        offsets = np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64)
        nodes = np.concatenate(self.trees) if self.trees else np.zeros(0, dtype=self.trees_dtype())
        have = list(nodes.dtype.names or ())
        absent = [f for f in NODE_FIELDS if f not in have]
        unknown = [f for f in have if f not in NODE_FIELDS]
        if absent or unknown:  # a file this environment could not load again is never written
            raise _engine_error(
                f"model.npz: this scikit-learn's tree node record differs from the stored fields (missing {absent}, "
                f"unknown {unknown})"
            )
        out = {"baseline": self.baseline.astype("<f8"), "tree_offsets": offsets.astype("<i8")}
        for name, dt in NODE_FIELDS.items():
            out[f"node.{name}"] = np.ascontiguousarray(nodes[name]).astype(dt)
        return out

    @staticmethod
    def trees_dtype() -> np.dtype:
        """scikit-learn's tree node record dtype."""
        return np.dtype(_sklearn_parts()[0])

    @classmethod
    def from_arrays(
        cls, classes: list[int], arrays: dict[str, np.ndarray], n_trees: int, n_features: int
    ) -> TreeEnsemble:
        """Rebuilds the ensemble from validated arrays (`validate_trees` ran on them)."""
        record = cls.trees_dtype()
        unknown = [f for f in record.names if f not in NODE_FIELDS]
        absent = [f for f in NODE_FIELDS if f not in record.names]
        if unknown or absent:
            raise _engine_error(
                f"model.npz: this scikit-learn's tree node record differs from the stored fields (missing {absent}, "
                f"unknown {unknown})"
            )
        offsets = arrays["tree_offsets"]
        nodes = np.zeros(int(offsets[-1]), dtype=record)
        for name in record.names:
            nodes[name] = arrays[f"node.{name}"]
        trees = [nodes[int(offsets[t]) : int(offsets[t + 1])].copy() for t in range(n_trees)]
        return cls(np.asarray(classes, dtype=np.int64), arrays["baseline"], trees, n_features)


# ---- isotonic layers ---------------------------------------------------------------------------------------------


def isotonic_arrays(j: int, iso: Any) -> dict[str, np.ndarray]:
    """`iso.<j>.x`, `iso.<j>.y`, `iso.<j>.bounds` of a fitted `IsotonicRegression`."""
    return {
        f"iso.{j}.x": np.asarray(iso.X_thresholds_, dtype="<f8"),
        f"iso.{j}.y": np.asarray(iso.y_thresholds_, dtype="<f8"),
        f"iso.{j}.bounds": np.array([iso.X_min_, iso.X_max_], dtype="<f8"),
    }


def isotonic_from_arrays(x: np.ndarray, y: np.ndarray, bounds: np.ndarray) -> Any:
    """An `IsotonicRegression` (`y_min` 0, `y_max` 1, increasing, clip) with the given thresholds, rebuilt the way
    scikit-learn's own `__setstate__` does (`_build_f`), so `predict` is unchanged."""
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
    iso.X_thresholds_ = np.asarray(x, dtype=np.float64)
    iso.y_thresholds_ = np.asarray(y, dtype=np.float64)
    iso.X_min_, iso.X_max_ = np.float64(bounds[0]), np.float64(bounds[1])
    iso.increasing_ = True
    iso.n_features_in_ = 1
    iso._build_f(iso.X_thresholds_, iso.y_thresholds_)
    return iso


# ---- file format -------------------------------------------------------------------------------------------------


def write_model_npz(path: str | os.PathLike[str], meta: dict[str, Any], arrays: dict[str, np.ndarray]) -> None:
    """Writes `meta.json` then the arrays in name order as stored ZIP members with a fixed timestamp."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as z:
        blob = (json.dumps(meta, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
        z.writestr(_info(META_MEMBER), blob)
        for name in sorted(arrays):
            arr = np.ascontiguousarray(arrays[name])
            if arr.dtype.hasobject:
                raise _engine_error(f"model.npz: {name} has an object dtype")
            buf = io.BytesIO()
            np.lib.format.write_array(buf, arr, version=(1, 0), allow_pickle=False)
            z.writestr(_info(f"{name}.npy"), buf.getvalue())


def _info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    info.create_system = 3  # fixed (unix), so the bytes do not depend on the writing platform
    return info


def read_model_npz(blob: bytes, what: str = MODEL_NAME) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """`(meta, arrays)` of a `model.npz` held in memory; `QT-INPUT-002` for anything but stored `meta.json` and
    `.npy` members of plain (non-object) dtypes."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        infos = z.infolist()
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as err:
        raise input_error(f"{what}: not a model archive ({err})") from None
    names = [i.filename for i in infos]
    if len(set(names)) != len(names):
        raise input_error(f"{what}: duplicate archive members")
    if META_MEMBER not in names:
        raise input_error(f"{what}: {META_MEMBER} missing")
    total = 0
    meta_size = 0
    for i in infos:
        if i.compress_type != zipfile.ZIP_STORED or i.flag_bits & 0x1:
            raise input_error(f"{what}: member {i.filename!r} is compressed or encrypted (only stored members)")
        if i.filename != META_MEMBER and not (i.filename.endswith(".npy") and "/" not in i.filename):
            raise input_error(f"{what}: unexpected member {i.filename!r}")
        total += i.file_size
        if i.filename == META_MEMBER:
            meta_size = i.file_size
    if total > len(blob):
        raise input_error(f"{what}: member sizes exceed the archive size")
    if meta_size > MAX_META_BYTES:
        raise input_error(f"{what}: {META_MEMBER} is {meta_size} bytes (limit {MAX_META_BYTES})")
    try:
        meta = json.loads(z.read(META_MEMBER).decode("utf-8"))
        arrays = {}
        for i in infos:
            if i.filename == META_MEMBER:
                continue
            arrays[i.filename[: -len(".npy")]] = _read_npy(z.read(i.filename), f"{what}: {i.filename}")
    except (ValueError, OSError, EOFError, UnicodeDecodeError, zipfile.BadZipFile, RecursionError) as err:
        raise input_error(f"{what}: unreadable member ({type(err).__name__}: {err})") from None
    if not isinstance(meta, dict):
        raise input_error(f"{what}: {META_MEMBER} is not an object")
    return meta, arrays


def _read_npy(data: bytes, what: str) -> np.ndarray:
    """One `.npy` (format 1.0) parsed without pickle: the header is a literal (numpy's `safe_eval`), object dtypes
    are refused and the payload must be exactly `prod(shape) * itemsize` bytes (no allocation from a claimed shape
    before the size is checked)."""
    f = io.BytesIO(data)
    if np.lib.format.read_magic(f) != (1, 0):
        raise input_error(f"{what}: only .npy format 1.0 is accepted")
    shape, fortran, dtype = np.lib.format.read_array_header_1_0(f)
    if dtype.hasobject:
        raise input_error(f"{what}: object dtype refused")
    count = math.prod(shape)
    offset = f.tell()
    if count * dtype.itemsize != len(data) - offset:
        raise input_error(f"{what}: payload is {len(data) - offset} bytes, header promises {count * dtype.itemsize}")
    arr = np.frombuffer(data, dtype=dtype, count=count, offset=offset).copy()
    return arr.reshape(shape[::-1]).T if fortran else arr.reshape(shape)


def _need(cond: bool, what: str, message: str) -> None:
    if not cond:
        raise input_error(f"{what}: {message}")


def _array(arrays: dict[str, np.ndarray], name: str, dtype: str, ndim: int, what: str) -> np.ndarray:
    a = arrays.get(name)
    _need(a is not None, what, f"array {name} missing")
    assert a is not None
    _need(a.dtype == np.dtype(dtype), what, f"array {name} has dtype {a.dtype}, expected {np.dtype(dtype)}")
    _need(a.ndim == ndim, what, f"array {name} has {a.ndim} dimensions, expected {ndim}")
    return a


def validate_trees(arrays: dict[str, np.ndarray], n_trees: int, n_features: int, what: str) -> None:
    """Structural checks of the tree arrays (module docstring); `QT-INPUT-002` naming the first failure."""
    offsets = _array(arrays, "tree_offsets", "<i8", 1, what)
    _need(offsets.size == n_trees + 1, what, f"tree_offsets has {offsets.size} entries, expected {n_trees + 1}")
    _need(int(offsets[0]) == 0 and bool(np.all(np.diff(offsets) >= 1)), what, "tree_offsets not increasing from 0")
    n_nodes = int(offsets[-1])
    cols = {name: _array(arrays, f"node.{name}", dt, 1, what) for name, dt in NODE_FIELDS.items()}
    for name, a in cols.items():
        _need(a.size == n_nodes, what, f"node.{name} has {a.size} entries, expected {n_nodes}")
    tree_of = np.repeat(np.arange(n_trees, dtype=np.int64), np.diff(offsets))
    local = np.arange(n_nodes, dtype=np.int64) - offsets[tree_of]
    size = np.diff(offsets)[tree_of]
    leaf = cols["is_leaf"]
    _need(bool(np.all(leaf <= 1)) and bool(np.all(cols["missing_go_to_left"] <= 1)), what, "flags not 0 or 1")
    _need(not bool(np.any(cols["is_categorical"])), what, "categorical splits are not supported")
    split = leaf == 0
    for side in ("left", "right"):
        child = cols[side].astype(np.int64)
        ok = (child[split] > local[split]) & (child[split] < size[split])
        _need(bool(np.all(ok)), what, f"a split node's {side} child is outside its tree or not after it")
    feat = cols["feature_idx"][split]
    _need(bool(np.all((feat >= 0) & (feat < n_features))), what, f"a split feature is outside 0..{n_features - 1}")
    _need(not bool(np.any(np.isnan(cols["num_threshold"][split]))), what, "a split threshold is NaN")
    _need(bool(np.all(np.isfinite(cols["value"][leaf == 1]))), what, "a leaf value is not finite")
    # Every tree's root (local 0) must exist; the children rule above makes every walk end at a leaf.


def validate_isotonic(arrays: dict[str, np.ndarray], j: int, what: str) -> None:
    """Shape and value checks of one isotonic layer. The clip range must be the first and last threshold, as
    scikit-learn's own fit leaves it (`X_min_` / `X_max_` are the smallest and largest distinct inputs, which the
    threshold trimming keeps): a wider range would clip inputs to points outside the interpolation table, where
    `predict` returns NaN."""
    x = _array(arrays, f"iso.{j}.x", "<f8", 1, what)
    y = _array(arrays, f"iso.{j}.y", "<f8", 1, what)
    b = _array(arrays, f"iso.{j}.bounds", "<f8", 1, what)
    _need(x.size >= 1 and x.size == y.size and b.size == 2, what, f"isotonic layer {j} has inconsistent sizes")
    _need(bool(np.all(np.isfinite(x)) and np.all(np.isfinite(y)) and np.all(np.isfinite(b))), what,
          f"isotonic layer {j} has non-finite values")  # fmt: skip
    _need(bool(np.all(np.diff(x) > 0)) and bool(np.all(np.diff(y) >= 0)), what, f"isotonic layer {j} not increasing")
    _need(bool(np.all((y >= 0) & (y <= 1))), what, f"isotonic layer {j} out of range")
    _need(
        bool(b[0] == x[0] and b[1] == x[-1]), what, f"isotonic layer {j} clip range is not its first and last threshold"
    )


def sha256_hex(blob: bytes) -> str:
    """Lower-case hex sha256 of bytes."""
    return hashlib.sha256(blob).hexdigest()


def load_legacy_pickle(blob: bytes) -> dict[str, Any]:
    """Unpickles an authenticated v0.1 `model.pkl` (callers verify the sha256 and the explicit opt-in first) and
    returns its state with the estimator as a `TreeEnsemble` and `fitted_with` (the scikit-learn version that pickled
    it: the installed one unless scikit-learn reports another); warns `LegacyPickleWarning`."""
    import pickle

    import sklearn
    from sklearn.exceptions import InconsistentVersionWarning

    warnings.warn(
        "unpickling a legacy model.pkl (allow_pickle=True): unpickling runs code from the file; migrate it with "
        "`python -m queuetruth.model.migrate <dir> --allow-pickle --sha256 <digest>` and load model.npz instead",
        LegacyPickleWarning,
        stacklevel=3,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        state = pickle.loads(blob)  # authenticated by sha256 and explicitly allowed by the caller
    fitted_with = f"scikit-learn {sklearn.__version__}"
    for w in caught:
        if isinstance(w.message, InconsistentVersionWarning):
            fitted_with = f"scikit-learn {w.message.original_sklearn_version}"
        warnings.warn_explicit(w.message, w.category, w.filename, w.lineno)
    if not isinstance(state, dict) or state.get("format_version") != LEGACY_FORMAT_VERSION:
        raise input_error(f"{LEGACY_PICKLE_NAME}: unsupported format")
    est = state.get("estimator")
    if est is None or not hasattr(est, "_predictors"):
        raise input_error(f"{LEGACY_PICKLE_NAME}: no fitted estimator")
    return {**state, "estimator": TreeEnsemble.from_sklearn(est), "fitted_with": fitted_with}


__all__ = [
    "LEGACY_PICKLE_NAME",
    "MODEL_NAME",
    "STATE_FORMAT_VERSION",
    "STATE_SCHEMA",
    "LegacyPickleWarning",
    "TreeEnsemble",
    "allotted_cpus",
    "effective_threads",
    "isotonic_arrays",
    "isotonic_from_arrays",
    "load_legacy_pickle",
    "read_model_npz",
    "validate_isotonic",
    "validate_trees",
    "write_model_npz",
]
