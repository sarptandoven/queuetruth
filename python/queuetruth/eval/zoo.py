"""Zoo evaluation against exact truth (SPEC 5.6 with audit rulings A2 and A10).

Strata (`assign_strata`): per (instrument_id, session_date), over the distinct shadows of the run (first row per
`shadow_id`): `queue_decile = clip(searchsorted(q, queue_ahead_at_live, side="right"), 0, 9)` with `q` the
0.1..0.9 `numpy.quantile` (linear) of the non-null `queue_ahead_at_live` (null stays null);
`liquidity_bucket` likewise with the 1/3, 2/3 quantiles of `trade_vol_60s` (0..2).

Truth labels (A2). With `planned_life` (one row per shadow: `shadow_id`, `ts_cancel_planned` = when the shadow's own
planned cancel takes effect, `ts_admin_end` = exogenous end such as session end or end of data; both nullable
timestamps, null = none), the label at horizon `h` exists iff `ts_admin_end` is null or `>= ts_live + h`,
independent of the fill outcome; an own cancel is never censoring. `planned_life` is required and must cover every
predicted shadow (`QT-INPUT-001` / `QT-INPUT-002`; E38 OC-7: never a silent fallback to the `censored` column).
Rows also need an exact arm (`truth_filled` not null). Rows whose 1.1 `status` is `hbt_312_abort` (a faithful hbt
pair aborted at its #312 event; the model stays, the pair stops there) are not predictions and are left out.
Populations: never-cancel shadows (no planned cancel before `ts_live + h_max`) give the unprefixed metrics and the
leaderboard; cancel-bearing shadows (e.g. real-order mirrors) are evaluated separately under metric names prefixed
`cancel_bearing/`, with the own cancel as a competing risk.

Metrics on eligible rows, `y = truth_filled` (first fill by `min(ts_live + h, cancel)`), `p = p_fill`, for every
horizon `<= h_max_ns` and stratum (`all`; `queue_decile` 0..9; `liquidity_bucket` 0..2; `instrument`):
- `brier = mean((p - y)^2)`; `logloss = -mean(y ln pc + (1 - y) ln(1 - pc))`, `pc = clip(p, 1e-6, 1 - 1e-6)`;
  `fill_rate_bias = mean(p) - mean(y)` (eligible rows of the stratum: a per-stratum diagnostic; the leaderboard
  bias is the AJ figure below); `qty_bias = sum(e_fill_qty - truth_filled_qty) / sum(qty)`.
- at `h_max`: model filled `= p >= 0.5`; `b` = truth-only, `c` = model-only; `overfill_bias = (c - b) / N`;
  primary cluster-robust discordance test (A10, `queuetruth.stats.clustered_mcnemar`, clusters = bootstrap blocks):
  `discordance_p_primary`, `discordance_p_obuchowski` (>= 5 clusters), `discordance_p_bootstrap`, `n_clusters`,
  `n_effective`; secondary plain McNemar `mcnemar_b`, `mcnemar_c`, `mcnemar_p_exact` (assumes independent orders).
- stratum `all` only: `fill_rate_bias_aj` per horizon = mean of `p` over the population's eligible rows at `h`
  (label exists: planned life reaches `ts_live + h`, the same rows as `fill_rate_bias`) minus the truth
  Aalen-Johansen `CIF_fill(h)`; `ttf_iad = sum_j |mean_i p(h_j) - CIF_fill(h_j)| (h_j - h_{j-1}) / h_max` over the
  horizons `<= h_max` plus `h_max` itself (`h_0 = 0`), with the same means (null when a horizon has no eligible row,
  in particular when the predictions carry no `h_max` rows, so the terms always span `[0, h_max]`). A row cut by its
  administrative end before `h` is left out of the mean because its forecast is truncated there ("fill by end of
  data") while the AJ CIF estimates the untruncated incidence (review F5): both terms then estimate the same
  P(fill by h) of the population under the AJ's independent-censoring assumption, and a calibrated model scores
  ~0. The truth CIF is Aalen-Johansen over the population's shadows: cause fill at the exact first fill, cause
  cancel at the planned cancel, censored at the administrative end (capped at the largest horizon). Never 1 - KM.
  `fill_rate_bias_aj` carries a 95% CI on the shared block draws (replicate = mean of `p` over the drawn eligible
  rows minus the AJ CIF of the drawn shadows, each shadow weighted by its block's multiplicity).
- Leaderboard (`qt.zoo_summary.leaderboard`): `fill_rate_bias[h]` is the never-cancel `fill_rate_bias_aj` at `h`
  with its CI (audit ruling A2 and SPEC 5.6: fill-rate truth is the AJ CIF), so the headline and the report's "bias
  vs AJ CIF" agree; the eligible-row `fill_rate_bias` rows of `qt.zoo_errors` stay as per-stratum diagnostics.
- `ttf_log_ratio_median` (h_max) = median over jointly filled eligible rows (truth filled, model first fill within
  `h_max`; particle families `databento_mc` excluded) of `ln(max(model_ttf, 1us) / max(truth_ttf, 1us))`;
  `qtrack_mae` (with `trajectory`) = mean over (model, shadow) pairs of the time-weighted mean of
  `|queue_ahead_est - queue_ahead_exact| / (level_qty_at_live + qty)` (each trajectory row holds until the next row;
  pairs spanning zero time use the plain mean; non-finite estimates skipped).
CIs (95% percentile) for the ratio metrics (brier, logloss, fill_rate_bias, qty_bias, overfill_bias) and
`fill_rate_bias_aj`: block
bootstrap of shadows with blocks = session dates (>= 5 dates) else 30-minute buckets of `ts_live`; one draw matrix
`block_counts(n_blocks, bootstrap_b, stable_seed(seed, [b"bootstrap", "zoo_eval"]))` shared by every model,
metric and stratum; replicate = `sum_k count_k num_k / sum_k count_k den_k`. The discordance bootstrap uses
`stable_seed(seed, [b"bootstrap", "zoo_discordance:<metric prefix><model_id>:<stratum_kind>:<stratum>"])`.
Output order: models in first-appearance order, populations (never-cancel, cancel-bearing), strata (all,
queue_decile, liquidity_bucket, instrument ascending), horizons ascending, metrics in the order listed. Undefined
values are not written to `qt.zoo_errors` and are null in the leaderboard (`qt.zoo_summary.leaderboard`).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from queuetruth.errors import QtError
from queuetruth.eval import _cols as C
from queuetruth.stats import (
    aalen_johansen,
    block_counts,
    block_labels,
    bootstrap_seed,
    clustered_mcnemar,
    percentile_ci,
)

DECILE_LEVELS = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
TERCILE_LEVELS = np.array([1.0 / 3.0, 2.0 / 3.0])
ONE_US_NS = 1_000
LOGLOSS_EPS = 1e-6
PARTICLE_FAMILIES = ("databento_mc",)
BOOTSTRAP_TEST_ID = "zoo_eval"
_PRED_COLS = [
    "model_id",
    "shadow_id",
    "qty",
    "horizon_ns",
    "p_fill",
    "e_fill_qty",
    "model_ts_first_fill",
    "truth_filled",
    "truth_ts_first_fill",
    "truth_filled_qty",
    "censored",
    "queue_ahead_at_live",
    "level_qty_at_live",
    "visible_level_qty_at_live",
    "instrument_id",
    "session_date",
    "ts_live",
    "trade_vol_60s",
]


def family(model_id: str) -> str:
    """Family name of a ModelId (`hbt_power3[n=2]` -> `hbt_power3`)."""
    return model_id.split("[", 1)[0]


def _first_order(keys: np.ndarray) -> np.ndarray:
    """Distinct keys in first-appearance order."""
    uniq, first = np.unique(keys, return_index=True)
    return uniq[np.argsort(first, kind="stable")]


def _buckets(values: np.ndarray, valid: np.ndarray, groups: np.ndarray, levels: np.ndarray, top: int) -> np.ndarray:
    out = np.full(values.size, -1, dtype=np.int64)
    for g in np.unique(groups):
        m = (groups == g) & valid
        if m.any():
            q = np.quantile(values[m].astype(float), levels)
            out[m] = np.clip(np.searchsorted(q, values[m].astype(float), side="right"), 0, top)
    return out


def assign_strata(predictions: pa.Table) -> pa.Table:
    """Adds `queue_decile` and `liquidity_bucket` (SPEC 5.6)."""
    C.require(
        predictions,
        ["shadow_id", "instrument_id", "session_date", "queue_ahead_at_live", "trade_vol_60s"],
        "assign_strata",
    )
    t = predictions
    for name in ("queue_decile", "liquidity_bucket"):
        if C.has(t, name):
            t = t.drop_columns([name])
    sid = C.strings(t, "shadow_id")
    if sid.size == 0:
        return t.append_column("queue_decile", pa.array([], pa.uint8())).append_column(
            "liquidity_bucket", pa.array([], pa.uint8())
        )
    _, first, inv = np.unique(sid, return_index=True, return_inverse=True)
    inst, _ = C.ints(t, "instrument_id")
    date, _ = C.ints(t, "session_date")
    qa, qav = C.ints(t, "queue_ahead_at_live")
    tv, tvv = C.ints(t, "trade_vol_60s")
    pairs = np.stack([inst[first], date[first]], axis=1)
    _, ginv = np.unique(pairs, axis=0, return_inverse=True)
    ginv = np.asarray(ginv).reshape(-1)
    dec = _buckets(qa[first], qav[first], ginv, DECILE_LEVELS, 9)[inv]
    liq = _buckets(tv[first], tvv[first], ginv, TERCILE_LEVELS, 2)[inv]
    t = t.append_column("queue_decile", pa.array(np.where(dec >= 0, dec, 0).astype(np.uint8), mask=dec < 0))
    return t.append_column("liquidity_bucket", pa.array(np.where(liq >= 0, liq, 0).astype(np.uint8), mask=liq < 0))


def _none(x: float) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


class _Ratio:
    """Ratio-of-sums statistic with its shared-draw block bootstrap."""

    def __init__(self, counts: np.ndarray) -> None:
        self.counts = counts
        self.nb = counts.shape[1]

    def __call__(self, num: np.ndarray, den: np.ndarray, blocks: np.ndarray) -> tuple[float, float, float]:
        ns = np.bincount(blocks, weights=num, minlength=self.nb)
        ds = np.bincount(blocks, weights=den, minlength=self.nb)
        total = float(ds.sum())
        value = float(ns.sum()) / total if total > 0 else float("nan")
        rn = (self.counts * ns[None, :]).sum(axis=1)
        rd = (self.counts * ds[None, :]).sum(axis=1)
        reps = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
        lo, hi = percentile_ci(reps, 0.05)
        return value, lo, hi


def _planned_life(planned_life: pa.Table, ids: np.ndarray, live_shadow: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per shadow (in `ids` order): planned cancel and administrative end relative to `ts_live` (inf = none)."""
    C.require(planned_life, ["shadow_id", "ts_cancel_planned", "ts_admin_end"], "planned_life")
    psid = C.strings(planned_life, "shadow_id")
    cancel, cancel_v = C.ints(planned_life, "ts_cancel_planned")
    admin, admin_v = C.ints(planned_life, "ts_admin_end")
    pos = {str(s): i for i, s in reversed(list(enumerate(psid)))}
    missing = [str(s) for s in ids if str(s) not in pos]
    if missing:
        raise QtError(
            "QT-INPUT-002",
            f"planned_life: no row for {len(missing)} predicted shadow(s) (first {missing[:3]}); label eligibility "
            "needs every shadow's planned life (audit ruling A2, OC-7)",
        )
    cancel_rel = np.full(ids.size, np.inf)
    admin_rel = np.full(ids.size, np.inf)
    for i, s in enumerate(ids):
        j = pos[str(s)]
        if cancel_v[j]:
            cancel_rel[i] = float(cancel[j] - live_shadow[i])
        if admin_v[j]:
            admin_rel[i] = float(admin[j] - live_shadow[i])
    return cancel_rel, admin_rel


