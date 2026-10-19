"""evaluate_zoo on hand-computed tiny prediction tables (SPEC 5.6)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from queuetruth import stats
from queuetruth.eval import evaluate_zoo
from queuetruth.schemas import SCHEMAS
from queuetruth.schemas.models import LeaderboardEntry

REPO = Path(__file__).resolve().parents[3]
S = 1_000_000_000
H1, HMAX = 1 * S, 10 * S
T0 = 20_460 * 86_400 * S + 36_000 * S


def _metric(errors: pa.Table, model: str, metric: str, h: int | None, kind: str = "all", stratum: str = "all"):
    rows = errors.to_pylist()
    hit = [
        r
        for r in rows
        if (r["model_id"], r["metric"], r["horizon_ns"], r["stratum_kind"], r["stratum"])
        == (model, metric, h, kind, stratum)
    ]
    assert len(hit) == 1, (model, metric, h, kind, stratum, len(hit))
    return hit[0]


def _rows():
    # four shadows; truth: A fills at 0.5 s, B at 5 s, C never, D censored at 3 s (unfilled)
    truth = {
        "A": (T0 + S // 2, {H1: True, HMAX: True}, {H1: False, HMAX: False}),
        "B": (T0 + 5 * S, {H1: False, HMAX: True}, {H1: False, HMAX: False}),
        "C": (None, {H1: False, HMAX: False}, {H1: False, HMAX: False}),
        "D": (None, {H1: False, HMAX: False}, {H1: False, HMAX: True}),
    }
    model_p = {  # (p at 1 s, p at 10 s, model first fill)
        "front": {"A": (1, 1, T0 + S // 4), "B": (1, 1, T0 + S // 2), "C": (0, 1, T0 + 8 * S), "D": (0, 1, T0 + 2 * S)},
        "soft": {"A": (0.6, 0.9, None), "B": (0.2, 0.7, None), "C": (0.1, 0.4, None), "D": (0.3, 0.6, None)},
    }
    rows = []
    for m, preds in model_p.items():
        for sid, (p1, p10, mts) in preds.items():
            ts, filled, cens = truth[sid]
            for h, p in ((H1, p1), (HMAX, p10)):
                rows.append(
                    {
                        "model_id": m,
                        "shadow_id": sid,
                        "horizon_ns": h,
                        "p_fill": float(p),
                        "model_ts_first_fill": mts,
                        "truth_filled": filled[h],
                        "truth_ts_first_fill": ts,
                        "censored": cens[h],
                        "ts_live": T0,
                        "queue_ahead_at_live": {"A": 1, "B": 5, "C": 9, "D": 3}[sid],
                    }
                )
    return rows


def test_hand_computed_metrics(predictions, planned) -> None:
    t = predictions(_rows())
    errors, board = evaluate_zoo(t, h_max_ns=HMAX, bootstrap_b=200, seed=1, planned_life=planned(t))
    assert errors.schema.equals(SCHEMAS["qt.zoo_errors"], check_metadata=True)
    # model "soft" at 1 s: all four rows usable (D censored only at 10 s)
    p1 = np.array([0.6, 0.2, 0.1, 0.3])
    y1 = np.array([1, 0, 0, 0])
    assert _metric(errors, "soft", "brier", H1)["value"] == pytest.approx(np.mean((p1 - y1) ** 2))
    assert _metric(errors, "soft", "logloss", H1)["value"] == pytest.approx(
        -np.mean(y1 * np.log(p1) + (1 - y1) * np.log(1 - p1))
    )
    assert _metric(errors, "soft", "fill_rate_bias", H1)["value"] == pytest.approx(p1.mean() - y1.mean())
    # at 10 s D is censored and excluded
    p10 = np.array([0.9, 0.7, 0.4])
    y10 = np.array([1, 1, 0])
    rec = _metric(errors, "soft", "brier", HMAX)
    assert rec["value"] == pytest.approx(np.mean((p10 - y10) ** 2)) and rec["n"] == 3
    # McNemar at h_max (model filled = p >= 0.5): soft fills A, B; truth A, B -> b = c = 0
    assert _metric(errors, "soft", "mcnemar_b", HMAX)["value"] == 0.0
    # front fills A, B, C at 10 s; truth A, B -> c = 1 (C), b = 0; overfill = 1 / 3
    assert _metric(errors, "front", "overfill_bias", HMAX)["value"] == pytest.approx(1 / 3)
    assert _metric(errors, "front", "mcnemar_c", HMAX)["value"] == 1.0
    assert _metric(errors, "front", "mcnemar_p_exact", HMAX)["value"] == stats.mcnemar(0, 1).p_exact
    # qty bias at 1 s for front: e = p * qty -> (1 + 1 + 0 + 0 - 1) / 4
    assert _metric(errors, "front", "qty_bias", H1)["value"] == pytest.approx(0.25)
    # ttf ratio: jointly filled A (0.25 s vs 0.5 s) and B (0.5 s vs 5 s)
    expected = float(np.median([math.log(0.25 / 0.5), math.log(0.5 / 5)]))
    assert _metric(errors, "front", "ttf_log_ratio_median", HMAX)["value"] == pytest.approx(expected)
    assert board[1]["ttf_log_ratio_median"] is None  # soft has no first-fill times
    # IAD: truth KM over shadows A (event 0.5 s), B (event 5 s), C (censored 10 s), D (censored 1 s):
    # S(1 s) = 3/4, S(10 s) = 3/4 * (1 - 1/2) = 3/8 -> 1 - S = 1/4, 5/8
    iad_front = (abs(0.5 - 0.25) * 1 + abs(1.0 - 0.625) * 9) / 10
    assert _metric(errors, "front", "ttf_iad", None)["value"] == pytest.approx(iad_front)
    assert board[0]["ttf_iad"] == pytest.approx(iad_front)
    # strata: queue deciles of {1, 5, 9, 3}
    assert {r["stratum"] for r in errors.to_pylist() if r["stratum_kind"] == "queue_decile"} <= {
        str(i) for i in range(10)
    }
    assert (
        _metric(errors, "front", "brier", H1, "instrument", "1")["value"]
        == _metric(errors, "front", "brier", H1)["value"]
    )
    for entry in board:
        LeaderboardEntry.model_validate(entry)
    assert [e["model_id"] for e in board] == ["front", "soft"]
    assert board[0]["mcnemar"] == {"b": 0, "c": 1, "p_exact": 1.0}
    assert set(board[0]["brier"]) == {str(H1), str(HMAX)}


def test_cis_deterministic_and_seeded(predictions, planned) -> None:
    rows = []
    rng = np.random.default_rng(0)
    for i in range(120):
        p = float(rng.random())
        y = bool(rng.random() < p)
        rows.append(
            {
                "model_id": "m",
                "shadow_id": f"s{i}",
                "horizon_ns": HMAX,
                "p_fill": p,
                "truth_filled": y,
                "session_date": 20_460 + i % 6,
                "ts_live": T0 + (i % 6) * 86_400 * S,
            }
        )
    t = predictions(rows)
    a = evaluate_zoo(t, h_max_ns=HMAX, bootstrap_b=300, seed=5, planned_life=planned(t))
    b = evaluate_zoo(t, h_max_ns=HMAX, bootstrap_b=300, seed=5, planned_life=planned(t))
    c = evaluate_zoo(t, h_max_ns=HMAX, bootstrap_b=300, seed=6, planned_life=planned(t))
    assert a[0].equals(b[0]) and a[1] == b[1]
    assert not a[0].equals(c[0])
    rec = _metric(a[0], "m", "fill_rate_bias", HMAX)
    assert rec["ci_lo"] < rec["value"] < rec["ci_hi"]
    # the CI equals the generic day-block bootstrap of the same statistic with the same draws
    p = np.array([r["p_fill"] for r in rows])
    y = np.array([r["truth_filled"] for r in rows], float)
    blocks = np.array([r["session_date"] for r in rows])
    seed = stats.bootstrap_seed(5, "zoo_eval")
    ci = stats.day_block_bootstrap(blocks, lambda i: float(np.mean(p[i] - y[i])), 300, seed)
    assert (rec["ci_lo"], rec["ci_hi"]) == pytest.approx((ci.lo, ci.hi), rel=1e-12)


def test_scoring_reference_zoo_metrics(predictions, planned) -> None:
    ref = json.loads((REPO / "tests" / "fixtures" / "reference" / "scoring_rules.json").read_text())
    zm = ref["zoo_metrics"]
    rows = [
        {
            "shadow_id": f"s{i}",
            "horizon_ns": HMAX,
            "p_fill": float(r["p"]),
            "truth_filled": bool(r["y"]),
            "censored": bool(r["censored"]),
        }
        for i, r in enumerate(zm["input"])
    ]
    errors, board = evaluate_zoo(
        predictions(rows), h_max_ns=HMAX, bootstrap_b=0, planned_life=planned(predictions(rows))
    )
    assert _metric(errors, "m", "brier", HMAX)["value"] == pytest.approx(zm["brier"], rel=1e-12)
    assert _metric(errors, "m", "brier", HMAX)["n"] == zm["n_scored"]
    assert _metric(errors, "m", "logloss", HMAX)["value"] == pytest.approx(zm["logloss"], rel=1e-12)
    assert _metric(errors, "m", "fill_rate_bias", HMAX)["value"] == pytest.approx(zm["fill_rate_bias"], rel=1e-12)
    assert _metric(errors, "m", "overfill_bias", HMAX)["value"] == pytest.approx(zm["overfill_bias"], rel=1e-12)
    assert board[0]["mcnemar"] == {"b": zm["b_truth_only"], "c": zm["c_model_only"], "p_exact": zm["mcnemar_p_exact"]}
    assert board[0]["fill_rate_bias"][str(HMAX)]["ci_lo"] is None  # B = 0 -> no interval


def test_qtrack_mae_time_weighted(predictions, planned) -> None:
    rows = [
        {
            "model_id": "m",
            "shadow_id": "A",
            "horizon_ns": HMAX,
            "p_fill": 1.0,
            "truth_filled": True,
            "level_qty_at_live": 9,
            "qty": 1,
        }
    ]
    traj = pa.Table.from_pylist(
        [
            {"model_id": "m", "shadow_id": "A", "ts_event": T0, "queue_ahead_est": 9.0, "queue_ahead_exact": 9},
            {"model_id": "m", "shadow_id": "A", "ts_event": T0 + 3 * S, "queue_ahead_est": 5.0, "queue_ahead_exact": 7},
            {"model_id": "m", "shadow_id": "A", "ts_event": T0 + 4 * S, "queue_ahead_est": 0.0, "queue_ahead_exact": 4},
        ],
        schema=SCHEMAS["qt.zoo_trajectory"],
    )
    errors, board = evaluate_zoo(
        predictions(rows), h_max_ns=HMAX, trajectory=traj, bootstrap_b=0, planned_life=planned(predictions(rows))
    )
    # errors 0 for 3 s, 2/10 for 1 s; last row has no duration
    assert board[0]["qtrack_mae"] == pytest.approx((0 * 3 + 0.2 * 1) / 4)
    assert _metric(errors, "m", "qtrack_mae", None)["n"] == 1


def test_ttf_iad_spans_zero_to_h_max(predictions, planned) -> None:
    """SPEC 5.6 (open conflict 7): `ttf_iad` sums over the run's horizons plus `h_max`, so the terms span [0, h_max].
    Predictions without `h_max` rows have no eligible row for the `(h_max - h_last)` term: `ttf_iad` is null, not
    the 1 s term divided by h_max (which understated the gap 10-fold here)."""
    full = predictions(_rows())
    _, board = evaluate_zoo(full, h_max_ns=HMAX, bootstrap_b=20, seed=1, planned_life=planned(full))
    assert board[0]["ttf_iad"] == pytest.approx((abs(0.5 - 0.25) * 1 + abs(1.0 - 0.625) * 9) / 10)
    short = predictions([r for r in _rows() if r["horizon_ns"] == H1])
    errors, board = evaluate_zoo(short, h_max_ns=HMAX, bootstrap_b=20, seed=1, planned_life=planned(short))
    assert [b["ttf_iad"] for b in board] == [None, None]
    assert not [r for r in errors.to_pylist() if r["metric"].endswith("ttf_iad")]
    assert _metric(errors, "front", "fill_rate_bias_aj", H1)["value"] == pytest.approx(0.25)  # the 1 s term is kept
    LeaderboardEntry.model_validate(board[0])
    # h_max itself a prediction horizon (and the only one): one term spanning [0, h_max]
    only = predictions([r for r in _rows() if r["horizon_ns"] == HMAX])
    _, board = evaluate_zoo(only, h_max_ns=HMAX, bootstrap_b=20, seed=1, planned_life=planned(only))
    assert board[0]["ttf_iad"] is not None and board[0]["ttf_iad"] >= 0.0


def test_l2_only_run_has_no_truth_metrics(predictions, planned) -> None:
    rows = [
        {
            "shadow_id": f"s{i}",
            "horizon_ns": HMAX,
            "p_fill": 0.5,
            "truth_filled": None,
            "queue_ahead_at_live": None,
            "level_qty_at_live": None,
        }
        for i in range(5)
    ]
    errors, board = evaluate_zoo(
        predictions(rows), h_max_ns=HMAX, bootstrap_b=10, planned_life=planned(predictions(rows))
    )
    assert errors.num_rows == 0
    assert board[0]["brier"] == {str(HMAX): None} and board[0]["ttf_iad"] is None
    LeaderboardEntry.model_validate(board[0])
    empty, lb = evaluate_zoo(predictions([]), h_max_ns=HMAX, planned_life=planned(predictions([])))
    assert empty.num_rows == 0 and lb == []
