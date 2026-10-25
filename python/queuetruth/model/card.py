"""`fit_hazard`: fit, predict every split, score the test split and assemble `qt.hazard_model_card` (SPEC 5.5.7-8).

Test-split scores: `rcll` (raw hazards), `ipcw_logloss`, IPCW `brier`, `bss_vs_climatology` (climatology =
`queuetruth.model.climatology` fitted on train; CI = percentile bootstrap over `queuetruth.stats.block_labels` blocks
of test orders (session date with >= 5 test dates, else 30-minute block of the order start), `bootstrap_b` resamples
drawn with `queuetruth.stats.block_counts` from `default_rng(bootstrap_seed(seed, "bss_vs_climatology:<tau>"))`,
the E5 / SPEC 5.7.5 draw procedure), `ibs`; calibration slope,
calibration-in-the-large intercept (slope fixed at 1), Spiegelhalter z and Hosmer-Lemeshow `hl_p` (K df) /
`hl_p_et` (8 df) weighted by the IPCW weights at `tau` (`queuetruth.model.scoring.ipcw_calibration`; rows censored
before `tau` weigh 0; review F2); `c_index_uno` at the grid point nearest 10 s and `auc` per horizon (improper,
discrimination only). `zoo_comparison` (population shadows, landmark 0, test split): Brier of every zoo model and of
this model against the zoo's exact never-cancel truth on never-cancel shadows whose planned life covers the horizon
(`zoo_comparison` docstring; review F6); the planned life is read from `HazardFitSpec.zoo_shadows` (schema 1.1), else
from `zoo_shadows.parquet` next to the zoo predictions, and is required (`QT-INPUT-001` without it: E38 OC-7); the
card's `warnings` state the rule, the row counts and policy-cancelled rows.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.model._common import (
    CANCEL,
    FILL,
    config_error,
    input_error,
    int64_column,
    outcome_arrays,
    require_columns,
)
from queuetruth.model.climatology import Climatology
from queuetruth.model.hazard import CARD_NAME, MODEL_NAME, PICKLE_NAME, PREDICTIONS_NAME, HazardModel
from queuetruth.model.scoring import (
    auc_at,
    brier_terms,
    c_index_uno,
    censoring_km,
    ibs,
    ipcw_calibration,
    ipcw_logloss,
    ipcw_weights,
    rcll,
)
from queuetruth.model.split import TEST, TRAIN
from queuetruth.schemas.arrow import ENGINE_VERSION, SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.schemas.models import HazardModelCard
from queuetruth.schemas.runid import run_id, spec_echo
from queuetruth.schemas.specs import HazardFitSpec, PathRef

C_INDEX_TAU_NS = 10_000_000_000
MODEL_FAMILY = "hazard_gbdt"


def _num(x: Any) -> float | None:
    """Finite float or None (JSON has no NaN)."""
    if x is None:
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _path_of(ref: Any, field: str) -> str:
    if isinstance(ref, PathRef):
        return ref.path
    raise config_error(f"{field}: only local path references are resolved here (got {type(ref).__name__})", field=field)


def read_qt_table(path: str, expect: str) -> pa.Table:
    """Reads a Parquet table and checks its `qt.schema` metadata equals `expect`."""
    p = Path(path)
    if not p.exists():
        raise QtError("QT-INPUT-001", f"input file missing: {path}")
    try:
        tbl = pq.read_table(p)
    except (OSError, pa.ArrowException) as err:
        raise input_error(f"{path}: unreadable Parquet ({err})") from None
    got = (tbl.schema.metadata or {}).get(b"qt.schema", b"").decode()
    if got != expect:
        raise input_error(f"{path}: qt.schema is {got!r}, expected {expect!r}")
    return tbl


def _resolve(ref: dict[str, Any]) -> str:
    if set(ref) == {"path"}:
        p = Path(ref["path"])
        if not p.exists():
            raise QtError("QT-INPUT-001", f"input file missing: {p}")
        return sha256_file(p)
    raise config_error("fit_hazard resolves only local path references")


def model_id_for(spec: HazardFitSpec) -> str:
    """`hazard_gbdt[_privileged]:<run_id>`."""
    rid = run_id(spec.model_dump(mode="json"), ENGINE_VERSION, _resolve)
    return f"{MODEL_FAMILY}{'_privileged' if spec.privileged else ''}:{rid}"


def _safe(fn: Callable[[], Any], name: str, tau: int, warnings: list[str]) -> float | None:
    try:
        return _num(fn())
    except (ValueError, ArithmeticError, np.linalg.LinAlgError) as err:
        warnings.append(f"{name} at {tau} ns undefined: {err}")
        return None


def _safe_dict(fn: Callable[[], dict[str, float]], name: str, tau: int, warnings: list[str]) -> dict[str, float]:
    try:
        return fn()
    except (ValueError, ArithmeticError, np.linalg.LinAlgError) as err:
        warnings.append(f"{name} at {tau} ns undefined: {err}")
        return {}


def _bootstrap_blocks(model: HazardModel, test_mask_kept: np.ndarray, t_tbl: pa.Table) -> np.ndarray:
    """Block index per test row: `queuetruth.stats.block_labels` of the row's `session_date` and its order's start
    (session dates with >= 5 test dates, else 30-minute blocks of the order start), as every other bootstrap."""
    from queuetruth.stats import block_labels

    res = model.split_result
    assert res is not None
    t0 = res.order_t0[res.order_index[res.keep]][test_mask_kept]
    labels, _kind = block_labels(int64_column(t_tbl, "session_date"), t0)
    _, inv = np.unique(labels, return_inverse=True)
    return np.asarray(inv).reshape(-1)


def _bss_ci(tm: np.ndarray, tc: np.ndarray, blocks: np.ndarray, b: int, seed: int, tau: int) -> tuple[Any, Any]:
    """95% percentile CI of `bss = 1 - sum BS_model / sum BS_clim` by block bootstrap with the agreed draw procedure
    (E5 / E29): `block_counts` (one `integers(0, n_blocks, (b, n_blocks))` call of
    `default_rng(bootstrap_seed(seed, "bss_vs_climatology:<tau>"))`), replicate = ratio of the block-count-weighted
    sums, `percentile_ci` (linear quantiles, NaN replicates ignored)."""
    from queuetruth.stats import block_counts, bootstrap_seed, percentile_ci

    n_blocks = int(blocks.max(initial=-1)) + 1
    if n_blocks < 2:
        return None, None
    sm = np.bincount(blocks, weights=tm, minlength=n_blocks)
    sc = np.bincount(blocks, weights=tc, minlength=n_blocks)
    counts = block_counts(n_blocks, b, bootstrap_seed(seed, f"bss_vs_climatology:{tau}")).astype(np.float64)
    bm, bc = counts @ sm, counts @ sc
    reps = np.where(bc > 0, 1.0 - bm / np.where(bc > 0, bc, 1.0), np.nan)
    lo, hi = percentile_ci(reps, 0.05)
    return _num(lo), _num(hi)


def empty_scores(grid_ns: np.ndarray) -> dict[str, Any]:
    """`scores_test` with every value null (no test split scored)."""
    h = {str(int(t)): None for t in grid_ns}
    return {
        "rcll": None,
        "ipcw_logloss": dict(h),
        "brier": dict(h),
        "bss_vs_climatology": {k: {"value": None, "ci_lo": None, "ci_hi": None} for k in h},
        "ibs": None,
        "calib_slope": dict(h),
        "calib_intercept": dict(h),
        "spiegelhalter_z": dict(h),
        "hl_p": dict(h),
        "c_index_uno": None,
        "auc": dict(h),
        "hl_p_et": dict(h),
    }


def score_test(model: HazardModel, warnings: list[str]) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Scores of the test split (module docstring) and the test arrays used (for `zoo_comparison`)."""
    tbl, res = model.split_features, model.split_result
    assert tbl is not None and res is not None
    grid = model.grid_ns
    label = res.label[res.keep]
    test = label == TEST
    if not test.any():
        warnings.append("test split is empty: scores are null")
        return empty_scores(grid), {}
    t_tbl = tbl.filter(pa.array(test))
    e, c = outcome_arrays(t_tbl)
    arr = model.split_arrays(test)  # the hazards cached by fit (equal to predict_arrays(t_tbl))
    g = censoring_km(e, c)
    train = label == TRAIN
    tr_tbl = tbl.filter(pa.array(train))
    e_tr, c_tr = outcome_arrays(tr_tbl)
    clim = Climatology.fit(tr_tbl, e_tr, c_tr, res.order_index[res.keep][train], grid).predict(t_tbl)
    blocks = _bootstrap_blocks(model, test, t_tbl)
    sp = model.spec

    out = empty_scores(grid)
    out["rcll"] = _num(rcll(arr["h_fill"], arr["h_cancel"], e, c, list(grid)))
    briers: list[float] = []
    for j, tau in enumerate(grid):
        tau = int(tau)
        k = str(tau)
        ff, fc = arr["cif_fill"][:, j], arr["cif_cancel"][:, j]
        out["ipcw_logloss"][k] = _num(ipcw_logloss(ff, fc, e, c, tau, censoring=g))
        tm = brier_terms(ff, e, c, tau, g)
        tc = brier_terms(clim[:, j], e, c, tau, g)
        bs = float(np.mean(tm))
        briers.append(bs)
        out["brier"][k] = _num(bs)
        bss = 1.0 - float(np.sum(tm)) / float(np.sum(tc)) if np.sum(tc) > 0 else None
        lo, hi = _bss_ci(tm, tc, blocks, sp.bootstrap_b, sp.seed, tau) if bss is not None else (None, None)
        out["bss_vs_climatology"][k] = {"value": _num(bss), "ci_lo": lo, "ci_hi": hi}
        w = ipcw_weights(e, c, tau, g)
        y = ((c == FILL) & (e <= tau)).astype(np.float64)
        known = w > 0
        if known.sum() >= 2 and 0 < y[known].sum() < known.sum():
            cal = _safe_dict(lambda ff=ff, y=y, w=w: ipcw_calibration(ff, y, w), "calibration", tau, warnings)
            out["calib_slope"][k] = _num(cal.get("calib_slope"))
            out["calib_intercept"][k] = _num(cal.get("calib_intercept"))
            out["spiegelhalter_z"][k] = _num(cal.get("spiegelhalter_z"))
            out["hl_p"][k] = _num(cal.get("hl_p"))
            out["hl_p_et"][k] = _num(cal.get("hl_p_et"))
        out["auc"][k] = _num(auc_at(e, c, ff, tau, censoring=g))
    out["ibs"] = _num(ibs(briers, list(grid)))
    jc = int(np.argmin(np.abs(grid - C_INDEX_TAU_NS)))
    out["c_index_uno"] = _num(c_index_uno(arr["cif_fill"][:, jc], e, c, int(grid[jc]), censoring=g, seed=sp.seed))
    if int(grid[jc]) != C_INDEX_TAU_NS:
        warnings.append(f"c_index_uno computed at {int(grid[jc])} ns (10 s is not on the grid)")
    return out, {"table": t_tbl, "e": e, "c": c, "cif_fill": arr["cif_fill"], "g": g}  # type: ignore[dict-item]


