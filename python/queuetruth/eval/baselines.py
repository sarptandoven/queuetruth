"""Python baseline predictors producing `qt.zoo_predictions` rows for the zoo's shadows (SPEC 5.6).

Inputs (`train`, `test`) are shadow tables: `qt.zoo_predictions` rows of one model (any; only identity and truth
columns are read) with the landmark-0 feature columns of `qt.features` joined by `shadow_id = order_ref`
(`join_features` builds them). Rows may repeat per horizon. Per shadow the first row supplies `qty`,
`instrument_id`, `session_date`, `ts_live`, `queue_ahead_at_live`, `level_qty_at_live`,
`visible_level_qty_at_live`, `out_of_view_at_live`, `trade_vol_60s` and the features; truth for an output horizon is
copied from the test row with that `horizon_ns` (null truth and `censored = false` if absent). Optional columns
`ts_cancel_planned` and `ts_admin_end` (planned life, audit ruling A2, as in `evaluate_zoo`) make label existence
outcome-independent (`ts_admin_end >= ts_live + h`) and turn a planned own cancel into a competing risk; without them
`censored` is the eligibility flag.

Time-of-day buckets are cut on the exchange-local, DST-safe session clock of `queuetruth.eval._tod` (review F8):
an optional `midnight_utc_ns` column (the instrument-day's local midnight), else the landmark-0 `tod_sin` / `tod_cos`
features joined by `join_features`, else the UTC clock.

- `climatology_aj`: train Aalen-Johansen `CIF_fill(h)` by stratum instrument x side_sign x dist bucket
  (`dist_ticks <= -1`, `< 0.5` -> 0, `< 2` -> 1, else `>= 2`; null -> its own bucket) x 30-minute time-of-day bucket
  of `ts_live` (session clock above); strata with fewer than 50
  train shadows fall back to instrument x side_sign, then to all train shadows. Train shadow times: exact first fill
  `truth_ts_first_fill - ts_live` (fill), planned own cancel (competing), else censored at the administrative end
  (without planned life: the last horizon before the first horizon flagged `censored`, or the largest horizon).
  `e_fill_qty = p * qty`; `model_ts_first_fill` null.
- `mmw_bucket`: train empirical fill rate (eligible rows with truth) per horizon by 15-minute time-of-day bucket
  of `ts_live` (session clock) x `imb_l1` quintile at arrival (cut points = train 0.2..0.8 quantiles,
  `searchsorted(side="right")`); an empty cell or a null `imb_l1` falls back to the time-of-day bucket, then to all
  train shadows (NaN if the train set has no usable row at that horizon). `e_fill_qty = p * qty`;
  `model_ts_first_fill` null.
- `back_of_queue_rate` (et `BackOfQueueModel`): `rate = trade_vol_px_10s / 10 s`; fills by `h` iff
  `rate * h > level_qty + qty` (`level_qty` = visible level qty at landmark 0); `p in {0, 1}`,
  `e_fill_qty = qty * p`; `model_ts_first_fill = ts_live + floor((level_qty + qty) / rate) + 1` ns when that is within
  the largest output horizon, else null. Needs no train data.
Output rows: test shadows in first-appearance order x `horizons_ns` ascending; `model_id` = the kind.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from queuetruth.errors import QtError
from queuetruth.eval import _cols as C
from queuetruth.eval._tod import session_clock_ns
from queuetruth.stats import aalen_johansen

KINDS = ("climatology_aj", "mmw_bucket", "back_of_queue_rate")
MIN_STRATUM_ORDERS = 50
HALF_HOUR_NS = 1_800_000_000_000
QUARTER_HOUR_NS = 900_000_000_000
DAY_NS = 86_400_000_000_000
TEN_S_NS = 10_000_000_000
QUINTILE_LEVELS = np.array([0.2, 0.4, 0.6, 0.8])
_SHADOW_COLS = [
    "shadow_id",
    "qty",
    "horizon_ns",
    "truth_filled",
    "truth_ts_first_fill",
    "truth_filled_qty",
    "censored",
    "queue_ahead_at_live",
    "level_qty_at_live",
    "visible_level_qty_at_live",
    "out_of_view_at_live",
    "instrument_id",
    "session_date",
    "ts_live",
    "trade_vol_60s",
]
_FEATURES_NEEDED = {
    "climatology_aj": ["side_sign", "dist_ticks"],
    "mmw_bucket": ["imb_l1"],
    "back_of_queue_rate": ["trade_vol_px_10s", "level_qty"],
}


def join_features(predictions: pa.Table, features: pa.Table, model_id: str | None = None) -> pa.Table:
    """Shadow table: rows of one model of `qt.zoo_predictions` (default: the first) + landmark-0 feature columns."""
    C.require(predictions, ["model_id", "shadow_id"], "join_features predictions")
    C.require(features, ["order_ref", "landmark_ns", "side_sign"], "join_features features")
    models = C.strings(predictions, "model_id")
    mid = model_id if model_id is not None else (str(models[0]) if models.size else None)
    preds = predictions.filter(pc.equal(predictions["model_id"], mid)) if mid is not None else predictions
    f0 = features.filter(pc.equal(features["landmark_ns"], 0))
    keep = [
        n
        for n in f0.column_names
        if n not in set(preds.column_names) | {"population", "ts_landmark", "landmark_ns", "event_ns", "cause"}
    ]
    refs = C.strings(f0, "order_ref")
    pos = {str(r): i for i, r in reversed(list(enumerate(refs)))}
    sids = C.strings(preds, "shadow_id")
    take = np.array([pos.get(str(s), -1) for s in sids], dtype=np.int64)
    mask = take < 0
    out = preds
    for name in keep:
        if name == "order_ref":
            continue
        col = f0[name].combine_chunks() if f0.num_rows else pa.array([], f0.schema.field(name).type)
        taken = col.take(pa.array(np.where(mask, 0, take), mask=mask)) if out.num_rows else pa.array([], col.type)
        out = out.append_column(name, taken)
    return out


class _Shadows:
    """Per-shadow attributes and per-(shadow, horizon) truth of a shadow table."""

    def __init__(self, t: pa.Table, what: str, features: list[str]) -> None:
        C.require(t, _SHADOW_COLS + features, what)
        sid = C.strings(t, "shadow_id")
        uniq, first = np.unique(sid, return_index=True)
        order = np.argsort(first, kind="stable")
        self.first = first[order]
        self.ids = uniq[order]
        index = {str(s): i for i, s in enumerate(self.ids)}
        self.row_shadow = np.array([index[str(s)] for s in sid], dtype=np.int64)
        self.table = t
        self.h, _ = C.ints(t, "horizon_ns")
        self.y, self.yv = C.bools(t, "truth_filled")
        self.cens, _ = C.bools(t, "censored")
        self.t_ts, self.t_tsv = C.ints(t, "truth_ts_first_fill")
        self.live_rows, _ = C.ints(t, "ts_live")
        f = self.first
        self.live = self.live_rows[f]
        self.qty = C.floats(t, "qty")[f]
        self.inst = C.ints(t, "instrument_id")[0][f]
        self.n = self.ids.size
        # A2: optional planned life columns (see evaluate_zoo); without them `censored` is the eligibility flag
        self.cancel_rel = np.full(self.n, np.inf)
        self.admin_rel = None
        if C.has(t, "ts_cancel_planned"):
            v, ok = C.ints(t, "ts_cancel_planned")
            self.cancel_rel = np.where(ok[f], (v[f] - self.live).astype(float), np.inf)
        if C.has(t, "ts_admin_end"):
            v, ok = C.ints(t, "ts_admin_end")
            self.admin_rel = np.where(ok[f], (v[f] - self.live).astype(float), np.inf)
            self.eligible = self.admin_rel[self.row_shadow] >= self.h.astype(float)
        else:
            self.eligible = ~self.cens

    def feature(self, name: str) -> np.ndarray:
        return C.floats(self.table, name)[self.first]

    def clock(self) -> np.ndarray:
        """Per shadow: DST-safe exchange-local time of day of `ts_live` in ns (module docstring)."""
        f, t = self.first, self.table
        mid = None
        if C.has(t, "midnight_utc_ns"):
            v, ok = C.ints(t, "midnight_utc_ns")
            mid = (v[f], ok[f])
        tods = C.has(t, "tod_sin") and C.has(t, "tod_cos")
        return session_clock_ns(
            self.live,
            self.inst,
            C.ints(t, "session_date")[0][f],
            midnight_utc_ns=mid,
            tod_sin=self.feature("tod_sin") if tods else None,
            tod_cos=self.feature("tod_cos") if tods else None,
        )[0]

    def surv_data(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(time, cause, usable) per shadow for the train Aalen-Johansen: cause 1 fill, 2 planned own cancel, 0
        censored at the administrative end (or, without planned life, the legacy flag-derived censor time)."""
        horizons = np.unique(self.h)
        usable = np.zeros(self.n, dtype=bool)
        usable[self.row_shadow[self.yv]] = True
        fill = np.full(self.n, np.inf)
        m = self.yv & self.t_tsv
        fill[self.row_shadow[m]] = np.maximum(self.t_ts[m] - self.live_rows[m], 0).astype(float)
        last = float(horizons[-1]) if horizons.size else 0.0
        if self.admin_rel is not None:
            cens_t = np.minimum(self.admin_rel, last)
        else:
            first_c = np.full(self.n, np.inf)
            mc = self.yv & self.cens
            np.minimum.at(first_c, self.row_shadow[mc], self.h[mc].astype(float))
            pos = np.searchsorted(horizons, first_c, side="left")
            cens_t = np.where(np.isinf(first_c), last, np.where(pos > 0, horizons[np.maximum(pos - 1, 0)], 0.0))
        time = np.minimum(np.minimum(fill, self.cancel_rel), cens_t)
        cause = np.where(fill <= np.minimum(self.cancel_rel, cens_t), 1, np.where(self.cancel_rel <= cens_t, 2, 0))
        return time, cause.astype(np.int64), usable


