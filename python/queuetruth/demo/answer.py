"""The demo's answer from a zoo run: each model's fill-rate bias by horizon next to the exact fill rate, with a
95% interval from a cluster bootstrap that suits a window of minutes.

Point values are the zoo leaderboard's (`qt.zoo_summary.leaderboard`: model fill rate minus the exact Aalen-Johansen
fill rate on never-cancel shadows, audit ruling A2). The zoo's own interval resamples 30-minute buckets of `ts_live`
(SPEC 5.6); a demo window of a few minutes falls in one or two buckets, so that interval is degenerate (one bucket:
every replicate equals the estimate). The demo therefore recomputes the interval with clusters = (instrument,
minute of `ts_live`), the same ratio-of-sums replicate as the zoo (`sum_k c_k num_k / sum_k c_k den_k` over
`block_counts(n_clusters, b, bootstrap_seed(seed, "demo_cluster"))`), and the percentile interval.

That recomputation is valid only when the label of every row exists at every horizon (no administrative end such as
the end of data or a halt inside a never-cancel shadow's `h_max`) and no never-cancel shadow is cancelled before
`h_max`: then the Aalen-Johansen fill rate is the plain fraction of shadows filled and `mean(p - y)` over the rows is
exactly the leaderboard bias. `cluster_intervals` checks both per model and horizon (|difference| <= 1e-9); a cell
that fails keeps the zoo's value with no interval (`ci_method = "none"`) and `notes["no_interval"]` names the reason
(`truncated_labels`, `non_finite` when a row's probability is NaN, or `mismatch`). Rows of shadows on which a
faithful hftbacktest model aborted (#312) are left out of every model, as in `queuetruth.eval.zoo.common_population`.

Shadows placed in the same minute on the same symbol share the same queue and the same price moves, and adjacent
minutes are not independent either (a shadow lives 5 minutes), so the interval is an approximation that accounts
for the dependence within a cluster only.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth.eval import _cols as C
from queuetruth.stats import block_counts, bootstrap_seed, percentile_ci

NT_DEFAULT = "nt_prob_touch[p=1]"
CLUSTER_NS = 60_000_000_000
BOOTSTRAP_ID = "demo_cluster"
MATCH_TOL = 1e-9
_PRED_COLS = ["model_id", "shadow_id", "horizon_ns", "p_fill", "truth_filled", "instrument_id", "ts_live", "status"]


def short_id(model_id: str) -> str:
    """The model id without the run-wide parameters `mode=faithful` and `exch=partial` (the demo's settings):
    `nt_prob_touch[mode=faithful;p=1]` -> `nt_prob_touch[p=1]`, `front_of_queue[mode=faithful]` -> `front_of_queue`."""
    name, bracket, rest = model_id.partition("[")
    if not bracket:
        return model_id
    params = [p for p in rest.rstrip("]").split(";") if p and p not in ("mode=faithful", "exch=partial")]
    return f"{name}[{';'.join(params)}]" if params else name


def find_model(leaderboard: list[dict[str, Any]], want: str) -> dict[str, Any] | None:
    """The leaderboard entry whose id matches `want` (`nt_prob_touch[p=1]` matches the canonical
    `nt_prob_touch[mode=faithful;p=1]`)."""
    from queuetruth.api import _model_matches

    return next((e for e in leaderboard if _model_matches(want, str(e["model_id"]))), None)


def _finite(x: Any) -> float | None:
    if x is None:
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def cluster_intervals(
    zoo_dir: Path, leaderboard: list[dict[str, Any]], *, h_max_ns: int, seed: int = 0, b: int = 2000
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    """({model_id: {horizon_ns: cell}}, notes) with cell = value, ci_lo, ci_hi, model_fill_rate, exact_fill_rate,
    n, ci_method (module docstring)."""
    preds = pq.read_table(zoo_dir / "zoo_predictions.parquet", columns=_PRED_COLS)
    # common_population (eval.zoo): every row of a shadow that any faithful hbt pair aborted on at #312 is left out
    aborted = pc.unique(preds.filter(pc.is_valid(preds["status"]))["shadow_id"])
    if len(aborted):
        preds = preds.filter(pc.invert(pc.is_in(preds["shadow_id"], value_set=aborted)))
    shadows = pq.read_table(zoo_dir / "zoo_shadows.parquet")
    live_s, _ = C.ints(shadows, "ts_live")
    cancel_s, cancel_v = C.ints(shadows, "ts_cancel_planned")
    admin_s, admin_v = C.ints(shadows, "ts_admin_end")
    nc_s = np.asarray(shadows["never_cancel"].to_numpy(zero_copy_only=False), dtype=bool)
    admin_rel_s = np.where(admin_v, (admin_s - live_s).astype(float), np.inf)
    cancelled_early = int((nc_s & cancel_v & (cancel_s - live_s < h_max_ns)).sum())

    row_s = pc.index_in(preds["shadow_id"], value_set=shadows["shadow_id"])
    known = np.asarray(pc.is_valid(row_s).to_numpy(zero_copy_only=False), dtype=bool)
    row = np.asarray(pc.fill_null(row_s, 0).to_numpy(zero_copy_only=False), dtype=np.int64)
    model = C.strings(preds, "model_id")
    h, _ = C.ints(preds, "horizon_ns")
    p = C.floats(preds, "p_fill")
    y, yv = C.bools(preds, "truth_filled")
    inst, _ = C.ints(preds, "instrument_id")
    live, _ = C.ints(preds, "ts_live")
    never_cancel = known & nc_s[row]
    admin_rel = np.where(known, admin_rel_s[row], -np.inf)
    use = never_cancel & yv & (admin_rel >= h.astype(float))
    truncated = int(np.unique(row[never_cancel & yv & (admin_rel < float(h_max_ns))]).size)

    t0 = int(live[use].min()) if use.any() else 0
    minute = (live - t0) // CLUSTER_NS
    cluster = np.full(live.size, -1, dtype=np.int64)
    n_clusters = 0
    if use.any():
        _, inv = np.unique(np.stack([inst[use], minute[use]], axis=1), axis=0, return_inverse=True)
        cluster[use] = np.asarray(inv).reshape(-1)
        n_clusters = int(cluster[use].max()) + 1
    counts = block_counts(n_clusters, b, bootstrap_seed(seed, BOOTSTRAP_ID)).astype(float)
    notes = {
        "clusters": n_clusters,
        "cluster": "instrument x minute of ts_live",
        "bootstrap_b": b,
        "shadows_with_truncated_labels": truncated,
        "never_cancel_shadows_cancelled_before_h_max": cancelled_early,
        "no_interval": [],
    }
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in leaderboard:
        mid = str(entry["model_id"])
        cells: dict[str, dict[str, Any]] = {}
        for hk, cv in sorted((entry.get("fill_rate_bias") or {}).items(), key=lambda kv: int(kv[0])):
            value = _finite((cv or {}).get("value"))
            cell: dict[str, Any] = {
                "value": value,
                "ci_lo": None,
                "ci_hi": None,
                "model_fill_rate": _finite((entry.get("model_fill_rate") or {}).get(hk)),
                "exact_fill_rate": _finite((entry.get("exact_fill_rate") or {}).get(hk)),
                "n": 0,
                "ci_method": "none",
            }
            sel = use & (model == mid) & (h == int(hk))
            n = int(sel.sum())
            cell["n"] = n
            if value is None or not n or not n_clusters:
                cells[hk] = cell
                continue
            d = p[sel] - y[sel].astype(float)
            direct = float(d.mean())
            if truncated or cancelled_early:
                notes["no_interval"].append(
                    {
                        "model_id": mid,
                        "horizon_ns": int(hk),
                        "reason": "truncated_labels",
                        "zoo": value,
                        "direct": direct,
                    }
                )
            elif not math.isfinite(direct):
                notes["no_interval"].append(
                    {"model_id": mid, "horizon_ns": int(hk), "reason": "non_finite", "zoo": value, "direct": None}
                )
            elif not abs(direct - value) <= MATCH_TOL:
                notes["no_interval"].append(
                    {"model_id": mid, "horizon_ns": int(hk), "reason": "mismatch", "zoo": value, "direct": direct}
                )
            else:
                num = np.bincount(cluster[sel], weights=d, minlength=n_clusters)
                den = np.bincount(cluster[sel], minlength=n_clusters).astype(float)
                rd = counts @ den
                reps = np.where(rd > 0, (counts @ num) / np.where(rd > 0, rd, 1.0), np.nan)
                lo, hi = percentile_ci(reps, 0.05)
                cell.update(ci_lo=_finite(lo), ci_hi=_finite(hi), ci_method="cluster_bootstrap")
            cells[hk] = cell
        out[mid] = cells
    return out, notes


__all__ = ["CLUSTER_NS", "NT_DEFAULT", "cluster_intervals", "find_model", "short_id"]
