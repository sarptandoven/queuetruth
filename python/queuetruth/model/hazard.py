"""`HazardModel`: discrete-time competing-risk hazard on person-period rows (SPEC 5.5.4-5.5.6, 5.5.8).

Inputs per person-period row are the model features (`model_feature_names`) plus `interval_idx` and `log_tau_end`;
`HistGradientBoostingClassifier` (3 classes: none / fill / cancel, `early_stopping=False`, `random_state=seed`) gives
the hazards. Raw CIFs follow SPEC 5.5.4. Recalibration (`isotonic`): one `IsotonicRegression` per grid point, fitted
on the calib split with IPCW sample weights `1 / max(G(min(e, tau_j)-), 0.05)` (`G` = KM of censoring on the calib
split, rows censored before `tau_j` excluded), then `cif_j = max(cif_j, cif_{j-1})` clipped to [0, 1].
`cif_cancel` is the raw cancel CIF, scaled per row by one constant only when the recalibrated `cif_fill(tau_J)` plus
the raw `cif_cancel(tau_J)` exceeds 1, so both CIFs stay monotone and `cif_fill + cif_cancel <= 1`; `survival` is
`1 - cif_fill - cif_cancel`.

Persistence (`queuetruth.model.persist`): `model.npz` (no pickle: JSON header plus `.npy` arrays of the trees and
isotonic layers, read without executing code and checked structurally before use) and `model.json`
(`qt.hazard_model_card`, which records the sha256 of `model.npz`). `HazardModel.load` verifies that digest (and the
caller's trusted `sha256` when given). A legacy v0.1 `model.pkl` is unpickled only with an explicit
`allow_pickle=True` plus a trusted sha256 (or `trusted=True`), with a `LegacyPickleWarning`; migrate it once with
`python -m queuetruth.model.migrate <dir> --allow-pickle --sha256 <digest>`.

Prediction: the fitted GBDT is held as a `persist.TreeEnsemble` (scikit-learn's own tree predictors and loss link,
bit-identical to `HistGradientBoostingClassifier.predict_proba`), so a loaded model predicts exactly what the fitted
one did. Each kept row of the split table is predicted at most once and cached: `fit` predicts the calib and test
rows (the isotonic layers and the test scores need them), `predict_split` adds the train rows only when the
predictions table is written (every step after the hazards is row-wise, so a slice of the cache equals predicting
that slice again; tested). The GBDT fit runs on `fit_threads()` OpenMP threads (`fit(threads=)` or `OMP_NUM_THREADS`
when given, else the allotted CPUs not already busy); scikit-learn's fit does not depend on the thread count
(tested), so this changes the wall time only.

Spec contract (E40): every `HazardFitSpec` field is read; the one combination where a field cannot take effect,
`split.n_blocks` other than its default with `split.scheme = days` (the days scheme has no blocks), raises
`QT-CONFIG-001` naming the field at `fit` (`check_spec`). With `split.scheme = auto` the scheme depends on the number
of session dates, so a non-default `n_blocks` that the resolved `days` scheme does not use is a model warning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

from queuetruth.errors import QtError
from queuetruth.model._common import (
    CENSOR,
    FILL,
    PRIVILEGED_PREFIX,
    check_grid,
    config_error,
    float_matrix,
    input_error,
    known_status,
    model_feature_names,
    outcome_arrays,
    require_columns,
)
from queuetruth.model.persist import (
    MAX_MODEL_BYTES,
    MODEL_NAME,
    NODE_FIELDS,
    STATE_FORMAT_VERSION,
    STATE_SCHEMA,
    TreeEnsemble,
    allotted_cpus,
    effective_threads,
    isotonic_arrays,
    isotonic_from_arrays,
    load_legacy_pickle,
    read_model_npz,
    validate_isotonic,
    validate_trees,
    write_model_npz,
)
from queuetruth.model.person_period import expand_indices, log_tau_end
from queuetruth.model.scoring import censoring_km, cif_from_hazards
from queuetruth.model.split import (
    CALIB,
    SPLIT_NAMES,
    TEST,
    TRAIN,
    SplitResult,
    check_split,
    split_assignment,
    split_column,
)
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.runid import spec_echo
from queuetruth.schemas.specs import HazardFitSpec, SplitSpec

#: `format_version` of the model card (`qt.hazard_model_card`); the model file has its own (`STATE_FORMAT_VERSION`).
FORMAT_VERSION = 1
#: Legacy v0.1 model file (pickle), read only with `allow_pickle=True`.
PICKLE_NAME = "model.pkl"
CARD_NAME = "model.json"
PREDICTIONS_NAME = "predictions.parquet"
_CHUNK_ROWS = 20_000


def fit_threads(requested: int | None = None) -> int:
    """OpenMP threads for the GBDT fit (execution only: the fit's result does not depend on it, scikit-learn's
    histogram GBDT being deterministic across thread counts; tested).

    - `requested` when given (`HazardModel.fit(threads=)`), else `OMP_NUM_THREADS` when set (scikit-learn's own
      count);
    - else scikit-learn's count (physical cores within the CPU affinity and cgroup quota) when the process is
      confined to fewer logical CPUs than the host has, since the host-wide load average says nothing about them;
    - else the CPUs not already busy: `os.cpu_count()` minus the 1-minute load average, at least 1 (scikit-learn's
      count when the platform has no load average).

    Oversubscribing a busy machine makes the fit's many short parallel regions wait on descheduled threads
    (measured in docs/design/extensions/model-perf.md). The load average lags by about a minute and counts this
    process's own earlier work, so right after a multi-threaded stage the fit may run on fewer threads than are
    free; pass `threads` (or set `OMP_NUM_THREADS`) to fix the count."""
    if requested is not None:
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
            raise config_error(f"fit-hazard: threads must be a positive integer, got {requested!r}", field="threads")
        return requested
    n = effective_threads()
    if os.environ.get("OMP_NUM_THREADS"):
        return n
    host = os.cpu_count() or n
    if allotted_cpus() < host:
        return n
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):  # no load average on this platform
        return n
    return max(1, min(n, int(host - load)))


@contextmanager
def _openmp_threads(n: int) -> Iterator[None]:
    from threadpoolctl import threadpool_limits

    with threadpool_limits(limits=n, user_api="openmp"):
        yield


def check_spec(spec: HazardFitSpec) -> None:
    """`QT-CONFIG-001` for a non-default field that cannot take effect (E40): `split.n_blocks` with
    `split.scheme = days`."""
    default_blocks = SplitSpec().n_blocks
    if spec.split.scheme == "days" and spec.split.n_blocks != default_blocks:
        raise config_error(
            f"fit-hazard: split.n_blocks = {spec.split.n_blocks} has no effect with split.scheme = days (whole "
            f"session dates, no blocks); use scheme blocks or auto, or leave n_blocks at {default_blocks}",
            field="split.n_blocks",
        )


class HazardModel:
    """Gradient-boosted hazard model with isotonic recalibration; `predict` returns `qt.hazard_predictions`."""

    def __init__(self, spec: HazardFitSpec | dict[str, Any]) -> None:
        self.spec = spec if isinstance(spec, HazardFitSpec) else HazardFitSpec.model_validate(spec)
        self.grid_ns = check_grid(self.spec.grid_ns)
        self.feature_names: list[str] = []
        self.estimator: TreeEnsemble | None = None
        #: The scikit-learn estimator fitted in this process (None for a loaded model); `estimator` predicts.
        self.sklearn_estimator: HistGradientBoostingClassifier | None = None
        self.isotonic: list[IsotonicRegression | None] = []
        self.n_rows_train = 0
        self.warnings: list[str] = []
        self.split_result: SplitResult | None = None
        self.split_features: pa.Table | None = None
        self.card: Any = None
        #: scikit-learn version that fitted the GBDT ("scikit-learn X.Y.Z"), kept through save, load and migrate.
        self.fitted_with: str | None = None
        #: OpenMP threads the last `fit` in this process used (execution only, not saved).
        self.fit_threads_used: int | None = None
        # Hazard cache of `split_features` (fit in this process only): design matrix, h_fill, h_cancel, rows filled.
        self._cache: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None

    # ---- design ------------------------------------------------------------------------------------------------

    def _design(self, x: np.ndarray, interval: np.ndarray) -> np.ndarray:
        lte = log_tau_end(self.grid_ns)
        return np.column_stack([x, interval.astype(np.float64), lte[interval - 1]])

    def _require_fitted(self) -> TreeEnsemble:
        if self.estimator is None:
            raise QtError("QT-USAGE-001", "HazardModel is not fitted")
        return self.estimator

    def hazards(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Raw per-interval hazards `(h_fill, h_cancel)`, shape (rows, J), for a feature matrix in model order."""
        est = self._require_fitted()
        n, j_count = x.shape[0], len(self.grid_ns)
        hf = np.zeros((n, j_count))
        hc = np.zeros((n, j_count))
        classes = [int(k) for k in est.classes_]
        for lo in range(0, n, _CHUNK_ROWS):
            hi = min(n, lo + _CHUNK_ROWS)
            m = hi - lo
            rows = np.repeat(np.arange(lo, hi), j_count)
            interval = np.tile(np.arange(1, j_count + 1), m)
            proba = est.predict_proba(self._design(x[rows], interval)).reshape(m, j_count, len(classes))
            if 1 in classes:
                hf[lo:hi] = proba[:, :, classes.index(1)]
            if 2 in classes:
                hc[lo:hi] = proba[:, :, classes.index(2)]
        return hf, hc

    def matrix(self, features: pa.Table) -> np.ndarray:
        """The model's feature matrix of a features table (QT-INPUT-002 if a feature column is missing)."""
        require_columns(features, self.feature_names, "features")
        return float_matrix(features, self.feature_names)

    def predict_arrays(self, features: pa.Table) -> dict[str, np.ndarray]:
        """`h_fill`, `h_cancel`, `cif_fill_raw`, `cif_fill`, `cif_cancel`, `survival`, each (rows, J)."""
        hf, hc = self.hazards(self.matrix(features))
        return self._arrays(hf, hc)

    def split_arrays(self, mask: np.ndarray | None = None) -> dict[str, np.ndarray]:
        """`predict_arrays(split_features)` (only the rows where the boolean `mask` is true when given), each row
        predicted at most once per fit and cached; equal to predicting those rows again. In-process only: a model
        that was loaded, not fitted here, has no split table (`QT-USAGE-001`)."""
        hf, hc = self._split_hazards(mask, "split_arrays")
        return self._arrays(hf, hc)

    def _split_hazards(self, mask: np.ndarray | None, caller: str) -> tuple[np.ndarray, np.ndarray]:
        """Hazards of the `mask` rows of `split_features` (all rows when None), predicting the uncached ones."""
        if self._cache is None:
            raise QtError("QT-USAGE-001", f"HazardModel.{caller}: needs a model fitted in this process")
        x, hf, hc, have = self._cache
        if mask is None:
            need = np.flatnonzero(~have)
        else:
            mask = np.asarray(mask, dtype=bool)
            if mask.shape != have.shape:
                raise QtError("QT-USAGE-001", f"HazardModel.{caller}: mask has shape {mask.shape}, need {have.shape}")
            need = np.flatnonzero(mask & ~have)
        if need.size:
            hf[need], hc[need] = self.hazards(x[need])
            have[need] = True
        return (hf, hc) if mask is None else (hf[mask], hc[mask])

    def _arrays(self, hf: np.ndarray, hc: np.ndarray) -> dict[str, np.ndarray]:
        cif_raw, cc_raw, _ = cif_from_hazards(hf, hc)
        cif, cc, surv = self._calibrated(cif_raw, cc_raw)
        return {
            "h_fill": hf,
            "h_cancel": hc,
            "cif_fill_raw": cif_raw,
            "cif_fill": cif,
            "cif_cancel": cc,
            "survival": surv,
        }

    def _calibrated(self, cif_raw: np.ndarray, cc_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        cif = cif_raw.copy()
        if self.spec.recalibration == "isotonic":
            for j, iso in enumerate(self.isotonic):
                if iso is not None:
                    cif[:, j] = iso.predict(cif_raw[:, j])
        cif = np.clip(np.maximum.accumulate(cif, axis=1), 0.0, 1.0)
        last_c = cc_raw[:, -1]
        over = (last_c > 0) & (cif[:, -1] + last_c > 1.0)
        factor = np.ones(len(cif))
        factor[over] = (1.0 - cif[over, -1]) / last_c[over]
        cc = np.clip(cc_raw * factor[:, None], 0.0, 1.0)
        surv = np.clip(1.0 - cif - cc, 0.0, 1.0)
        return cif, cc, surv

    # ---- fit ---------------------------------------------------------------------------------------------------

    def fit(self, features: pa.Table, *, threads: int | None = None) -> HazardModel:
        """Splits (purge / embargo), fits the GBDT on the train person-period rows and the isotonic layers on the
        calib split. Keeps the split table (`split_features`) and assignment (`split_result`) for scoring.
        `threads` (execution only) fixes the GBDT's OpenMP threads (`fit_threads`). A fit that raises leaves the
        model unfitted (no estimator, no split table, no cached hazards from an earlier fit)."""
        self._reset()
        try:
            self._fit(features, threads)
        except BaseException:
            self._reset()
            raise
        return self

    def _fit(self, features: pa.Table, threads: int | None) -> None:
        sp = self.spec
        check_spec(sp)
        self.feature_names = model_feature_names(features, sp.privileged)
        if sp.privileged and not any(n.startswith(PRIVILEGED_PREFIX) for n in self.feature_names):
            raise input_error("privileged = true but the features have no p_* columns")
        e_all, _ = outcome_arrays(features)
        h_max = max(int(self.grid_ns[-1]), int(e_all.max(initial=0)))
        res = split_assignment(features, sp.split, h_max_ns=h_max)
        check_split(features, res)
        if res.scheme != sp.split.scheme:
            self.warnings.append(f"split scheme {sp.split.scheme} resolved to {res.scheme}")
        if res.scheme == "days" and sp.split.n_blocks != SplitSpec().n_blocks:
            self.warnings.append(
                f"split.n_blocks = {sp.split.n_blocks} has no effect: split.scheme {sp.split.scheme} resolved to days "
                "(whole session dates, no blocks)"
            )
        base = features.drop_columns(["split"]) if "split" in features.column_names else features
        tbl = base.append_column("split", split_column(res.label)).filter(pa.array(res.keep))
        self.split_result, self.split_features = res, tbl
        label = res.label[res.keep]
        e, c = outcome_arrays(tbl)
        x = self.matrix(tbl)

        train = np.flatnonzero(label == TRAIN)
        rows, interval, target = expand_indices(e[train], c[train], self.grid_ns)
        self.n_rows_train = len(rows)
        if len(rows) == 0 or not np.any(target == FILL):
            raise input_error("the train split has no person-period rows with a fill event")
        est = HistGradientBoostingClassifier(
            loss="log_loss",
            learning_rate=sp.gbdt.learning_rate,
            max_iter=sp.gbdt.max_iter,
            max_leaf_nodes=sp.gbdt.max_leaf_nodes,
            min_samples_leaf=sp.gbdt.min_samples_leaf,
            l2_regularization=sp.gbdt.l2_regularization,
            early_stopping=False,
            random_state=sp.seed,
        )
        self.fit_threads_used = fit_threads(threads)
        with _openmp_threads(self.fit_threads_used):
            est.fit(self._design(x[train][rows], interval), target.astype(np.int64))
        self.sklearn_estimator = est
        self.estimator = TreeEnsemble.from_sklearn(est)
        self.fitted_with = f"scikit-learn {sklearn.__version__}"
        if not np.any(target == 2):
            self.warnings.append("the train split has no cancel events: cancel hazards are 0")

        # Each kept row is predicted at most once: the calib and test rows now (isotonic layers, test scores), the
        # train rows only when the predictions table is asked for (`predict_split`).
        n, j_count = len(label), len(self.grid_ns)
        self._cache = (x, np.zeros((n, j_count)), np.zeros((n, j_count)), np.zeros(n, dtype=bool))
        self._split_hazards((label == CALIB) | (label == TEST), "fit")
        self.isotonic = [None] * j_count
        if sp.recalibration == "isotonic":
            calib = label == CALIB
            if not calib.any():
                self.warnings.append("calib split is empty: recalibration skipped")
            else:
                hf, hc = self._split_hazards(calib, "fit")
                self._fit_isotonic(hf, hc, e[calib], c[calib])

    def _reset(self) -> None:
        """Back to the unfitted state (start of `fit`, and a fit that raised)."""
        self.warnings = []
        self.feature_names = []
        self.estimator = self.sklearn_estimator = None
        self.isotonic = []
        self.n_rows_train = 0
        self.split_result = self.split_features = None
        self.fitted_with = self.fit_threads_used = None
        self.card = None
        self._cache = None

    def _fit_isotonic(self, hf: np.ndarray, hc: np.ndarray, e: np.ndarray, c: np.ndarray) -> None:
        cif_raw, _, _ = cif_from_hazards(hf, hc)
        g = censoring_km(e, c)
        for j, tau in enumerate(self.grid_ns):
            tau = int(tau)
            _, _, censored_before = known_status(e, c, tau)
            use = ~censored_before
            if not use.any():
                self.warnings.append(f"calib split has no rows with known status at {tau} ns: identity layer")
                continue
            y = ((c == FILL) & (e <= tau))[use].astype(np.float64)
            w = g.weight_left(np.minimum(e[use], tau))
            iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
            self.isotonic[j] = iso.fit(cif_raw[use, j], y, sample_weight=w)

    # ---- predict -----------------------------------------------------------------------------------------------

    def predict(self, features: pa.Table) -> pa.Table:
        """`qt.hazard_predictions`: one row per input row and grid horizon (input order, then horizon). `split` is
        taken from the input's `split` column when present, else `test`; `y_fill` / `censored` need `event_ns` and
        `cause` (else null / false)."""
        require_columns(features, ("order_ref", "landmark_ns"), "features")
        return self._prediction_table(features, self.predict_arrays(features))

    def predict_split(self) -> pa.Table:
        """`predict(split_features)` through the hazard cache of `fit` (`split_arrays`: only rows not yet predicted
        are predicted). In-process only (`QT-USAGE-001` for a model that was loaded, not fitted here)."""
        if self.split_features is None:
            raise QtError("QT-USAGE-001", "HazardModel.predict_split: needs a model fitted in this process")
        return self._prediction_table(self.split_features, self.split_arrays())

    def _prediction_table(self, features: pa.Table, arr: dict[str, np.ndarray]) -> pa.Table:
        n, j_count = features.num_rows, len(self.grid_ns)
        rep = pa.array(np.repeat(np.arange(n, dtype=np.int64), j_count))
        horizon = np.tile(self.grid_ns, n)
        if "event_ns" in features.column_names and "cause" in features.column_names:
            e, c = outcome_arrays(features)
            e_r, c_r = np.repeat(e, j_count), np.repeat(c, j_count)
            censored = (c_r == CENSOR) & (e_r < horizon)
            y = (c_r == FILL) & (e_r <= horizon)
            y_fill = pa.array(y, mask=censored, type=pa.bool_())
        else:
            censored = np.zeros(n * j_count, dtype=bool)
            y_fill = pa.nulls(n * j_count, type=pa.bool_())
        if "split" in features.column_names:
            names = features.column("split").combine_chunks().cast(pa.utf8()).to_pylist()
            lut = {s: i for i, s in enumerate(SPLIT_NAMES)}
            try:
                split_codes = np.array([lut[s] for s in names], dtype=np.int8)
            except KeyError as err:
                raise input_error(f"split: unknown value {err.args[0]!r}") from None
        else:
            split_codes = np.full(n, TEST, dtype=np.int8)
        cols = {
            "order_ref": features.column("order_ref").take(rep),
            "landmark_ns": features.column("landmark_ns").take(rep),
            "horizon_ns": pa.array(horizon, type=pa.int64()),
            "cif_fill_raw": pa.array(arr["cif_fill_raw"].reshape(-1)),
            "cif_fill": pa.array(arr["cif_fill"].reshape(-1)),
            "cif_cancel": pa.array(arr["cif_cancel"].reshape(-1)),
            "survival": pa.array(arr["survival"].reshape(-1)),
            "y_fill": y_fill,
            "censored": pa.array(censored),
            "split": split_column(np.repeat(split_codes, j_count)),
        }
        schema = SCHEMAS["qt.hazard_predictions"]
        return pa.Table.from_arrays([cols[f.name].cast(f.type) for f in schema], schema=schema)

    # ---- persistence -------------------------------------------------------------------------------------------

    def state(self) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
        """`(meta, arrays)` of `model.npz` (`queuetruth.model.persist`). `fitted_with` is the version that fitted the
        model (kept through load, migrate and re-save), not the one saving it."""
        est = self._require_fitted()
        arrays = est.to_arrays()
        layers = []
        for j, iso in enumerate(self.isotonic):
            if iso is not None:
                arrays.update(isotonic_arrays(j, iso))
                layers.append(j)
        meta = {
            "schema": STATE_SCHEMA,
            "format_version": STATE_FORMAT_VERSION,
            "spec": spec_echo(self.spec.model_dump(mode="json")),
            "feature_names": list(self.feature_names),
            "grid_ns": [int(t) for t in self.grid_ns],
            "n_rows_train": int(self.n_rows_train),
            "estimator": {
                "family": "sklearn.HistGradientBoostingClassifier",
                "fitted_with": self.fitted_with or f"scikit-learn {sklearn.__version__}",
                "classes": [int(k) for k in est.classes_],
                "n_iter": est.n_iter,
                "n_trees_per_iteration": est.n_trees_per_iteration,
                "n_features": est.n_features,
            },
            "isotonic": layers,
        }
        return meta, arrays

    def save(self, out_dir: str) -> None:
        """Writes `model.npz` and records its sha256 in the model card (`model.json`, written or updated)."""
        from queuetruth.model.card import write_card

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        meta, arrays = self.state()
        tmp = out / f".{MODEL_NAME}.tmp"
        try:
            write_model_npz(tmp, meta, arrays)
            tmp.replace(out / MODEL_NAME)  # a reader never sees a half-written model.npz
        finally:
            tmp.unlink(missing_ok=True)
        write_card(self, out)

    @classmethod
    def load(
        cls, path: str, *, sha256: str | None = None, trusted: bool = False, allow_pickle: bool = False
    ) -> HazardModel:
        """Loads a saved model. `path` is the output directory or a file in it (`model.json`, `model.npz`).

        `model.npz` is read without executing code; its bytes must match the sha256 the card records (integrity)
        and `sha256` when given (authenticity: the digest `fit_hazard` returned, `model_sha256(card)`), else
        `QT-INPUT-002`. `trusted` does not apply to it. Without `sha256` the load succeeds, but whoever can write the
        directory can replace the model and its card together, so `warnings` then says the model is not
        authenticated. A model fitted under another scikit-learn version than the installed one also says so in
        `warnings` (the tree format is scikit-learn's own).

        A legacy v0.1 directory (`model.pkl`) is refused with `QT-CONFIG-001` unless `allow_pickle=True`; then, as in
        v0.1, unpickling also needs the trusted `sha256` of `model.pkl` or `trusted=True` (a directory only you can
        write), the pickle is read once and the hashed bytes are the bytes unpickled, and `LegacyPickleWarning` is
        emitted. Migrate such a directory once with `python -m queuetruth.model.migrate`."""
        from queuetruth.schemas.models import HazardModelCard

        if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise config_error(f"HazardModel.load: sha256 must be 64 hex characters, got {sha256!r}")
        p = Path(path)
        out = p if p.is_dir() else p.parent
        card_path = out / CARD_NAME
        if not card_path.exists():
            raise QtError("QT-INPUT-001", f"HazardModel.load: {CARD_NAME} is required in {out}")
        try:
            card = HazardModelCard.model_validate(json.loads(card_path.read_text(encoding="utf-8")))
        except (ValueError, RecursionError) as err:  # RecursionError: JSON nested too deep
            raise input_error(f"HazardModel.load: invalid {CARD_NAME}: {type(err).__name__}: {err}") from None
        listed = {f.name: f.sha256 for f in card.files}
        if MODEL_NAME in listed:
            blob = _read_verified(out / MODEL_NAME, listed[MODEL_NAME], sha256)
            meta, arrays = read_model_npz(blob)
            model = cls._from_state(meta, arrays, card)
            if sha256 is None:
                model.warnings.append(
                    f"{MODEL_NAME} not authenticated: loaded without a trusted sha256 (its digest matches only the "
                    f"{CARD_NAME} in the same directory)"
                )
        elif PICKLE_NAME in listed:
            if not allow_pickle:
                raise config_error(
                    f"HazardModel.load: {out} holds a legacy {PICKLE_NAME} (v0.1); unpickling runs code from the "
                    f"file, so it is loaded only with allow_pickle=True and a trusted sha256 (or trusted=True for a "
                    f"directory only you can write); convert it once with `python -m queuetruth.model.migrate {out} "
                    f"--allow-pickle --sha256 <digest>` and load {MODEL_NAME}",
                    field="allow_pickle",
                )
            if sha256 is None and not trusted:
                raise config_error(
                    f"HazardModel.load: refusing to unpickle {PICKLE_NAME} without a trusted sha256: pass "
                    f"sha256=<digest recorded by fit_hazard> (or trusted=True for a directory only you can write); the "
                    f"{CARD_NAME} beside the pickle is not an authenticity check"
                )
            blob = _read_verified(out / PICKLE_NAME, listed[PICKLE_NAME], sha256)
            state = load_legacy_pickle(blob)  # authenticated above (trusted digest or explicit trusted=True)
            model = cls(HazardFitSpec.model_validate(state["spec"]))
            model.feature_names = list(state["feature_names"])
            model.grid_ns = check_grid(state["grid_ns"])
            model.estimator = state["estimator"]
            model.isotonic = list(state["isotonic"])
            model.n_rows_train = int(state["n_rows_train"])
            model.fitted_with = str(state["fitted_with"])
            model.warnings.append(f"loaded from a legacy {PICKLE_NAME} (allow_pickle=True); migrate it to {MODEL_NAME}")
        else:
            raise input_error(f"HazardModel.load: {CARD_NAME} lists neither {MODEL_NAME} nor {PICKLE_NAME}")
        here = f"scikit-learn {sklearn.__version__}"
        if model.fitted_with != here:
            model.warnings.append(
                f"fitted with {model.fitted_with}, loaded with {here}: predictions are tested only under the fitting "
                "version"
            )
        model.card = card
        return model

    @classmethod
    def _from_state(cls, meta: dict[str, Any], arrays: dict[str, np.ndarray], card: Any) -> HazardModel:
        """A model from validated `model.npz` contents; `QT-INPUT-002` naming the first inconsistency."""
        what = MODEL_NAME
        try:
            if meta.get("schema") != STATE_SCHEMA or meta.get("format_version") != STATE_FORMAT_VERSION:
                raise input_error(
                    f"{what}: schema {meta.get('schema')!r} format {meta.get('format_version')!r}, expected "
                    f"{STATE_SCHEMA!r} format {STATE_FORMAT_VERSION}"
                )
            spec = HazardFitSpec.model_validate(meta["spec"])
            names = meta["feature_names"]
            if not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
                raise input_error(f"{what}: feature_names must be a list of strings")
            grid_list = _ints(meta["grid_ns"], "grid_ns")
            if names != list(card.feature_names) or grid_list != list(card.grid_ns):
                raise input_error(f"{what}: feature names or grid differ from {CARD_NAME}")
            grid = check_grid(grid_list)
            est_meta = meta["estimator"]
            if not isinstance(est_meta, dict):
                raise input_error(f"{what}: estimator must be an object")
            classes = _ints(est_meta["classes"], "estimator.classes")
            k = _int(est_meta["n_trees_per_iteration"], "estimator.n_trees_per_iteration")
            n_iter = _int(est_meta["n_iter"], "estimator.n_iter")
            n_features = _int(est_meta["n_features"], "estimator.n_features")
            if sorted(set(classes)) != classes or not set(classes) <= {0, 1, 2} or len(classes) < 2:
                raise input_error(f"{what}: classes {classes} are not increasing labels among 0, 1, 2")
            if k != (1 if len(classes) == 2 else len(classes)) or n_iter < 1:
                raise input_error(f"{what}: {k} trees per iteration, {n_iter} iterations for classes {classes}")
            if n_features != len(names) + 2:
                raise input_error(f"{what}: n_features {n_features} is not the {len(names)} features + 2")
            layers = _ints(meta["isotonic"], "isotonic layers")
            if sorted(set(layers)) != layers or any(j < 0 or j >= len(grid) for j in layers):
                raise input_error(f"{what}: isotonic layers {layers} are not distinct grid indexes")
            if layers and spec.recalibration != "isotonic":
                raise input_error(f"{what}: isotonic layers with recalibration {spec.recalibration!r}")
            expect = {"baseline", "tree_offsets", *(f"node.{f}" for f in NODE_FIELDS)}
            expect |= {f"iso.{j}.{part}" for j in layers for part in ("x", "y", "bounds")}
            if set(arrays) != expect:
                raise input_error(f"{what}: arrays {sorted(set(arrays) ^ expect)} missing or unexpected")
            base = arrays["baseline"]
            if base.dtype != np.dtype("<f8") or base.shape != (1, k) or not np.all(np.isfinite(base)):
                raise input_error(f"{what}: baseline must be finite float64 of shape (1, {k})")
            validate_trees(arrays, n_iter * k, n_features, what)
            for j in layers:
                validate_isotonic(arrays, j, what)
            n_rows_train = _int(meta["n_rows_train"], "n_rows_train")
            fitted_with = est_meta["fitted_with"]
            if not isinstance(fitted_with, str):
                raise input_error(f"{what}: estimator.fitted_with must be a string")
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            if isinstance(err, QtError):
                raise
            raise input_error(f"{what}: invalid model state ({type(err).__name__}: {err})") from None
        model = cls(spec)
        model.feature_names = list(names)
        model.grid_ns = grid
        model.estimator = TreeEnsemble.from_arrays(classes, arrays, n_iter * k, n_features)
        model.isotonic = [None] * len(grid)
        for j in layers:
            parts = (arrays[f"iso.{j}.{part}"] for part in ("x", "y", "bounds"))
            model.isotonic[j] = isotonic_from_arrays(*parts)
        model.n_rows_train = n_rows_train
        model.fitted_with = fitted_with
        return model


def _int(value: Any, name: str) -> int:
    """A JSON integer (not a bool) of `model.npz`'s header, else `QT-INPUT-002`."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise input_error(f"{MODEL_NAME}: {name} must be an integer, got {value!r}")
    return value


def _ints(value: Any, name: str) -> list[int]:
    """A JSON list of integers of `model.npz`'s header, else `QT-INPUT-002`."""
    if not isinstance(value, list):
        raise input_error(f"{MODEL_NAME}: {name} must be a list of integers, got {value!r}")
    return [_int(v, name) for v in value]


def _read_verified(path: Path, recorded: str, trusted: str | None) -> bytes:
    """Reads `path` once, at most `MAX_MODEL_BYTES`; its sha256 must equal `trusted` (when given) and the card's
    `recorded` digest."""
    try:
        with open(path, "rb") as f:
            blob = f.read(MAX_MODEL_BYTES + 1)  # read once: these bytes are hashed and parsed (no reopen)
    except OSError as err:
        raise QtError("QT-INPUT-001", f"HazardModel.load: cannot read {path}: {err}") from None
    if len(blob) > MAX_MODEL_BYTES:
        raise input_error(f"HazardModel.load: {path.name} is over {MAX_MODEL_BYTES} bytes; refusing")
    got = hashlib.sha256(blob).hexdigest()
    if trusted is not None and got != trusted.lower():
        raise input_error(
            f"HazardModel.load: sha256 of {path.name} is {got}, the trusted digest is {trusted.lower()}; refusing"
        )
    if got != recorded:
        raise input_error(f"HazardModel.load: sha256 of {path.name} is {got}, card records {recorded}; refusing")
    return blob


def model_sha256(card: Any) -> str | None:
    """The sha256 of the model file a `qt.hazard_model_card` lists (`model.npz`, else a legacy `model.pkl`; model or
    dict; None when neither is listed): the trusted digest to pass to `HazardModel.load` when the card comes from
    `fit_hazard` itself."""
    files = card.get("files") if isinstance(card, dict) else getattr(card, "files", None)
    found: dict[str, str | None] = {}
    for f in files or []:
        name = f.get("name") if isinstance(f, dict) else getattr(f, "name", None)
        if name in (MODEL_NAME, PICKLE_NAME):
            sha = f.get("sha256") if isinstance(f, dict) else getattr(f, "sha256", None)
            found[name] = None if sha is None else str(sha)
    return found.get(MODEL_NAME, found.get(PICKLE_NAME))


#: v0.1 name of `model_sha256` (it now returns the digest of `model.npz` when the card lists one).
pickle_sha256 = model_sha256


__all__ = [
    "CARD_NAME",
    "FORMAT_VERSION",
    "MODEL_NAME",
    "PICKLE_NAME",
    "PREDICTIONS_NAME",
    "HazardModel",
    "fit_threads",
    "model_sha256",
    "pickle_sha256",
]
