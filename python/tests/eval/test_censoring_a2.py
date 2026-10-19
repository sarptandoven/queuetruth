"""Audit ruling A2 regression (audit check cens.py, proposed O-ZOO-7): with outcome-independent label existence and
own cancels as a competing risk, a perfectly calibrated model has a fill-rate bias CI containing 0 at every horizon
and AJ-based ttf_iad near 0; the old SPEC rule (drop unfilled rows whose life ended early, 1 - KM) is biased."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pyarrow as pa
import pytest

from queuetruth.errors import QtError
from queuetruth.eval import evaluate_zoo, planned_life_table
from queuetruth.schemas import SCHEMAS

S = 1_000_000_000
DAY = 86_400 * S
HORIZONS = (10 * S, 60 * S)
HMAX = 60 * S
EPOCH = date(1970, 1, 1)


def _simulate(rule: str, seed: int = 20260118):
    rng = np.random.default_rng(seed)
    n = 4000
    fill = rng.exponential(30.0, n) * S  # exact first fill time after ts_live (ns)
    cancel_bearing = np.arange(n) % 2 == 1
    cancel = np.where(cancel_bearing, rng.uniform(0, 120, n) * S, np.inf)  # shared own cancel (mirror-like)
    admin = np.where(rng.random(n) < 0.15, rng.uniform(0, 120, n) * S, np.inf)  # session end / data end
    day = (np.arange(n) // 2) % 20
    live = 20_460 * DAY + day * DAY + 36_000 * S + (np.arange(n) // 40) * S
    filled_at = np.where(fill <= np.minimum(cancel, admin), fill, np.inf)
    rows = []
    for i in range(n):
        for h in HORIZONS:
            life = min(h, cancel[i])
            p = 1.0 - np.exp(-life / (30.0 * S))  # perfectly calibrated, knows the shared cancel
            y = bool(filled_at[i] <= h)
            if rule == "a2":
                censored = bool(admin[i] < h)  # outcome-independent
            else:  # old SPEC 5.3.2: !filled && ts_live + h > censor_time, cancel counted as censoring
                censored = (not y) and min(cancel[i], admin[i]) < h
            rows.append(
                {
                    "model_id": "calibrated",
                    "shadow_id": f"s{i}",
                    "qty": 1,
                    "horizon_ns": h,
                    "p_fill": float(p),
                    "e_fill_qty": float(p),
                    "model_ts_first_fill": None,
                    "truth_filled": y,
                    "truth_ts_first_fill": int(live[i] + filled_at[i]) if np.isfinite(filled_at[i]) else None,
                    "truth_filled_qty": int(y),
                    "censored": censored,
                    "queue_ahead_at_live": i % 50,
                    "level_qty_at_live": 50,
                    "visible_level_qty_at_live": 50,
                    "out_of_view_at_live": False,
                    "instrument_id": 1,
                    "session_date": EPOCH + timedelta(days=20_460 + int(day[i])),
                    "ts_live": int(live[i]),
                    "trade_vol_60s": i % 7,
                }
            )
    table = pa.Table.from_pylist(rows, schema=SCHEMAS["qt.zoo_predictions"])
    life = planned_life_table(
        [f"s{i}" for i in range(n)],
        [int(live[i] + cancel[i]) if np.isfinite(cancel[i]) else None for i in range(n)],
        [int(live[i] + admin[i]) if np.isfinite(admin[i]) else None for i in range(n)],
    )
    return table, life


def _get(errors: pa.Table, metric: str, h: int | None):
    hit = [
        r for r in errors.to_pylist() if r["metric"] == metric and r["horizon_ns"] == h and r["stratum_kind"] == "all"
    ]
    assert len(hit) == 1, metric
    return hit[0]


def test_o_zoo_7_calibrated_model_unbiased_under_a2() -> None:
    table, life = _simulate("a2")
    errors, board = evaluate_zoo(table, h_max_ns=HMAX, bootstrap_b=300, seed=0, planned_life=life)
    for prefix in ("", "cancel_bearing/"):
        for h in HORIZONS:
            rec = _get(errors, prefix + "fill_rate_bias", h)
            assert rec["ci_lo"] <= 0.0 <= rec["ci_hi"], (prefix, h, rec)
            aj = _get(errors, prefix + "fill_rate_bias_aj", h)
            assert abs(aj["value"]) < 0.02, (prefix, h)
            assert aj["ci_lo"] <= 0.0 <= aj["ci_hi"], (prefix, h, aj)  # AJ bias carries its block-bootstrap CI
        assert _get(errors, prefix + "ttf_iad", None)["value"] < 0.02, prefix
        assert _get(errors, prefix + "discordance_p_primary", HMAX)["value"] > 0.0
    # the leaderboard is the never-cancel population only
    # A2 / SPEC 5.6 (open conflict 1): the leaderboard bias is the AJ figure with its CI, not mean(p) - mean(y)
    lb = board[0]
    for h in HORIZONS:
        rec = _get(errors, "fill_rate_bias_aj", h)
        got = lb["fill_rate_bias"][str(h)]
        assert (got["value"], got["ci_lo"], got["ci_hi"]) == (rec["value"], rec["ci_lo"], rec["ci_hi"])
        assert got["ci_lo"] <= 0.0 <= got["ci_hi"]
    rec = _get(errors, "fill_rate_bias", HMAX)
    # population: no planned cancel before ts_live + h_max; label exists iff the administrative end is after ts_live + h
    live = dict(zip(table["shadow_id"].to_pylist(), table["ts_live"].cast(pa.int64()).to_pylist(), strict=True))
    ints = pa.table({k: life[k].cast(pa.int64()) if k != "shadow_id" else life[k] for k in life.column_names})
    expected = 0
    for r in ints.to_pylist():
        t0 = live[r["shadow_id"]]
        cancel, admin = r["ts_cancel_planned"], r["ts_admin_end"]
        never_cancel = cancel is None or cancel - t0 >= HMAX
        expected += never_cancel and (admin is None or admin - t0 >= HMAX)
    assert rec["n"] == expected


def test_o_zoo_7_old_rule_is_biased(planned) -> None:
    table, _ = _simulate("old")
    # open conflict 2 (OC-7): no planned-life table is an error, never a fallback to the censored flag
    with pytest.raises(QtError) as ei:
        evaluate_zoo(table, h_max_ns=HMAX, bootstrap_b=300, seed=0)
    assert ei.value.code == "QT-INPUT-001"
    # the old rule spelled out as a planned life built from the outcome-dependent flag
    errors, _ = evaluate_zoo(table, h_max_ns=HMAX, bootstrap_b=300, seed=0, planned_life=planned(table))
    rec = _get(errors, "fill_rate_bias", HMAX)
    assert rec["ci_hi"] < 0.0  # outcome-dependent exclusion makes a calibrated model look like an underfiller


def test_f5_aj_bias_and_ttf_iad_use_label_eligible_rows() -> None:
    """Review F5 (`exp_zoo_aj_truncated.py`): half the shadows' data ends 5 s after entry, so the zoo's forecast for
    them at 10 s is truncated ("fill by end of data") while the AJ truth corrects for that censoring. Averaging the
    forecast over every row gave fill_rate_bias_aj(10 s) = -0.118 and ttf_iad = 0.106 for a perfect model."""
    rng = np.random.default_rng(3)
    n, mean_s, horizons = 2000, 10.0, (1, 10)
    rows, admin = [], []
    for i in range(n):
        live = 20_460 * DAY + 36_000 * S + i * S
        t_fill = rng.exponential(mean_s)
        end_s = 5.0 if i % 2 else 1e9
        for h in horizons:
            seen = min(h, end_s)
            rows.append(
                {
                    "model_id": "calibrated",
                    "shadow_id": f"s{i}",
                    "qty": 1,
                    "horizon_ns": h * S,
                    "p_fill": float(1.0 - np.exp(-seen / mean_s)),  # exact for what the run can observe
                    "e_fill_qty": float(1.0 - np.exp(-seen / mean_s)),
                    "model_ts_first_fill": None,
                    "truth_filled": bool(t_fill <= seen),
                    "truth_ts_first_fill": int(live + t_fill * S) if t_fill <= end_s else None,
                    "truth_filled_qty": int(t_fill <= seen),
                    "censored": h > end_s,
                    "queue_ahead_at_live": i % 50,
                    "level_qty_at_live": 50,
                    "visible_level_qty_at_live": 50,
                    "out_of_view_at_live": False,
                    "instrument_id": 1,
                    "session_date": EPOCH + timedelta(days=20_460),
                    "ts_live": live,
                    "trade_vol_60s": i % 7,
                }
            )
        admin.append(None if end_s > 1e8 else int(live + end_s * S))
    table = pa.Table.from_pylist(rows, schema=SCHEMAS["qt.zoo_predictions"])
    life = planned_life_table([f"s{i}" for i in range(n)], [None] * n, admin)
    errors, board = evaluate_zoo(table, h_max_ns=10 * S, bootstrap_b=50, seed=0, planned_life=life)
    for h in horizons:
        assert abs(_get(errors, "fill_rate_bias_aj", h * S)["value"]) < 0.02, h
        assert abs(_get(errors, "fill_rate_bias", h * S)["value"]) < 0.02, h
    # the AJ bias averages the label-eligible rows only (n = 1000 at 10 s, all 2000 at 1 s)
    assert _get(errors, "fill_rate_bias_aj", 10 * S)["n"] == n // 2
    assert _get(errors, "fill_rate_bias_aj", S)["n"] == n
    iad = _get(errors, "ttf_iad", None)["value"]
    assert iad < 0.01 and board[0]["ttf_iad"] == iad


def test_planned_life_must_cover_every_shadow() -> None:
    """Open conflict 2 (OC-7): a predicted shadow without a planned-life row is an error, not a silent no-end."""
    table, life = _simulate("a2")
    with pytest.raises(QtError) as ei:
        evaluate_zoo(table, h_max_ns=HMAX, bootstrap_b=0, seed=0, planned_life=life.slice(1))
    assert ei.value.code == "QT-INPUT-002"