ZOO_SHADOWS_NAME = "zoo_shadows.parquet"


def _zoo_truth_rows(zoo: pa.Table, planned_life: pa.Table | None) -> tuple[dict[str, Any], str]:
    """Columns per zoo prediction row: `model_id` (Arrow utf8), `shadow_id` (Arrow utf8), `horizon_ns`, `p_fill`,
    never-cancel truth `truth` with its validity `has_truth`, and `comparable`; plus the eligibility rule used.
    Comparable = the row has an exact truth, the shadow is never-cancel and its planned (administrative) life reaches
    `ts_live + h` (audit ruling A2: outcome-independent). `planned_life` is required (`QT-INPUT-001`; E38 OC-7: never
    a silent fallback to the `censored` flag); a shadow without a row is not comparable, and of repeated planned-life
    rows of one shadow the first counts. Vectorised: the planned life is looked up once per distinct shadow id."""
    if planned_life is None:
        raise QtError(
            "QT-INPUT-001",
            "zoo_comparison: the planned-life table (qt.zoo_shadows) of the zoo predictions is required; the "
            "never-cancel population is never inferred from the censored flag (E38 OC-7)",
            "set HazardFitSpec.zoo_shadows or keep zoo_shadows.parquet next to zoo_predictions.parquet",
        )
    need = ("model_id", "shadow_id", "horizon_ns", "p_fill", "truth_filled", "ts_live")
    require_columns(zoo, need, "zoo_predictions")
    sid = zoo.column("shadow_id").combine_chunks().cast(pa.utf8())
    hz = int64_column(zoo, "horizon_ns")
    pf = zoo.column("p_fill").combine_chunks().cast(pa.float64()).to_numpy(zero_copy_only=False)
    truth_col = zoo.column("truth_filled").combine_chunks().cast(pa.bool_())
    has_truth = np.asarray(truth_col.is_valid().to_numpy(zero_copy_only=False), dtype=bool)
    truth = np.asarray(truth_col.fill_null(False).to_numpy(zero_copy_only=False), dtype=bool)
    live = int64_column(zoo, "ts_live")
    require_columns(planned_life, ("shadow_id", "ts_admin_end", "never_cancel"), "zoo_shadows")
    ps = planned_life.column("shadow_id").combine_chunks().cast(pa.utf8()).to_pylist()
    adm = planned_life.column("ts_admin_end").combine_chunks()
    adm_l = (adm.cast(pa.int64()) if pa.types.is_timestamp(adm.type) else adm).to_pylist()
    nc = planned_life.column("never_cancel").combine_chunks().to_pylist()
    life: dict[str | None, tuple[bool, int | None]] = {}
    for a, b, n in zip(ps, adm_l, nc, strict=True):
        life.setdefault(a, (bool(n), b))
    codes, uniq = _encode(sid)
    u_never = np.zeros(len(uniq), dtype=bool)  # known, never-cancel shadow
    u_open = np.zeros(len(uniq), dtype=bool)  # no administrative end
    u_end = [0] * len(uniq)
    for k, s in enumerate(uniq):
        got = life.get(s)
        if got is not None and got[0]:
            u_never[k] = True
            u_open[k] = got[1] is None
            u_end[k] = 0 if got[1] is None else int(got[1])
    end = _int_or_object(u_end)[codes]
    reach = _ge_sum(end, live, hz)
    comparable = u_never[codes] & (u_open[codes] | reach) & has_truth
    rule = "planned life of qt.zoo_shadows (never-cancel shadows whose administrative end is >= ts_live + h)"
    cols = {
        "model_id": zoo.column("model_id").combine_chunks().cast(pa.utf8()),
        "shadow_id": sid,
        "horizon_ns": hz,
        "p_fill": pf,
        "truth": truth,
        "has_truth": has_truth,
        "comparable": comparable,
    }
    return cols, rule


