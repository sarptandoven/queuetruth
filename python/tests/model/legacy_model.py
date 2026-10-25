"""v0.1 (dd6f8c7) behaviour of the hazard model kept as test references: the `model.pkl` writer (the pickle of a plain
dict with the scikit-learn estimator and isotonic layers, protocol 5, listed in `model.json`) and the hazard
prediction through scikit-learn's own `predict_proba` in chunks of 20,000 rows."""

from __future__ import annotations

import hashlib
import json
import pickle
from pathlib import Path

import numpy as np

from queuetruth.model.card import _card_json, assemble_card
from queuetruth.model.hazard import CARD_NAME, MODEL_NAME, PICKLE_NAME, HazardModel
from queuetruth.schemas.models import HazardModelCard
from queuetruth.schemas.runid import spec_echo

V01_CHUNK_ROWS = 20_000


def v01_state(model: HazardModel) -> dict:
    """The dict v0.1 pickled (`HazardModel._state`)."""
    assert model.sklearn_estimator is not None
    return {
        "format_version": 1,
        "spec": spec_echo(model.spec.model_dump(mode="json")),
        "feature_names": list(model.feature_names),
        "grid_ns": [int(t) for t in model.grid_ns],
        "estimator": model.sklearn_estimator,
        "isotonic": list(model.isotonic),
        "n_rows_train": int(model.n_rows_train),
    }


def write_v01_dir(model: HazardModel, out: Path, blob: bytes | None = None) -> str:
    """Writes a v0.1 model directory: `model.pkl` (or the given bytes) and a card listing it (keeping the other
    files of `model.card`, without `model.npz`); returns the pickle's sha256."""
    out.mkdir(parents=True, exist_ok=True)
    data = pickle.dumps(v01_state(model), protocol=5) if blob is None else blob
    (out / PICKLE_NAME).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    entry = {"name": PICKLE_NAME, "rows": None, "bytes": len(data), "sha256": sha}
    if model.card is not None:
        doc = model.card.model_dump(mode="json")
        doc["files"] = [f for f in doc["files"] if f["name"] not in (MODEL_NAME, PICKLE_NAME)] + [entry]
        card = HazardModelCard.model_validate(doc)
    else:
        card = assemble_card(model, model_id=f"hazard_gbdt:{sha[:16]}", scores=None, files=[entry], warnings=[])
    (out / CARD_NAME).write_text(_card_json(card), encoding="utf-8")
    (out / MODEL_NAME).unlink(missing_ok=True)
    return sha


def v01_hazards(model: HazardModel, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """v0.1 `HazardModel.hazards`: scikit-learn `predict_proba` per chunk of 20,000 rows."""
    est = model.sklearn_estimator
    assert est is not None
    n, j_count = x.shape[0], len(model.grid_ns)
    hf, hc = np.zeros((n, j_count)), np.zeros((n, j_count))
    classes = [int(k) for k in est.classes_]
    for lo in range(0, n, V01_CHUNK_ROWS):
        hi = min(n, lo + V01_CHUNK_ROWS)
        m = hi - lo
        rows = np.repeat(np.arange(lo, hi), j_count)
        interval = np.tile(np.arange(1, j_count + 1), m)
        proba = est.predict_proba(model._design(x[rows], interval)).reshape(m, j_count, len(classes))
        if 1 in classes:
            hf[lo:hi] = proba[:, :, classes.index(1)]
        if 2 in classes:
            hc[lo:hi] = proba[:, :, classes.index(2)]
    return hf, hc


def card_doc(path: Path) -> dict:
    """`model.json` of a directory as a dict."""
    return json.loads((path / CARD_NAME).read_text(encoding="utf-8"))
