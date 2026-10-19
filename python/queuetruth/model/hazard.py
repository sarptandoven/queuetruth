"""`HazardModel`: discrete-time competing-risk hazard on person-period rows (SPEC 5.5.4-5.5.6, 5.5.8).

Inputs per person-period row are the model features (`model_feature_names`) plus `interval_idx` and `log_tau_end`;
`HistGradientBoostingClassifier` (3 classes: none / fill / cancel, `early_stopping=False`, `random_state=seed`) gives
the hazards. Raw CIFs follow SPEC 5.5.4. Recalibration (`isotonic`): one `IsotonicRegression` per grid point, fitted
on the calib split with IPCW sample weights `1 / max(G(min(e, tau_j)-), 0.05)` (`G` = KM of censoring on the calib
split, rows censored before `tau_j` excluded), then `cif_j = max(cif_j, cif_{j-1})` clipped to [0, 1].
`cif_cancel` is the raw cancel CIF, scaled per row by one constant only when the recalibrated `cif_fill(tau_J)` plus
the raw `cif_cancel(tau_J)` exceeds 1, so both CIFs stay monotone and `cif_fill + cif_cancel <= 1`; `survival` is
`1 - cif_fill - cif_cancel`.

Persistence: `model.pkl` (pickle of a plain dict: format version, spec echo, feature names, grid, estimator, isotonic
layers) and `model.json` (`qt.hazard_model_card`, which records the pickle's sha256). Unpickling runs code, so
`HazardModel.load` unpickles only an authenticated pickle: the caller passes the trusted sha256 recorded when it was
written (`pickle_sha256(card)` of the card `fit_hazard` returned, kept where the model directory's writer cannot
change it) or states `trusted=True` explicitly; the `model.json` beside the pickle is only an integrity check (whoever
can write the pickle can write the card). `model.pkl` is read once and the hashed bytes are the bytes unpickled.

Spec contract (E40): every `HazardFitSpec` field is read; the one combination where a field cannot take effect,
`split.n_blocks` other than its default with `split.scheme = days` (the days scheme has no blocks), raises
`QT-CONFIG-001` naming the field at `fit` (`check_spec`). With `split.scheme = auto` the scheme depends on the number
of session dates, so a non-default `n_blocks` that the resolved `days` scheme does not use is a model warning.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import re
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
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

FORMAT_VERSION = 1
PICKLE_NAME = "model.pkl"
CARD_NAME = "model.json"
PREDICTIONS_NAME = "predictions.parquet"
PICKLE_PROTOCOL = 5
_CHUNK_ROWS = 20_000


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
        self.estimator: HistGradientBoostingClassifier | None = None
        self.isotonic: list[IsotonicRegression | None] = []
        self.n_rows_train = 0
        self.warnings: list[str] = []
        self.split_result: SplitResult | None = None
        self.split_features: pa.Table | None = None
        self.card: Any = None

    # ---- design ------------------------------------------------------------------------------------------------

    def _design(self, x: np.ndarray, interval: np.ndarray) -> np.ndarray:
        lte = log_tau_end(self.grid_ns)
        return np.column_stack([x, interval.astype(np.float64), lte[interval - 1]])

    def _require_fitted(self) -> HistGradientBoostingClassifier:
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

    def fit(self, features: pa.Table) -> HazardModel:
        """Splits (purge / embargo), fits the GBDT on the train person-period rows and the isotonic layers on the
        calib split. Keeps the split table (`split_features`) and assignment (`split_result`) for scoring."""
        self.warnings = []
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
        est.fit(self._design(x[train][rows], interval), target.astype(np.int64))
        self.estimator = est
        # The binner remembers the OpenMP thread count of the fit; it does not affect results, only the pickle bytes.
        if hasattr(est, "_bin_mapper") and hasattr(est._bin_mapper, "n_threads"):
            est._bin_mapper.n_threads = None
        if not np.any(target == 2):
            self.warnings.append("the train split has no cancel events: cancel hazards are 0")

        self.isotonic = [None] * len(self.grid_ns)
        if sp.recalibration == "isotonic":
            calib = np.flatnonzero(label == CALIB)
            if calib.size == 0:
                self.warnings.append("calib split is empty: recalibration skipped")
            else:
                self._fit_isotonic(x[calib], e[calib], c[calib])
        return self

    def _fit_isotonic(self, x: np.ndarray, e: np.ndarray, c: np.ndarray) -> None:
        hf, hc = self.hazards(x)
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
        arr = self.predict_arrays(features)
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

    def _state(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "spec": spec_echo(self.spec.model_dump(mode="json")),
            "feature_names": list(self.feature_names),
            "grid_ns": [int(t) for t in self.grid_ns],
            "estimator": self._require_fitted(),
            "isotonic": list(self.isotonic),
            "n_rows_train": int(self.n_rows_train),
        }

    def save(self, out_dir: str) -> None:
        """Writes model.pkl and records its sha256 in the model card (`model.json`, written or updated)."""
        from queuetruth.model.card import write_card

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        pkl = out / PICKLE_NAME
        with open(pkl, "wb") as f:
            pickle.dump(self._state(), f, protocol=PICKLE_PROTOCOL)
        write_card(self, out)

    @classmethod
    def load(cls, path: str, *, sha256: str | None = None, trusted: bool = False) -> HazardModel:
        """Loads a saved model. `path` is the output directory, its `model.json` or its `model.pkl`.

        Unpickling runs code: unless `trusted=True` is passed explicitly, `sha256` must be the trusted digest of
        `model.pkl` recorded when it was written (`pickle_sha256(card)` of `fit_hazard`'s card), else `QT-CONFIG-001`;
        a pickle whose bytes differ from it, or from the `model.json` beside it (integrity only, not authenticity),
        is refused with `QT-INPUT-002`. The pickle is read once; the bytes hashed are the bytes unpickled."""
        from queuetruth.schemas.models import HazardModelCard

        if sha256 is None and not trusted:
            raise config_error(
                f"HazardModel.load: refusing to unpickle {PICKLE_NAME} without a trusted sha256: pass "
                f"sha256=<digest recorded by fit_hazard> (or trusted=True for a directory only you can write); the "
                f"{CARD_NAME} beside the pickle is not an authenticity check"
            )
        if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise config_error(f"HazardModel.load: sha256 must be 64 hex characters, got {sha256!r}")
        p = Path(path)
        out = p if p.is_dir() else p.parent
        card_path, pkl = out / CARD_NAME, out / PICKLE_NAME
        if not card_path.exists() or not pkl.exists():
            raise QtError("QT-INPUT-001", f"HazardModel.load: {CARD_NAME} and {PICKLE_NAME} are required in {out}")
        try:
            card = HazardModelCard.model_validate(json.loads(card_path.read_text(encoding="utf-8")))
        except ValueError as err:
            raise input_error(f"HazardModel.load: invalid {CARD_NAME}: {err}") from None
        want = pickle_sha256(card)
        if want is None:
            raise input_error(f"HazardModel.load: {CARD_NAME} lists no {PICKLE_NAME}")
        try:
            blob = pkl.read_bytes()  # read once: these bytes are hashed and unpickled (no reopen between the two)
        except OSError as err:
            raise QtError("QT-INPUT-001", f"HazardModel.load: cannot read {pkl}: {err}") from None
        got = hashlib.sha256(blob).hexdigest()
        if sha256 is not None and got != sha256.lower():
            raise input_error(
                f"HazardModel.load: sha256 of {PICKLE_NAME} is {got}, the trusted digest is {sha256.lower()}; refusing"
            )
        if got != want:
            raise input_error(f"HazardModel.load: sha256 of {PICKLE_NAME} is {got}, card records {want}; refusing")
        state = pickle.loads(blob)  # authenticated above (trusted digest or explicit trusted=True)
        if not isinstance(state, dict) or state.get("format_version") != FORMAT_VERSION:
            raise input_error(f"HazardModel.load: unsupported {PICKLE_NAME} format")
        spec = HazardFitSpec.model_validate(state["spec"])
        model = cls(spec)
        model.feature_names = list(state["feature_names"])
        model.grid_ns = check_grid(state["grid_ns"])
        model.estimator = state["estimator"]
        model.isotonic = list(state["isotonic"])
        model.n_rows_train = int(state["n_rows_train"])
        model.card = card
        return model


def pickle_sha256(card: Any) -> str | None:
    """The sha256 of `model.pkl` recorded in a `qt.hazard_model_card` (model or dict; None when not listed): the
    trusted digest to pass to `HazardModel.load` when the card comes from `fit_hazard` itself."""
    files = card.get("files") if isinstance(card, dict) else getattr(card, "files", None)
    for f in files or []:
        name = f.get("name") if isinstance(f, dict) else getattr(f, "name", None)
        if name == PICKLE_NAME:
            sha = f.get("sha256") if isinstance(f, dict) else getattr(f, "sha256", None)
            return None if sha is None else str(sha)
    return None


__all__ = ["CARD_NAME", "FORMAT_VERSION", "PICKLE_NAME", "PREDICTIONS_NAME", "HazardModel", "pickle_sha256"]
