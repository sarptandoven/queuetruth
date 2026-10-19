"""Exchange-local, DST-safe time-of-day buckets of the climatology / baseline strata (review F8, `exp_tod_dst.py`):
09:45 New York time must land in the same bucket on 2026-10-30 (EDT, UTC-4) and 2026-11-02 (EST, UTC-5)."""

from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pytest

from queuetruth.eval import baseline_predictions
from queuetruth.eval._tod import DAY_NS, session_clock_ns
from queuetruth.model.climatology import strata_keys

S = 1_000_000_000
ET = ZoneInfo("America/New_York")
OPEN_S, CLOSE_S = 34_200, 57_600  # 09:30-16:00 local
EPOCH = dt.date(1970, 1, 1)
EDT_DAY, EST_DAY = dt.date(2026, 10, 30), dt.date(2026, 11, 2)
HALF_HOUR = 1800 * S


def _ts(d: dt.date, hh: int, mm: int, ss: int = 0) -> int:
    return int(dt.datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=ET).timestamp()) * S


def _midnight(d: dt.date) -> int:
    return _ts(d, 0, 0)


def _tod(d: dt.date, t: int) -> tuple[float, float]:
    """`tod_sin`, `tod_cos` as the features crate computes them (SPEC 5.5.2)."""
    frac = min(max((t - _midnight(d) - OPEN_S * S) / ((CLOSE_S - OPEN_S) * S), 0.0), 1.0)
    return math.sin(2 * math.pi * frac), math.cos(2 * math.pi * frac)


def _days(d: dt.date) -> int:
    return (d - EPOCH).days


def test_session_clock_is_dst_safe() -> None:
    times = [(d, hh, mm) for d in (EDT_DAY, EST_DAY) for hh, mm in ((9, 45), (11, 5), (15, 40))]
    ts = np.array([_ts(d, hh, mm) for d, hh, mm in times], dtype=np.int64)
    day = np.array([_days(d) for d, _, _ in times])
    inst = np.ones(len(times), dtype=np.int64)
    tods = np.array([_tod(d, int(t)) for (d, _, _), t in zip(times, ts, strict=True)])
    clock, rule = session_clock_ns(ts, inst, day, tod_sin=tods[:, 0], tod_cos=tods[:, 1])
    assert rule == "tod_sin/tod_cos"
    # time since the 09:30 open: 15 min, 95 min, 370 min on both days
    assert (clock // (60 * S)).tolist() == [15, 95, 370, 15, 95, 370]
    mids = np.array([_midnight(d) for d, _, _ in times], dtype=np.int64)
    local, rule = session_clock_ns(ts, inst, day, midnight_utc_ns=(mids, np.ones(len(ts), dtype=bool)))
    assert rule == "midnight_utc_ns"
    assert (local // HALF_HOUR).tolist() == [19, 22, 31, 19, 22, 31]
    utc, rule = session_clock_ns(ts, inst, day)
    assert rule == "utc"
    assert (utc // HALF_HOUR).tolist() == [27, 30, 39, 29, 32, 41]  # the old, DST-shifted grid


def test_session_clock_fallbacks_within_an_instrument() -> None:
    # day 1: two interior rows (solvable); day 2: one interior row (instrument median length); day 3: none
    d1, d2, d3 = dt.date(2026, 11, 2), dt.date(2026, 11, 3), dt.date(2026, 11, 4)
    rows = [(d1, 10, 0), (d1, 12, 0), (d2, 13, 30), (d3, 8, 0)]
    ts = np.array([_ts(d, h, m) for d, h, m in rows], dtype=np.int64)
    tods = np.array([_tod(d, int(t)) for (d, _, _), t in zip(rows, ts, strict=True)])
    clock, rule = session_clock_ns(
        ts, np.ones(4, dtype=np.int64), np.array([_days(d) for d, _, _ in rows]), tod_sin=tods[:, 0], tod_cos=tods[:, 1]
    )
    assert (clock // (60 * S)).tolist() == [30, 150, 240, (DAY_NS // (60 * S)) - 90]  # 08:00 = 90 min before open
    assert "instrument median open offset" in rule


def _shadow_rows(prefix: str, d: dt.date, hh: int, mm: int, n: int, filled: bool) -> tuple[list[dict], list]:
    rows, tods = [], []
    for i in range(n):
        t = _ts(d, hh, mm, i % 60)
        for h in (S, 10 * S):
            rows.append(
                {
                    "model_id": "exact",
                    "shadow_id": f"{prefix}{i}",
                    "horizon_ns": h,
                    "p_fill": 0.0,
                    "truth_filled": filled,
                    "truth_ts_first_fill": t + S // 2 if filled else None,
                    "ts_live": t,
                    "session_date": _days(d),
                }
            )
            tods.append(_tod(d, t))
    return rows, tods


def _with(table: pa.Table, tods: list, imb: float = 0.0) -> pa.Table:
    n = table.num_rows
    for name, vals in (
        ("side_sign", [1.0] * n),
        ("dist_ticks", [0.0] * n),
        ("imb_l1", [imb] * n),
        ("tod_sin", [x[0] for x in tods]),
        ("tod_cos", [x[1] for x in tods]),
    ):
        table = table.append_column(name, pa.array(vals, pa.float64()))
    return table


@pytest.mark.parametrize("kind", ["climatology_aj", "mmw_bucket"])
def test_baselines_match_the_same_local_time_across_dst(predictions, kind: str) -> None:
    """Train in October: every 09:45 shadow fills, no 10:45 shadow does. A 09:45 shadow in November must get the
    09:45 rate; on the UTC clock 09:45 EST is the October 10:45 bucket."""
    r1, t1 = _shadow_rows("a", EDT_DAY, 9, 45, 60, True)
    r2, t2 = _shadow_rows("b", EDT_DAY, 10, 45, 60, False)
    train = _with(predictions(r1 + r2), t1 + t2)
    r3, t3 = _shadow_rows("x", EST_DAY, 9, 45, 3, True)
    test = _with(predictions(r3), t3)
    p = baseline_predictions(kind, train, test, [S, 10 * S])["p_fill"].to_pylist()
    assert p == [1.0] * 6
    utc = baseline_predictions(
        kind, train.drop_columns(["tod_sin", "tod_cos"]), test.drop_columns(["tod_sin", "tod_cos"]), [S, 10 * S]
    )
    assert utc["p_fill"].to_pylist() == [0.0] * 6


def test_hazard_climatology_strata_dst_safe() -> None:
    rows = [(EDT_DAY, 9, 45), (EST_DAY, 9, 45), (EST_DAY, 10, 45)]
    ts = [_ts(d, h, m) for d, h, m in rows]
    tods = [_tod(d, t) for (d, _, _), t in zip(rows, ts, strict=True)]
    t = pa.table(
        {
            "instrument_id": pa.array([1, 1, 1], pa.uint32()),
            "session_date": pa.array([_days(d) for d, _, _ in rows], pa.int32()).cast(pa.date32()),
            "side_sign": pa.array([1, 1, 1], pa.int8()),
            "dist_ticks": [0.0, 0.0, 0.0],
            "ts_landmark": pa.array(ts, pa.int64()).cast(pa.timestamp("ns", tz="UTC")),
            "tod_sin": [x[0] for x in tods],
            "tod_cos": [x[1] for x in tods],
        }
    )
    fine, coarse = strata_keys(t)
    assert fine[0] == fine[1] != fine[2] and len(set(coarse.tolist())) == 1
    old, _ = strata_keys(t.drop_columns(["tod_sin", "tod_cos"]))
    assert old[0] != old[1]  # the UTC clock (no tod features) still shifts; documented fallback