def _aj_fill_reps(time: np.ndarray, cause: np.ndarray, weights: np.ndarray, at: np.ndarray) -> np.ndarray:
    """Aalen-Johansen `CIF_fill` at `at` for each row of integer frequency `weights` (`(B, n)` -> `(B, len(at))`):
    replicate `b` equals `aalen_johansen` on the rows repeated `weights[b]` times (the drawn blocks concatenated)."""
    ut, inv = np.unique(time, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    idx = np.searchsorted(ut, at, side="right") - 1
    out = np.zeros((weights.shape[0], at.size))
    for b in range(weights.shape[0]):
        w = weights[b].astype(float)
        exits = np.bincount(inv, weights=w, minlength=ut.size)
        at_risk = w.sum() - np.concatenate([[0.0], np.cumsum(exits)[:-1]])
        safe = np.where(at_risk > 0, at_risk, 1.0)
        d_all = np.bincount(inv, weights=w * (cause != 0), minlength=ut.size)
        d_fill = np.bincount(inv, weights=w * (cause == 1), minlength=ut.size)
        s = np.cumprod(np.where(at_risk > 0, 1.0 - d_all / safe, 1.0))
        s_left = np.concatenate([[1.0], s[:-1]])
        cif = np.cumsum(np.where(at_risk > 0, s_left * d_fill / safe, 0.0))
        out[b] = np.where(idx >= 0, cif[np.clip(idx, 0, None)], 0.0)
    return out


def without_aborted(predictions: pa.Table) -> pa.Table:
    """The rows that are predictions: a 1.1 `status` of `hbt_312_abort` (faithful hbt pair aborted at its #312
    event at or before the row's deadline) is left out of every metric."""
    if "status" not in predictions.column_names:
        return predictions
    return predictions.filter(pc.is_null(predictions.column("status")))


def evaluate_zoo(
    predictions: pa.Table,
    *,
    h_max_ns: int,
    trajectory: pa.Table | None = None,
    bootstrap_b: int = 1000,
    seed: int = 0,
    planned_life: pa.Table | None = None,
) -> tuple[pa.Table, list[dict[str, Any]]]:
    """Metrics per model, horizon and stratum (`qt.zoo_errors`) and the leaderboard entries. `planned_life` is
    required (`QT-INPUT-001` without it, `QT-INPUT-002` when a predicted shadow has no row): label eligibility is
    never inferred from the outcome-flavoured `censored` column (audit ruling A2, E38 OC-7)."""
    C.require(predictions, _PRED_COLS, "evaluate_zoo")
    if planned_life is None:
        raise QtError(
            "QT-INPUT-001",
            "evaluate_zoo: planned_life (qt.zoo_shadows: shadow_id, ts_cancel_planned, ts_admin_end) is required; "
            "label eligibility is never inferred from the censored flag (audit ruling A2, E38 OC-7)",
            "pass planned_life=pyarrow.parquet.read_table('<zoo out>/zoo_shadows.parquet') or planned_life_table(...)",
        )
    t = assign_strata(without_aborted(predictions))
    model = C.strings(t, "model_id")
    sid = C.strings(t, "shadow_id")
    h, _ = C.ints(t, "horizon_ns")
    p = C.floats(t, "p_fill")
    e_qty = C.floats(t, "e_fill_qty")
    qty = C.floats(t, "qty")
    y, yv = C.bools(t, "truth_filled")
    t_qty = C.floats(t, "truth_filled_qty")
    m_ts, m_tsv = C.ints(t, "model_ts_first_fill")
    t_ts, t_tsv = C.ints(t, "truth_ts_first_fill")
    live, _ = C.ints(t, "ts_live")
    date, _ = C.ints(t, "session_date")
    inst, _ = C.ints(t, "instrument_id")
    dec, decv = C.ints(t, "queue_decile")
    liq, liqv = C.ints(t, "liquidity_bucket")
    level = C.floats(t, "level_qty_at_live")
    vis_level = C.floats(t, "visible_level_qty_at_live")

    rows: dict[str, list[Any]] = {k: [] for k in SCHEMA_ORDER}
    leaderboard: list[dict[str, Any]] = []
    if sid.size == 0:
        return _errors_table(rows), leaderboard

    ids, first, sid_inv = np.unique(sid, return_index=True, return_inverse=True)
    sid_inv = np.asarray(sid_inv).reshape(-1)
    n_shadows = ids.size
    labels, _kind = block_labels(date[first], live[first])
    _, block_of_shadow = np.unique(labels, return_inverse=True)
    blocks = np.asarray(block_of_shadow).reshape(-1)[sid_inv]
    n_blocks = int(blocks.max()) + 1
    counts = block_counts(n_blocks, bootstrap_b, bootstrap_seed(seed, BOOTSTRAP_TEST_ID))
    ratio = _Ratio(counts.astype(float))
    shadow_block = np.asarray(block_of_shadow).reshape(-1)
    horizons = np.unique(h[h <= h_max_ns])

    # A2: outcome-independent label existence and the per-shadow planned life
    cancel_rel, admin_rel = _planned_life(planned_life, ids, live[first])
    eligible = admin_rel[sid_inv] >= h.astype(float)
    censor_rel = np.minimum(admin_rel, float(horizons[-1]) if horizons.size else 0.0)
    usable = yv & eligible
    has_truth = np.zeros(n_shadows, dtype=bool)
    has_truth[sid_inv[yv]] = True
    fill_rel = np.full(n_shadows, np.inf)
    mf = yv & t_tsv
    fill_rel[sid_inv[mf]] = np.maximum(t_ts[mf] - live[mf], 0).astype(float)
    aj_time = np.minimum(np.minimum(fill_rel, cancel_rel), censor_rel)
    aj_cause = np.where(
        fill_rel <= np.minimum(cancel_rel, censor_rel), 1, np.where(cancel_rel <= censor_rel, 2, 0)
    ).astype(np.int64)
    never_cancel = cancel_rel >= float(h_max_ns)
    populations = [("", never_cancel)]
    if (~never_cancel).any():
        populations.append(("cancel_bearing/", ~never_cancel))

    yf = y.astype(float)
    pc = np.clip(p, LOGLOSS_EPS, 1.0 - LOGLOSS_EPS)
    ll = -(yf * np.log(pc) + (1.0 - yf) * np.log(1.0 - pc))
    strata: list[tuple[str, str, np.ndarray]] = [("all", "all", np.ones(sid.size, dtype=bool))]
    strata += [("queue_decile", str(d), decv & (dec == d)) for d in range(10) if (decv & (dec == d)).any()]
    strata += [("liquidity_bucket", str(b), liqv & (liq == b)) for b in range(3) if (liqv & (liq == b)).any()]
    strata += [("instrument", str(i), inst == i) for i in np.unique(inst)]

    def emit(
        m: str, metric: str, hz: int | None, kind: str, stratum: str, v: float, lo: float, hi: float, n: int
    ) -> None:
        if v is None or not np.isfinite(v):
            return
        rows["model_id"].append(m)
        rows["metric"].append(metric)
        rows["horizon_ns"].append(hz)
        rows["stratum_kind"].append(kind)
        rows["stratum"].append(stratum)
        rows["value"].append(float(v))
        rows["ci_lo"].append(_none(lo))
        rows["ci_hi"].append(_none(hi))
        rows["n"].append(int(n))

    cif = {}
    cif_reps = {}
    for prefix, pop in populations:
        ok = pop & has_truth
        if ok.any() and horizons.size:
            aj = aalen_johansen(aj_time[ok], aj_cause[ok], causes=(1,))[1]
            cif[prefix] = np.asarray(aj.at(horizons.astype(float), before=0.0), dtype=float)
            # the same block draws as every ratio metric: replicate b weighs shadow i by its block's multiplicity
            cif_reps[prefix] = _aj_fill_reps(
                aj_time[ok], aj_cause[ok], counts[:, shadow_block[ok]], horizons.astype(float)
            )
    qtrack = _qtrack(trajectory, sid, first, level, vis_level, qty) if trajectory is not None else {}
    nan = float("nan")

    for m in _first_order(model):
        mm = model == m
        entry: dict[str, Any] = {
            "model_id": str(m),
            "brier": {},
            "logloss": {},
            "fill_rate_bias": {},
            "overfill_bias": {"value": None, "ci_lo": None, "ci_hi": None},
            "qty_bias": None,
            "ttf_log_ratio_median": None,
            "ttf_iad": None,
            "qtrack_mae": None,
            "mcnemar": {"b": 0, "c": 0, "p_exact": None},
        }
        for prefix, pop in populations:
            lead = prefix == ""
            in_pop = mm & pop[sid_inv]
            for kind, stratum, smask in strata:
                for hz in horizons:
                    sel = in_pop & (h == hz) & usable & smask
                    n = int(sel.sum())
                    if n == 0:
                        continue
                    bs = blocks[sel]
                    ones = np.ones(n)
                    stats = {
                        "brier": ratio((p[sel] - yf[sel]) ** 2, ones, bs),
                        "logloss": ratio(ll[sel], ones, bs),
                        "fill_rate_bias": ratio(p[sel] - yf[sel], ones, bs),
                    }
                    tq_ok = np.isfinite(t_qty[sel])
                    if tq_ok.any():
                        stats["qty_bias"] = ratio(
                            np.where(tq_ok, e_qty[sel] - t_qty[sel], 0.0), np.where(tq_ok, qty[sel], 0.0), bs
                        )
                    for metric, (v, lo, hi) in stats.items():
                        emit(m, prefix + metric, int(hz), kind, stratum, v, lo, hi, n)
                    if lead and kind == "all":
                        key = str(int(hz))
                        entry["brier"][key] = _none(stats["brier"][0])
                        entry["logloss"][key] = _none(stats["logloss"][0])
                    if hz != h_max_ns:
                        continue
                    model_filled = p[sel] >= 0.5
                    truth = y[sel]
                    v, lo, hi = ratio(model_filled.astype(float) - truth.astype(float), ones, bs)
                    emit(m, prefix + "overfill_bias", int(hz), kind, stratum, v, lo, hi, n)
                    test_id = f"zoo_discordance:{prefix}{m}:{kind}:{stratum}"
                    cd = clustered_mcnemar(truth, model_filled, bs, b=bootstrap_b, seed=bootstrap_seed(seed, test_id))
                    emit(m, prefix + "discordance_p_primary", int(hz), kind, stratum, cd.p_primary, nan, nan, n)
                    if cd.p_obuchowski is not None:
                        emit(
                            m, prefix + "discordance_p_obuchowski", int(hz), kind, stratum, cd.p_obuchowski, nan, nan, n
                        )
                    emit(m, prefix + "discordance_p_bootstrap", int(hz), kind, stratum, cd.p_bootstrap, nan, nan, n)
                    emit(m, prefix + "n_clusters", int(hz), kind, stratum, float(cd.n_clusters), nan, nan, n)
                    if cd.n_effective is not None:
                        emit(m, prefix + "n_effective", int(hz), kind, stratum, cd.n_effective, nan, nan, n)
                    mc = cd.mcnemar
                    emit(m, prefix + "mcnemar_b", int(hz), kind, stratum, float(mc.b), nan, nan, n)
                    emit(m, prefix + "mcnemar_c", int(hz), kind, stratum, float(mc.c), nan, nan, n)
                    emit(m, prefix + "mcnemar_p_exact", int(hz), kind, stratum, mc.p_exact, nan, nan, n)
                    if lead and kind == "all":
                        entry["overfill_bias"] = {"value": _none(v), "ci_lo": _none(lo), "ci_hi": _none(hi)}
                        entry["mcnemar"] = {"b": mc.b, "c": mc.c, "p_exact": _none(mc.p_exact)}
                        if "qty_bias" in stats:
                            entry["qty_bias"] = _none(stats["qty_bias"][0])
            # time to fill (stratum all)
            if family(str(m)) not in PARTICLE_FAMILIES and h_max_ns in horizons:
                sel = in_pop & (h == h_max_ns) & usable & y & t_tsv & m_tsv & (m_ts - live <= h_max_ns)
                if sel.any():
                    mt = np.maximum(m_ts[sel] - live[sel], ONE_US_NS).astype(float)
                    tt = np.maximum(t_ts[sel] - live[sel], ONE_US_NS).astype(float)
                    v = float(np.median(np.log(mt / tt)))
                    emit(m, prefix + "ttf_log_ratio_median", int(h_max_ns), "all", "all", v, nan, nan, int(sel.sum()))
                    if lead:
                        entry["ttf_log_ratio_median"] = _none(v)
            if prefix in cif:
                iad = 0.0
                prev = 0.0
                # SPEC 5.6: the sum runs over the run's horizons plus h_max, so it always spans [0, h_max]; without
                # h_max rows the last term (h_max - h_last) has no eligible row and ttf_iad is null, never a sum
                # over a shorter span divided by h_max
                complete = bool(h_max_ns in horizons)
                for j, hz in enumerate(horizons):
                    # F5: rows whose label exists at hz (planned life >= hz): a row cut by the administrative end
                    # before hz holds a truncated forecast ("fill by end of data"), the AJ CIF corrects for that cut
                    mh = in_pop & (h == hz) & usable
                    if not mh.any():
                        complete = False
                        continue
                    diff = float(np.mean(p[mh])) - float(cif[prefix][j])
                    # 95% CI on the shared block draws: replicate mean of p over the drawn eligible rows minus the
                    # replicate AJ CIF of the drawn shadows
                    nb = counts.shape[1]
                    ps = np.bincount(blocks[mh], weights=p[mh], minlength=nb)
                    ns = np.bincount(blocks[mh], minlength=nb).astype(float)
                    rd = counts @ ns
                    rep_p = np.where(rd > 0, (counts @ ps) / np.where(rd > 0, rd, 1.0), np.nan)
                    lo, hi = percentile_ci(rep_p - cif_reps[prefix][:, j], 0.05)
                    emit(m, prefix + "fill_rate_bias_aj", int(hz), "all", "all", diff, lo, hi, int(mh.sum()))
                    if lead:
                        # A2 / SPEC 5.6: the leaderboard bias is the AJ figure (truth = AJ CIF, never mean(y))
                        entry["fill_rate_bias"][str(int(hz))] = {
                            "value": _none(diff),
                            "ci_lo": _none(lo),
                            "ci_hi": _none(hi),
                        }
                    iad += abs(diff) * (float(hz) - prev)
                    prev = float(hz)
                if complete:
                    v = iad / float(h_max_ns)
                    emit(m, prefix + "ttf_iad", None, "all", "all", v, nan, nan, int((in_pop & yv).sum()))
                    if lead:
                        entry["ttf_iad"] = _none(v)
        for hz in horizons:
            key = str(int(hz))
            entry["brier"].setdefault(key, None)
            entry["logloss"].setdefault(key, None)
            entry["fill_rate_bias"].setdefault(key, {"value": None, "ci_lo": None, "ci_hi": None})
        if str(m) in qtrack:
            v, n_pairs = qtrack[str(m)]
            emit(m, "qtrack_mae", None, "all", "all", v, nan, nan, n_pairs)
            entry["qtrack_mae"] = _none(v)
        leaderboard.append(entry)
    return _errors_table(rows), leaderboard


def planned_life_table(
    shadow_ids: list[str], ts_cancel_planned: list[int | None], ts_admin_end: list[int | None]
) -> pa.Table:
    """The `planned_life` input of `evaluate_zoo` (timestamps in ns UTC; None = none)."""
    ts = pa.timestamp("ns", tz="UTC")
    return pa.table(
        {
            "shadow_id": pa.array(shadow_ids, pa.utf8()),
            "ts_cancel_planned": pa.array(ts_cancel_planned, pa.int64()).cast(ts),
            "ts_admin_end": pa.array(ts_admin_end, pa.int64()).cast(ts),
        }
    )


SCHEMA_ORDER = ["model_id", "metric", "horizon_ns", "stratum_kind", "stratum", "value", "ci_lo", "ci_hi", "n"]


def _errors_table(rows: dict[str, list[Any]]) -> pa.Table:
    cols: dict[str, Any] = dict(rows)
    cols["stratum_kind"] = C.dict_array(rows["stratum_kind"], "qt.zoo_errors", "stratum_kind")
    return C.build("qt.zoo_errors", cols)


def _qtrack(
    trajectory: pa.Table,
    sid: np.ndarray,
    first: np.ndarray,
    level: np.ndarray,
    vis_level: np.ndarray,
    qty: np.ndarray,
) -> dict[str, tuple[float, int]]:
    """Per model: (mean over pairs of the time-weighted normalized tracking error, number of pairs)."""
    C.require(trajectory, ["model_id", "shadow_id", "ts_event", "queue_ahead_est", "queue_ahead_exact"], "trajectory")
    norm_by_sid: dict[str, float] = {}
    for i in first:
        base = level[i] if np.isfinite(level[i]) else vis_level[i]
        norm_by_sid[str(sid[i])] = float(base + qty[i])
    tm = C.strings(trajectory, "model_id")
    ts_sid = C.strings(trajectory, "shadow_id")
    ts, _ = C.ints(trajectory, "ts_event")
    est = C.floats(trajectory, "queue_ahead_est")
    exact = C.floats(trajectory, "queue_ahead_exact")
    out: dict[str, tuple[float, int]] = {}
    if tm.size == 0:
        return out
    keys = np.array([f"{a}\x00{b}" for a, b in zip(tm, ts_sid, strict=True)], dtype=object)
    per_model: dict[str, list[float]] = {}
    order = _first_order(keys)
    for key in order:
        idx = np.flatnonzero(keys == key)
        m_id, s_id = str(key).split("\x00", 1)
        norm = norm_by_sid.get(s_id)
        if norm is None or not np.isfinite(norm) or norm <= 0:
            continue
        idx = idx[np.argsort(ts[idx], kind="stable")]
        err = np.abs(est[idx] - exact[idx]) / norm
        dt = np.diff(ts[idx]).astype(float)
        good = np.isfinite(err)
        if not good.any():
            continue
        w = np.append(dt, 0.0)
        if np.sum(w[good]) > 0:
            v = float(np.sum(err[good] * w[good]) / np.sum(w[good]))
        else:
            v = float(np.mean(err[good]))
        per_model.setdefault(m_id, []).append(v)
    for m_id, vals in per_model.items():
        out[m_id] = (float(np.mean(vals)), len(vals))
    return out
