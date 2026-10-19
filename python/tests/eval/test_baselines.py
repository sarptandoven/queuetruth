"""Python baselines (SPEC 5.6) on constructed train/test tables."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest

from queuetruth.errors import QtError
from queuetruth.eval import baseline_predictions, evaluate_zoo, join_features
from queuetruth.schemas import SCHEMAS, features_schema

S = 1_000_000_000
DAY = 86_400 * S
T0 = 20_460 * DAY + 36_000 * S  # 10:00 UTC
H = [S, 10 * S]


def _with_features(table: pa.Table, feats: dict[str, list[float | None]]) -> pa.Table:
    for name, vals in feats.items():
        table = table.append_column(name, pa.array(vals, pa.float64()))
    return table


def test_back_of_queue_rate(predictions, planned) -> None:
    rows = []
    for sid, ts_fill in (("a", T0 + 3 * S), ("b", None)):
        for h in H:
            rows.append(
                {
                    "model_id": "exact",
                    "shadow_id": sid,
                    "horizon_ns": h,
                    "p_fill": 0.0,
                    "qty": 10,
                    "truth_filled": ts_fill is not None and h >= 3 * S,
                    "truth_ts_first_fill": ts_fill,
                    "ts_live": T0,
                }
            )
    test = _with_features(predictions(rows), {"trade_vol_px_10s": [100.0] * 2 + [0.0] * 2, "level_qty": [40.0] * 4})
    out = baseline_predictions("back_of_queue_rate", test, test, H)
    assert out.schema.equals(SCHEMAS["qt.zoo_predictions"], check_metadata=True)
    assert out["model_id"].to_pylist() == ["back_of_queue_rate"] * 4
    # rate 10/s, need 40 + 10 = 50 -> fills after 5 s
    assert out["p_fill"].to_pylist() == [0.0, 1.0, 0.0, 0.0]
    assert out["e_fill_qty"].to_pylist() == [0.0, 10.0, 0.0, 0.0]
    mts = out["model_ts_first_fill"].cast(pa.int64()).to_pylist()
    assert mts == [T0 + 5 * S + 1, T0 + 5 * S + 1, None, None]
    assert out["truth_filled"].to_pylist() == [False, True, False, False]
    _errors, board = evaluate_zoo(out, h_max_ns=10 * S, bootstrap_b=0, planned_life=planned(out))
    assert board[0]["mcnemar"]["b"] == 0 and board[0]["mcnemar"]["c"] == 0


def _train_rows(n: int, fill_frac: float, side: float, dist: float, imb: float, ts: int, prefix: str):
    rows, feats = [], {"side_sign": [], "dist_ticks": [], "imb_l1": []}
    for i in range(n):
        filled = i < round(n * fill_frac)
        for h in H:
            rows.append(
                {
                    "model_id": "exact",
                    "shadow_id": f"{prefix}{i}",
                    "horizon_ns": h,
                    "p_fill": 0.0,
                    "truth_filled": filled and h >= 10 * S if i % 2 else filled,
                    "truth_ts_first_fill": (ts + (S // 2 if i % 2 == 0 else 2 * S)) if filled else None,
                    "ts_live": ts,
                }
            )
            feats["side_sign"].append(side)
            feats["dist_ticks"].append(dist)
            feats["imb_l1"].append(imb)
    return rows, feats


def test_climatology_aj_strata_and_fallback(predictions) -> None:
    rows, feats = _train_rows(60, 0.5, 1.0, 0.0, 0.0, T0, "t")  # one fine stratum with 60 orders
    rows2, feats2 = _train_rows(20, 1.0, 1.0, 1.0, 0.0, T0, "u")  # dist bucket 1: only 20 orders
    train = _with_features(predictions(rows + rows2), {k: feats[k] + feats2[k] for k in feats})
    test_rows = [
        {"model_id": "exact", "shadow_id": sid, "horizon_ns": h, "p_fill": 0.0, "ts_live": T0, "truth_filled": False}
        for sid in ("x", "y")
        for h in H
    ]
    test = _with_features(
        predictions(test_rows), {"side_sign": [1.0] * 4, "dist_ticks": [0.0, 0.0, 1.0, 1.0], "imb_l1": [0.0] * 4}
    )
    out = baseline_predictions("climatology_aj", train, test, H)
    p = out["p_fill"].to_pylist()
    # fine stratum: 30 of 60 filled, 15 by 0.5 s and 15 at 2 s -> CIF(1 s) = 0.25, CIF(10 s) = 0.5
    assert p[:2] == pytest.approx([0.25, 0.5])
    # dist 1 has 20 < 50 orders -> instrument x side over all 80: (15 + 10) / 80 by 1 s, (30 + 20) / 80 by 10 s
    assert p[2:] == pytest.approx([25 / 80, 50 / 80])
    assert out["e_fill_qty"].to_pylist() == pytest.approx(p)
    assert out["model_ts_first_fill"].null_count == 4


def test_mmw_bucket(predictions) -> None:
    rows, feats = [], {"imb_l1": []}
    for i in range(50):
        imb = -0.9 + i * 0.036
        filled = imb > 0.3
        for h in H:
            rows.append(
                {
                    "model_id": "exact",
                    "shadow_id": f"t{i}",
                    "horizon_ns": h,
                    "p_fill": 0.0,
                    "ts_live": T0,
                    "truth_filled": filled,
                }
            )
            feats["imb_l1"].append(imb)
    train = _with_features(predictions(rows), feats)
    test_rows = [
        {"model_id": "exact", "shadow_id": sid, "horizon_ns": h, "p_fill": 0.0, "ts_live": ts}
        for sid, ts in (("hi", T0), ("lo", T0), ("none", T0), ("other_tod", T0 + 3600 * S))
        for h in H
    ]
    test = _with_features(predictions(test_rows), {"imb_l1": [0.85] * 2 + [-0.85] * 2 + [None] * 2 + [0.85] * 2})
    out = baseline_predictions("mmw_bucket", train, test, H)
    p = out["p_fill"].to_pylist()
    imbs = np.array(feats["imb_l1"][::2])
    top_cut = np.quantile(imbs, 0.8)
    top = imbs >= top_cut
    assert p[0:2] == pytest.approx([float(np.mean(imbs[top] > 0.3))] * 2)
    assert p[2:4] == [0.0, 0.0]
    overall = float(np.mean(imbs > 0.3))
    assert p[4:6] == pytest.approx([overall] * 2)  # null imbalance -> time-of-day bucket
    assert p[6:8] == pytest.approx([overall] * 2)  # empty time-of-day bucket -> all train shadows
    assert out["truth_filled"].null_count == 8


def test_join_features_and_errors(predictions) -> None:
    preds = predictions(
        [{"model_id": m, "shadow_id": s, "horizon_ns": S, "p_fill": 0.1} for m in ("m1", "m2") for s in ("a", "b")]
    )
    names = ["imb_l1", "level_qty"]
    fs = features_schema(names)
    feats = pa.Table.from_pylist(
        [
            {
                "order_ref": "b",
                "population": "shadows",
                "instrument_id": 1,
                "session_date": 20_460,
                "ts_landmark": T0,
                "landmark_ns": 0,
                "side_sign": -1,
                "imb_l1": 0.2,
                "level_qty": 7.0,
                "event_ns": 5,
                "cause": "fill",
            },
            {
                "order_ref": "b",
                "population": "shadows",
                "instrument_id": 1,
                "session_date": 20_460,
                "ts_landmark": T0,
                "landmark_ns": S,
                "side_sign": -1,
                "imb_l1": 0.9,
                "level_qty": 1.0,
                "event_ns": 5,
                "cause": "fill",
            },
        ],
        schema=fs,
    )
    joined = join_features(preds, feats)
    assert joined.num_rows == 2 and joined["model_id"].to_pylist() == ["m1", "m1"]
    assert joined["imb_l1"].to_pylist() == [None, 0.2] and joined["side_sign"].to_pylist() == [None, -1]
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        baseline_predictions("nope", joined, joined, [S])
    with pytest.raises(QtError, match="QT-INPUT-002"):
        baseline_predictions("back_of_queue_rate", joined, joined, [S])
