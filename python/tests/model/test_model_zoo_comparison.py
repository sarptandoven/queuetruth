"""`zoo_comparison` estimand (review F6, E45): zoo models are scored only on never-cancel shadows whose
planned life covers the horizon, against the zoo's exact never-cancel truth, so the hazard labels' policy cancel does
not compete. Reproducer `exp_zoo_comparison_estimand.py`: scored against the hazard labels (policy cancel
competing), a perfectly calibrated zoo model had IPCW Brier 0.250 and lost to a constant no-skill predictor (0.203).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest

from queuetruth.errors import QtError
from queuetruth.model._common import CANCEL, CENSOR, FILL
from queuetruth.model.card import zoo_comparison
from queuetruth.model.scoring import brier_terms, censoring_km
from queuetruth.schemas.arrow import SCHEMAS

NS = 1_000_000_000
TAU = 10 * NS
LIVE0 = 20_460 * 86_400 * NS + 36_000 * NS


def _world(n: int, seed: int = 7) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    t_fill = np.where(rng.random(n) < 0.7, rng.exponential(8e9, n), np.inf)  # never-cancel exact first fill
    t_cancel = rng.exponential(6e9, n)  # policy cancel of the hazard labels (features)
    cap = 60 * NS
    e = np.minimum(np.minimum(t_fill, t_cancel), cap).astype(np.int64)
    c = np.where(t_fill <= np.minimum(t_cancel, cap), FILL, np.where(t_cancel <= cap, CANCEL, CENSOR)).astype(np.int8)
    return {"t_fill": t_fill, "e": e, "c": c}


def _zoo(sids: list[str], models: dict[str, np.ndarray], truth: np.ndarray, censored: np.ndarray) -> pa.Table:
    rows: dict[str, list] = {k: [] for k in SCHEMAS["qt.zoo_predictions"].names}
    for m, p in models.items():
        for i, s in enumerate(sids):
            rows["model_id"].append(m)
            rows["shadow_id"].append(s)
            rows["qty"].append(1)
            rows["horizon_ns"].append(TAU)
            rows["p_fill"].append(float(p[i]))
            rows["e_fill_qty"].append(float(p[i]))
            for k in ("model_ts_first_fill", "truth_ts_first_fill", "queue_ahead_at_live", "level_qty_at_live"):
                rows[k].append(None)
            rows["truth_filled"].append(bool(truth[i]))
            rows["truth_filled_qty"].append(int(truth[i]))
            rows["censored"].append(bool(censored[i]))
            rows["visible_level_qty_at_live"].append(0)
            rows["out_of_view_at_live"].append(False)
            rows["instrument_id"].append(1)
            rows["session_date"].append(dt.date(2026, 1, 7))
            rows["ts_live"].append(LIVE0 + i)
            rows["trade_vol_60s"].append(0)
            if "liquidity" in rows:
                rows["liquidity"].append(None)
    schema = SCHEMAS["qt.zoo_predictions"]
    return pa.Table.from_arrays([pa.array(rows[f.name]).cast(f.type) for f in schema], schema=schema)


def _shadows(sids: list[str], never: np.ndarray, admin_rel: np.ndarray) -> pa.Table:
    n = len(sids)
    ts = pa.timestamp("ns", tz="UTC")
    adm = [None if not np.isfinite(a) else int(LIVE0 + i + a) for i, a in enumerate(admin_rel)]
    planned = [None if v else LIVE0 + i + NS for i, v in enumerate(never)]
    return pa.table(
        {
            "shadow_id": pa.array(sids, pa.utf8()),
            "ts_cancel_planned": pa.array(planned, pa.int64()).cast(ts),
            "ts_admin_end": pa.array(adm, pa.int64()).cast(ts),
            "instrument_id": pa.array([1] * n, pa.uint32()),
            "session_date": pa.array([dt.date(2026, 1, 7)] * n, pa.date32()),
            "ts_live": pa.array([LIVE0 + i for i in range(n)], pa.int64()).cast(ts),
            "session_close": pa.array([LIVE0 + 10**13] * n, pa.int64()).cast(ts),
            "exact_end_state": pa.array([None] * n, pa.utf8()),
            "never_cancel": pa.array([bool(v) for v in never], pa.bool_()),
        }
    )


def _test_arrays(sids: list[str], w: dict[str, np.ndarray], cif: np.ndarray, n_grid: int = 1) -> dict:
    n = len(sids)
    table = pa.table(
        {
            "order_ref": pa.array(sids, pa.utf8()),
            "landmark_ns": pa.array([0] * n, pa.int64()),
            "population": pa.array(["shadows"] * n).dictionary_encode(),
        }
    )
    cifs = np.tile(cif[:, None], (1, n_grid))
    return {"table": table, "e": w["e"], "c": w["c"], "cif_fill": cifs, "g": censoring_km(w["e"], w["c"])}


def test_f6_perfect_zoo_model_beats_no_skill() -> None:
    n = 20_000
    w = _world(n)
    sids = [f"s{i}" for i in range(n)]
    p_true = 0.7 * (1.0 - np.exp(-TAU / 8e9))  # never-cancel P(fill by 10 s)
    y_hazard = ((w["c"] == FILL) & (w["e"] <= TAU)).astype(float)
    # the old comparison (hazard labels, IPCW) ranked the perfect zoo model below a constant no-skill predictor
    g = censoring_km(w["e"], w["c"])
    old_perfect = np.mean(brier_terms(np.full(n, p_true), w["e"], w["c"], TAU, g))
    old_noskill = np.mean(brier_terms(np.full(n, y_hazard.mean()), w["e"], w["c"], TAU, g))
    assert old_perfect > old_noskill
    truth = w["t_fill"] <= TAU
    models = {"perfect": np.full(n, p_true), "no_skill": np.full(n, y_hazard.mean())}
    zoo = _zoo(sids, models, truth, np.zeros(n, dtype=bool))
    life = _shadows(sids, np.ones(n, dtype=bool), np.full(n, np.inf))
    model = SimpleNamespace(grid_ns=np.array([NS, TAU]))
    notes: list[str] = []
    hazard_cif = np.full(n, y_hazard.mean())  # fill before the policy cancel
    arrays = _test_arrays(sids, w, hazard_cif, n_grid=2)
    out = zoo_comparison(model, "hazard", zoo, arrays, planned_life=life, warnings=notes)
    assert set(out) == {"perfect", "no_skill", "hazard"}
    b = {m: out[m]["brier"][str(TAU)] for m in out}
    assert b["perfect"] < b["no_skill"]
    assert b["perfect"] == pytest.approx(p_true * (1 - p_true), abs=0.01)
    assert str(NS) not in out["perfect"]["brier"]  # the zoo did not predict 1 s
    assert any("never-cancel truth" in x and f"{TAU}={n}" in x for x in notes)
    assert any("policy-cancelled" in x and f"{TAU}=" in x for x in notes)


def test_f6_only_never_cancel_shadows_whose_planned_life_covers_h() -> None:
    n = 400
    w = _world(n, seed=3)
    sids = [f"s{i}" for i in range(n)]
    never = np.arange(n) % 4 != 0  # every 4th shadow carries a planned cancel
    admin = np.where(np.arange(n) % 5 == 0, 5.0 * NS, np.inf)  # every 5th: data ends 5 s after entry
    truth = w["t_fill"] <= TAU
    p = np.where(truth, 1.0, 0.0)
    p[~never] = 1.0 - p[~never]  # wrong on the shadows that must be excluded
    p[admin < TAU] = 1.0 - p[admin < TAU]
    zoo = _zoo(sids, {"oracle": p}, truth, admin < TAU)
    notes: list[str] = []
    out = zoo_comparison(
        SimpleNamespace(grid_ns=np.array([TAU])),
        "hazard",
        zoo,
        _test_arrays(sids, w, np.full(n, 0.3)),
        planned_life=_shadows(sids, never, admin),
        warnings=notes,
    )
    assert out["oracle"]["brier"][str(TAU)] == 0.0
    expected = int(np.sum(never & ~(admin < TAU)))
    assert any(f"{TAU}={expected}" in x and "planned life of qt.zoo_shadows" in x for x in notes)
    # without zoo_shadows: an error, never the censored flag (open conflict 2, E38 OC-7)
    with pytest.raises(QtError) as ei:
        zoo_comparison(
            SimpleNamespace(grid_ns=np.array([TAU])), "hazard", zoo, _test_arrays(sids, w, np.full(n, 0.3)), warnings=[]
        )
    assert ei.value.code == "QT-INPUT-001"
