"""Strata cut points of SPEC 5.6 on hand-computed examples."""

from __future__ import annotations

from queuetruth.eval import assign_strata


def test_queue_deciles_and_liquidity_terciles_hand_computed(predictions) -> None:
    rows = []
    for i in range(11):  # queue ahead 0, 10, ..., 100 -> quantiles 10, 20, ..., 90 exactly
        for h in (1, 2):  # repeated rows per horizon must not change the cut points
            rows.append(
                {
                    "shadow_id": f"s{i}",
                    "horizon_ns": h,
                    "p_fill": 0.5,
                    "queue_ahead_at_live": 10 * i,
                    "trade_vol_60s": 30 * i,
                }
            )
    rows.append({"shadow_id": "nq", "horizon_ns": 1, "p_fill": 0.5, "queue_ahead_at_live": None, "trade_vol_60s": 0})
    t = assign_strata(predictions(rows))
    by_sid = dict(zip(t["shadow_id"].to_pylist(), t["queue_decile"].to_pylist(), strict=True))
    # side="right": a value equal to a cut point goes to the upper bucket; 100 is clipped to 9
    assert [by_sid[f"s{i}"] for i in range(11)] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9]
    assert by_sid["nq"] is None
    liq = dict(zip(t["shadow_id"].to_pylist(), t["liquidity_bucket"].to_pylist(), strict=True))
    # trade_vol over the 12 shadows {0 (nq), 0, 30, ..., 300}: terciles 80 and 190 (linear interpolation)
    assert [liq[f"s{i}"] for i in range(11)] == [0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2]
    assert liq["nq"] == 0


def test_strata_are_per_instrument_day(predictions) -> None:
    rows = [
        {"shadow_id": f"a{i}", "horizon_ns": 1, "p_fill": 0.0, "instrument_id": 1, "queue_ahead_at_live": i}
        for i in range(11)
    ]
    rows += [
        {"shadow_id": f"b{i}", "horizon_ns": 1, "p_fill": 0.0, "instrument_id": 2, "queue_ahead_at_live": 1000 * i}
        for i in range(11)
    ]
    t = assign_strata(predictions(rows))
    dec = t["queue_decile"].to_pylist()
    assert dec[:11] == dec[11:] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9]
    again = assign_strata(t)  # idempotent: existing strata columns are replaced
    assert again.column_names == t.column_names and again.equals(t)
