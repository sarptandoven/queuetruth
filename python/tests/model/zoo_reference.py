"""The v0.1 (dd6f8c7) per-row Python loop of `zoo_comparison`, kept verbatim as the reference that the vectorised
`queuetruth.model.card.zoo_comparison` must reproduce exactly (`test_model_perf_equivalence.py`)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.model._common import CANCEL, int64_column, require_columns
from queuetruth.model.card import _num


def _reference_truth_rows(
    zoo: pa.Table, planned_life: pa.Table | None
) -> tuple[list[tuple[str, str, int, float, bool | None, bool]], str]:
    """Per zoo prediction row: (model_id, shadow_id, horizon, p_fill, never-cancel truth or None, comparable) and the
    eligibility rule used. Comparable = the shadow is never-cancel and its planned (administrative) life reaches
    `ts_live + h` (audit ruling A2: outcome-independent). `planned_life` is required (`QT-INPUT-001`; E38 OC-7:
    never a silent fallback to the `censored` flag); a shadow without a row is not comparable."""
    if planned_life is None:
        raise QtError(
            "QT-INPUT-001",
            "zoo_comparison: the planned-life table (qt.zoo_shadows) of the zoo predictions is required; the "
            "never-cancel population is never inferred from the censored flag (E38 OC-7)",
            "set HazardFitSpec.zoo_shadows or keep zoo_shadows.parquet next to zoo_predictions.parquet",
        )
    need = ("model_id", "shadow_id", "horizon_ns", "p_fill", "truth_filled", "ts_live")
    require_columns(zoo, need, "zoo_predictions")
    mid = zoo.column("model_id").combine_chunks().cast(pa.utf8()).to_pylist()
    sid = zoo.column("shadow_id").combine_chunks().cast(pa.utf8()).to_pylist()
    hz = int64_column(zoo, "horizon_ns")
    pf = zoo.column("p_fill").combine_chunks().cast(pa.float64()).to_numpy(zero_copy_only=False)
    truth = zoo.column("truth_filled").combine_chunks().to_pylist()
    live = int64_column(zoo, "ts_live")
    require_columns(planned_life, ("shadow_id", "ts_admin_end", "never_cancel"), "zoo_shadows")
    ps = planned_life.column("shadow_id").combine_chunks().cast(pa.utf8()).to_pylist()
    adm = planned_life.column("ts_admin_end").combine_chunks()
    adm_l = (adm.cast(pa.int64()) if pa.types.is_timestamp(adm.type) else adm).to_pylist()
    nc = planned_life.column("never_cancel").combine_chunks().to_pylist()
    life: dict[str, tuple[bool, int | None]] = {}
    for a, b, n in zip(ps, adm_l, nc, strict=True):
        life.setdefault(a, (bool(n), b))
    rule = "planned life of qt.zoo_shadows (never-cancel shadows whose administrative end is >= ts_live + h)"
    out = []
    for m, s, h, p, y, t0 in zip(mid, sid, hz, pf, truth, live, strict=True):
        nc_adm = life.get(s)
        ok = nc_adm is not None and nc_adm[0] and (nc_adm[1] is None or int(nc_adm[1]) >= int(t0) + int(h))
        out.append((m, s, int(h), float(p), None if y is None else bool(y), ok and y is not None))
    return out, rule


def reference_zoo_comparison(
    model: Any,
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
    counts."""
    if not test:
        return {}
    note = warnings if warnings is not None else []
    from queuetruth.eval.zoo import without_aborted

    rows, rule = _reference_truth_rows(without_aborted(zoo), planned_life)
    t_tbl: pa.Table = test["table"]
    lm = int64_column(t_tbl, "landmark_ns")
    pop = t_tbl.column("population").combine_chunks().cast(pa.utf8()).to_pylist()
    refs = t_tbl.column("order_ref").combine_chunks().cast(pa.utf8()).to_pylist()
    row_of = {r: i for i, (r, l0, pp) in enumerate(zip(refs, lm, pop, strict=True)) if l0 == 0 and pp == "shadows"}
    grid = [int(t) for t in model.grid_ns]
    on_grid = set(grid)
    preds: dict[str, dict[int, dict[int, float]]] = {}
    truth: dict[tuple[int, int], bool] = {}
    for mid, sid, h, p, y, ok in rows:
        i = row_of.get(sid)
        if i is None or h not in on_grid or not ok or not np.isfinite(p) or y is None:
            continue
        truth.setdefault((i, h), y)
        preds.setdefault(mid, {}).setdefault(h, {})[i] = p
    e, c, cif = test["e"], test["c"], test["cif_fill"]
    out: dict[str, Any] = {}
    common: dict[int, set[int]] = {}
    for mid in sorted(preds):
        hm: dict[str, float | None] = {}
        for tau in grid:
            rows_p = preds[mid].get(tau)
            if not rows_p:
                continue
            idx = sorted(rows_p)
            p = np.array([rows_p[i] for i in idx])
            y = np.array([truth[(i, tau)] for i in idx], dtype=np.float64)
            hm[str(tau)] = _num(np.mean((p - y) ** 2))
            common[tau] = set(rows_p) if tau not in common else (common[tau] & set(rows_p))
        out[mid] = {"brier": hm}
    if not out:
        if rows:
            note.append(
                "zoo_comparison: no test shadow is comparable (never-cancel, planned life covering the horizon, exact "
                f"truth, horizon on the grid); label rule: {rule}"
            )
        return out
    own: dict[str, float | None] = {}
    sizes, cancelled = [], []
    for tau, rows_c in sorted(common.items()):
        if not rows_c:
            continue
        j = grid.index(tau)
        idx = np.array(sorted(rows_c), dtype=np.int64)
        y = np.array([truth[(int(i), tau)] for i in idx], dtype=np.float64)
        own[str(tau)] = _num(np.mean((cif[idx, j] - y) ** 2))
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