def _dist_bucket(d: np.ndarray) -> np.ndarray:
    return np.where(np.isnan(d), 4, np.where(d <= -1, 0, np.where(d < 0.5, 1, np.where(d < 2, 2, 3))))


def _keys(*cols: np.ndarray) -> np.ndarray:
    return np.array(["|".join(str(int(v)) for v in row) for row in zip(*cols, strict=True)], dtype=object)


def _climatology(train: _Shadows, test: _Shadows, horizons: np.ndarray) -> np.ndarray:
    time, cause, ok = train.surv_data()
    if not ok.any():
        raise QtError("QT-INPUT-002", "climatology_aj: train table has no shadow with an exact arm")
    tr_side = train.feature("side_sign")
    te_side = test.feature("side_sign")
    tr_fine = _keys(train.inst, tr_side, _dist_bucket(train.feature("dist_ticks")), train.clock() // HALF_HOUR_NS)
    te_fine = _keys(test.inst, te_side, _dist_bucket(test.feature("dist_ticks")), test.clock() // HALF_HOUR_NS)
    tr_coarse = _keys(train.inst, tr_side)
    te_coarse = _keys(test.inst, te_side)
    cache: dict[tuple[str, str], np.ndarray] = {}

    def cif(level: str, key: str, mask: np.ndarray) -> np.ndarray:
        if (level, key) not in cache:
            aj = aalen_johansen(time[mask], cause[mask], causes=(1,))[1]
            cache[(level, key)] = np.asarray(aj.at(horizons.astype(float), before=0.0), dtype=float)
        return cache[(level, key)]

    out = np.empty((test.n, horizons.size))
    for i in range(test.n):
        fine = ok & (tr_fine == te_fine[i])
        if fine.sum() >= MIN_STRATUM_ORDERS:
            out[i] = cif("fine", str(te_fine[i]), fine)
            continue
        coarse = ok & (tr_coarse == te_coarse[i])
        out[i] = cif("coarse", str(te_coarse[i]), coarse) if coarse.any() else cif("all", "", ok)
    return out


def _mmw(train: _Shadows, test: _Shadows, horizons: np.ndarray) -> np.ndarray:
    imb_tr = train.feature("imb_l1")
    imb_te = test.feature("imb_l1")
    finite = np.isfinite(imb_tr)
    cuts = np.quantile(imb_tr[finite], QUINTILE_LEVELS) if finite.any() else np.zeros(0)
    q_tr = np.where(finite, np.searchsorted(cuts, np.nan_to_num(imb_tr), side="right"), -1)
    q_te = np.where(np.isfinite(imb_te), np.searchsorted(cuts, np.nan_to_num(imb_te), side="right"), -1)
    tod_tr = train.clock() // QUARTER_HOUR_NS
    tod_te = test.clock() // QUARTER_HOUR_NS
    row_ok = train.yv & train.eligible
    out = np.full((test.n, horizons.size), np.nan)
    for j, hz in enumerate(horizons):
        rows = row_ok & (train.h == hz)
        s = train.row_shadow[rows]
        yv = train.y[rows].astype(float)
        if s.size == 0:
            continue
        overall = float(np.mean(yv))
        for i in range(test.n):
            in_tod = tod_tr[s] == tod_te[i]
            cell = in_tod & (q_tr[s] == q_te[i]) if q_te[i] >= 0 else np.zeros(s.size, dtype=bool)
            if cell.any():
                out[i, j] = float(np.mean(yv[cell]))
            elif in_tod.any():
                out[i, j] = float(np.mean(yv[in_tod]))
            else:
                out[i, j] = overall
    return out


def baseline_predictions(kind: str, train: pa.Table, test: pa.Table, horizons_ns: Sequence[int]) -> pa.Table:
    """`qt.zoo_predictions` rows of a Python baseline: climatology_aj | mmw_bucket | back_of_queue_rate."""
    if kind not in KINDS:
        raise QtError("QT-CONFIG-001", f"baseline kind must be one of {', '.join(KINDS)}, got {kind!r}")
    horizons = np.unique(np.asarray(list(horizons_ns), dtype=np.int64))
    feats = _FEATURES_NEEDED[kind]
    te = _Shadows(test, f"baseline {kind} test", feats)
    model_ts = np.full((te.n, horizons.size), -1, dtype=np.int64)
    if kind == "back_of_queue_rate":
        rate = np.nan_to_num(te.feature("trade_vol_px_10s")) / TEN_S_NS
        need = np.nan_to_num(te.feature("level_qty")) + te.qty
        p = (rate[:, None] * horizons[None, :].astype(float) > need[:, None]).astype(float)
        e = p * te.qty[:, None]
        limit = float(horizons[-1]) if horizons.size else 0.0
        with np.errstate(divide="ignore", invalid="ignore"):
            t_star = np.where(rate > 0, need / np.where(rate > 0, rate, 1.0), np.inf)
        ok = t_star < limit
        first = np.where(ok, te.live + np.floor(np.where(ok, t_star, 0)).astype(np.int64) + 1, -1)
        model_ts[:] = first[:, None]
    else:
        tr = _Shadows(train, f"baseline {kind} train", feats)
        p = _climatology(tr, te, horizons) if kind == "climatology_aj" else _mmw(tr, te, horizons)
        e = p * te.qty[:, None]
    return _assemble(kind, te, horizons, p, e, model_ts)


def _assemble(
    kind: str, te: _Shadows, horizons: np.ndarray, p: np.ndarray, e: np.ndarray, model_ts: np.ndarray
) -> pa.Table:
    nh = horizons.size
    shadow_idx = np.repeat(np.arange(te.n), nh)
    h_out = np.tile(horizons, te.n)
    src = np.full(te.n * nh, -1, dtype=np.int64)
    key = {(int(s), int(hz)): r for r, (s, hz) in enumerate(zip(te.row_shadow, te.h, strict=True))}
    for k, (s, hz) in enumerate(zip(shadow_idx, h_out, strict=True)):
        src[k] = key.get((int(s), int(hz)), -1)
    found = src >= 0
    srcs = pa.array(np.where(found, src, 0), mask=~found)
    t = te.table
    first = pa.array(te.first[shadow_idx])

    def per_row(name: str) -> pa.Array:
        return t[name].combine_chunks().take(srcs) if t.num_rows else pa.array([], t.schema.field(name).type)

    def per_shadow(name: str) -> pa.Array:
        return t[name].combine_chunks().take(first)

    censored = per_row("censored").fill_null(False)
    mts = model_ts.reshape(-1)
    cols = {
        "model_id": pa.array([kind] * (te.n * nh), pa.utf8()),
        "shadow_id": per_shadow("shadow_id"),
        "qty": per_shadow("qty"),
        "horizon_ns": pa.array(h_out, pa.int64()),
        "p_fill": pa.array(p.reshape(-1), pa.float64()),
        "e_fill_qty": pa.array(e.reshape(-1), pa.float64()),
        "model_ts_first_fill": pa.array(np.where(mts >= 0, mts, 0), mask=mts < 0).cast(pa.timestamp("ns", tz="UTC")),
        "truth_filled": per_row("truth_filled"),
        "truth_ts_first_fill": per_row("truth_ts_first_fill"),
        "truth_filled_qty": per_row("truth_filled_qty"),
        "censored": censored,
        "queue_ahead_at_live": per_shadow("queue_ahead_at_live"),
        "level_qty_at_live": per_shadow("level_qty_at_live"),
        "visible_level_qty_at_live": per_shadow("visible_level_qty_at_live"),
        "out_of_view_at_live": per_shadow("out_of_view_at_live"),
        "instrument_id": per_shadow("instrument_id"),
        "session_date": per_shadow("session_date"),
        "ts_live": per_shadow("ts_live"),
        "trade_vol_60s": per_shadow("trade_vol_60s"),
    }
    schema = C.SCHEMAS["qt.zoo_predictions"]
    arrays = [cols[f.name].cast(f.type) for f in schema]
    return pa.Table.from_arrays(arrays, schema=schema)