def _encode(arr: pa.Array) -> tuple[np.ndarray, list[Any]]:
    """`(codes, distinct values)` of a utf8 array, nulls included as a value (`None`)."""
    enc = pc.dictionary_encode(arr, null_encoding="encode")
    codes = np.asarray(enc.indices.to_numpy(zero_copy_only=False), dtype=np.int64)
    return codes, enc.dictionary.to_pylist()


_SAFE_INT = 1 << 62


def _int_or_object(values: list[int]) -> np.ndarray:
    """int64 when every value is inside +-2^62 (sums of two cannot overflow), else exact Python integers."""
    if all(-_SAFE_INT < v < _SAFE_INT for v in values):
        return np.asarray(values, dtype=np.int64)
    return np.asarray(values, dtype=object)


def _ge_sum(end: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """`end >= a + b` elementwise, exact (no int64 overflow)."""
    wide = end.dtype == object or bool(np.any((a >= _SAFE_INT) | (a <= -_SAFE_INT)))
    if wide or bool(np.any((b >= _SAFE_INT) | (b <= -_SAFE_INT))):
        return np.array([int(x) >= int(y) + int(z) for x, y, z in zip(end, a, b, strict=True)], dtype=bool)
    return end >= a + b


def zoo_comparison(
    model: HazardModel,
    model_id: str,
    zoo: pa.Table,
    test: dict[str, Any],
    *,
    planned_life: pa.Table | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """`zoo_comparison`: Brier `mean((p - y)^2)` of every zoo model and of this model (`cif_fill`, key = its
    `model_id`) at each grid horizon the zoo predicts, on the same test shadows (landmark 0, population shadows).

    Estimand (review F6, E45): a zoo `p_fill(h)` is P(first fill by h) of a shadow that never
    cancels, so the comparison uses ONLY never-cancel shadows whose planned life covers h (`qt.zoo_shadows`, A2) and
    their exact never-cancel truth `truth_filled` from the zoo predictions as the label. The hazard model's policy
    cancel (`cancel_away_ticks`) is therefore not a competing event of the comparison, and no row is censored
    before h (eligibility is outcome-independent), so the Brier is unweighted. Every zoo model is scored on the
    rows it predicts; this model on the rows that every zoo model predicts at that horizon. The hazard model's own
    labels still treat the policy cancel as competing: where compared shadows were policy-cancelled before h its
    `CIF_fill(h)` estimates "fill before the policy cancel" (at most the never-cancel probability), which a warning
    counts.

    Duplicates: a test shadow's truth at h is that of the first eligible zoo row for it; a model's prediction is its
    last eligible row; a test `order_ref` that repeats maps to its last landmark-0 shadow row. Vectorised over the
    zoo rows (the per-model sums run over the shadows in test-row order, as the v0.1 loop did; tested equal)."""
    if not test:
        return {}
    note = warnings if warnings is not None else []
    from queuetruth.eval.zoo import without_aborted

    zoo = without_aborted(zoo)
    cols, rule = _zoo_truth_rows(zoo, planned_life)
    t_tbl: pa.Table = test["table"]
    lm = int64_column(t_tbl, "landmark_ns")
    pop = t_tbl.column("population").combine_chunks().cast(pa.utf8()).to_pylist()
    refs = t_tbl.column("order_ref").combine_chunks().cast(pa.utf8()).to_pylist()
    row_of = {r: i for i, (r, l0, pp) in enumerate(zip(refs, lm, pop, strict=True)) if l0 == 0 and pp == "shadows"}
    grid_arr = np.asarray([int(t) for t in model.grid_ns], dtype=np.int64)
    grid = [int(t) for t in grid_arr]
    n_grid, n_test = len(grid), t_tbl.num_rows

    s_codes, s_uniq = _encode(cols["shadow_id"])
    row = np.array([row_of.get(s, -1) for s in s_uniq], dtype=np.int64)[s_codes] if s_uniq else s_codes
    h, p = cols["horizon_ns"], cols["p_fill"]
    j = np.searchsorted(grid_arr, h)
    on_grid = (j < n_grid) & (grid_arr[np.minimum(j, n_grid - 1)] == h)
    keep = np.flatnonzero((row >= 0) & on_grid & cols["comparable"] & np.isfinite(p))
    m_codes, m_uniq = _encode(cols["model_id"])
    i_v, j_v, p_v, y_v, m_v = row[keep], j[keep], p[keep], cols["truth"][keep], m_codes[keep]
    # truth per (shadow row, horizon): the first eligible zoo row
    tkey = i_v * n_grid + j_v
    uk, first = np.unique(tkey, return_index=True)
    truth_at = np.full(n_test * n_grid, np.nan)
    truth_at[uk] = y_v[first].astype(np.float64)
    # prediction per (model, horizon, shadow row): the last eligible zoo row; keys sort by model, horizon, row
    pkey = (m_v * n_grid + j_v) * n_test + i_v
    rev_keys, rev_first = np.unique(pkey[::-1], return_index=True)
    p_last = p_v[::-1][rev_first]
    k_model, rest = np.divmod(rev_keys, n_grid * n_test)
    k_j, k_i = np.divmod(rest, n_test)
    present = [int(m) for m in np.unique(k_model)]
    by_name = {m_uniq[m]: m for m in present}

    out: dict[str, Any] = {}
    common: dict[int, np.ndarray] = {}
    n_models_at = np.zeros(n_grid, dtype=np.int64)
    hits = np.zeros((n_grid, n_test), dtype=np.int32)
    for name in sorted(by_name):
        m = by_name[name]
        hm: dict[str, float | None] = {}
        in_m = np.flatnonzero(k_model == m)
        lo_m, hi_m = (int(in_m[0]), int(in_m[-1]) + 1)
        for jj, tau in enumerate(grid):
            lo = lo_m + int(np.searchsorted(k_j[lo_m:hi_m], jj, side="left"))
            hi = lo_m + int(np.searchsorted(k_j[lo_m:hi_m], jj, side="right"))
            if lo == hi:
                continue
            idx = k_i[lo:hi]
            y = truth_at[idx * n_grid + jj]
            hm[str(tau)] = _num(np.mean((p_last[lo:hi] - y) ** 2))
            n_models_at[jj] += 1
            hits[jj, idx] += 1
        out[name] = {"brier": hm}
    for jj, tau in enumerate(grid):
        if n_models_at[jj]:
            common[tau] = np.flatnonzero(hits[jj] == n_models_at[jj])
    if not out:
        if zoo.num_rows:
            note.append(
                "zoo_comparison: no test shadow is comparable (never-cancel, planned life covering the horizon, exact "
                f"truth, horizon on the grid); label rule: {rule}"
            )
        return out
    e, c, cif = test["e"], test["c"], test["cif_fill"]
    own: dict[str, float | None] = {}
    sizes, cancelled = [], []
    for tau, idx in sorted(common.items()):
        if not idx.size:
            continue
        jj = grid.index(tau)
        y = truth_at[idx * n_grid + jj]
        own[str(tau)] = _num(np.mean((cif[idx, jj] - y) ** 2))
        sizes.append(f"{tau}={idx.size}")
        k = int(np.sum((c[idx] == CANCEL) & (e[idx] <= tau)))
        if k:
            cancelled.append(f"{tau}={k}/{idx.size}")
    out[model_id] = {"brier": own}
    note.append(
        "zoo_comparison: Brier against the zoo's exact never-cancel truth (truth_filled) on test shadows, landmark 0; "
        f"comparable shadows per horizon (ns) {', '.join(sizes) or 'none'}; label rule: {rule}; the policy cancel is "
        "not a competing event here (review F6, E45)"
    )
    if cancelled:
        note.append(
            "zoo_comparison: compared shadows policy-cancelled (cancel_away_ticks) before the horizon in this model's "
            f"labels, per horizon (ns) {', '.join(cancelled)}: there the model's CIF_fill estimates a fill before "
            "that cancel and is at most the never-cancel probability the zoo models predict; for a like-for-like "
            "comparison extract the features with cancel_away_ticks null or 0 (never cancel)"
        )
    return out


def assemble_card(
    model: HazardModel,
    *,
    model_id: str,
    scores: dict[str, Any] | None,
    files: list[dict[str, Any]],
    warnings: list[str],
    zoo: dict[str, Any] | None = None,
) -> HazardModelCard:
    """Builds and validates the `qt.hazard_model_card` document."""
    res = model.split_result
    split = (
        res.echo if res is not None else {"scheme": model.spec.split.scheme, "train": None, "calib": None, "test": None}
    )
    n_orders = res.n_orders() if res is not None else {"train": 0, "calib": 0, "test": 0}
    doc = {
        "schema": "qt.hazard_model_card",
        "schema_version": 1,
        "model_id": model_id,
        "format_version": 1,
        "spec": spec_echo(model.spec.model_dump(mode="json")),
        "grid_ns": [int(t) for t in model.grid_ns],
        "causes": ["fill", "cancel"],
        "feature_names": list(model.feature_names),
        "privileged": bool(model.spec.privileged),
        "split": split,
        "n_orders": n_orders,
        "n_rows_train": int(model.n_rows_train),
        "scores_test": scores if scores is not None else empty_scores(model.grid_ns),
        "zoo_comparison": zoo or None,
        "recalibration": model.spec.recalibration,
        "files": files,
        "warnings": list(warnings),
    }
    return HazardModelCard.model_validate(doc)


def _card_json(card: HazardModelCard) -> str:
    return json.dumps(card.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def write_card(model: HazardModel, out: Path) -> HazardModelCard:
    """Writes `model.json` for a model whose `model.npz` exists in `out`: updates the card of `fit_hazard` with the
    model file's entry (replacing any `model.npz` / legacy `model.pkl` entry), or builds one with null scores for a
    model fitted directly."""
    path = out / MODEL_NAME
    entry = {"name": MODEL_NAME, "rows": None, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
    if model.card is not None:
        doc = model.card.model_dump(mode="json")
        doc["files"] = [f for f in doc["files"] if f["name"] not in (MODEL_NAME, PICKLE_NAME)] + [entry]
        card = HazardModelCard.model_validate(doc)
    else:
        card = assemble_card(
            model,
            model_id=f"{MODEL_FAMILY}:{entry['sha256'][:16]}",
            scores=None,
            files=[entry],
            warnings=[*model.warnings, "card written by HazardModel.save without test scores"],
        )
    (out / CARD_NAME).write_text(_card_json(card), encoding="utf-8")
    model.card = card
    return card


def fit_hazard(spec: HazardFitSpec | dict[str, Any]) -> HazardModelCard:
    """`fit-hazard`: reads `qt.features`, fits, writes `predictions.parquet` (every split), `model.npz` and
    `model.json` under `out` (nothing is written when `out` is null) and returns the model card."""
    sp = spec if isinstance(spec, HazardFitSpec) else HazardFitSpec.model_validate(spec)
    features = read_qt_table(_path_of(sp.features, "features"), "qt.features")
    zoo_tbl = zoo_shadows = None
    if sp.zoo_shadows is not None and sp.zoo_predictions is None:
        raise config_error("zoo_shadows: only applies with zoo_predictions", field="zoo_shadows")
    if sp.zoo_predictions is not None:
        zoo_path = _path_of(sp.zoo_predictions, "zoo_predictions")
        zoo_tbl = read_qt_table(zoo_path, "qt.zoo_predictions")
        if sp.zoo_shadows is not None:  # schema 1.1: the planned-life table named in the spec
            zoo_shadows = read_qt_table(_path_of(sp.zoo_shadows, "zoo_shadows"), "qt.zoo_shadows")
        else:  # discovery: the zoo output directory's own table
            sibling = Path(zoo_path).with_name(ZOO_SHADOWS_NAME)
            if not sibling.is_file():
                raise QtError(
                    "QT-INPUT-001",
                    f"{sibling}: missing; the zoo comparison needs the planned-life table of the zoo predictions "
                    "(audit ruling A2, E38 OC-7: never a silent fallback to the censored flag)",
                    "set HazardFitSpec.zoo_shadows to the zoo run's zoo_shadows.parquet",
                )
            zoo_shadows = read_qt_table(str(sibling), "qt.zoo_shadows")
    mid = model_id_for(sp)
    model = HazardModel(sp).fit(features)
    warnings = list(model.warnings)
    scores, test = score_test(model, warnings)
    zoo = None
    if zoo_tbl is not None:
        zoo = zoo_comparison(model, mid, zoo_tbl, test, planned_life=zoo_shadows, warnings=warnings)
    assert model.split_features is not None
    files: list[dict[str, Any]] = []
    if sp.out is not None:
        out = Path(sp.out)
        out.mkdir(parents=True, exist_ok=True)
        preds = model.predict_split()
        files.append(write_table(preds, out / PREDICTIONS_NAME, SCHEMAS["qt.hazard_predictions"]))
    model.card = assemble_card(model, model_id=mid, scores=scores, files=files, warnings=warnings, zoo=zoo)
    if sp.out is not None:
        model.save(sp.out)
    return model.card


__all__ = [
    "assemble_card",
    "empty_scores",
    "fit_hazard",
    "model_id_for",
    "read_qt_table",
    "score_test",
    "write_card",
    "zoo_comparison",
]
