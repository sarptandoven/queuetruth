"""`queuetruth.eval.impact`: square-root-law impact, queue share, volume share, population, flags, summary, warning
lines and the fail-soft hooks of `zoo` and `score`."""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from queuetruth.errors import QtError
from queuetruth.eval import impact as im
from queuetruth.schemas import SCHEMAS
from queuetruth.schemas.models import DOC_MODELS, parse_document

S = 1_000_000_000
DAY = 20_460
DAY_NS = 86_400 * S
MID = DAY * DAY_NS
OPEN = MID + 34_200 * S
CLOSE = MID + 57_600 * S
HALF = (CLOSE - OPEN) // 2
TS = pa.timestamp("ns", tz="UTC")
REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "synth_small"

#: grid = open, open + 11700 s, close; two returns, so `min_returns = 2`.
CFG = im.ImpactConfig(rv_interval_ns=HALF, min_returns=2)


def _ts(vals: list[int | None]) -> pa.Array:
    return pa.array(vals, pa.int64()).cast(TS)


def trades_table(rows: list[tuple[int, int, int, int, str]], seq: list[int] | None = None) -> pa.Table:
    """(instrument_id, ts, price, size, kind) rows as a `qt.trades`-like table."""
    return pa.table(
        {
            "ts_event": _ts([r[1] for r in rows]),
            "instrument_id": pa.array([r[0] for r in rows], pa.uint32()),
            "sequence": pa.array(seq if seq is not None else list(range(len(rows))), pa.uint64()),
            "price": pa.array([r[2] for r in rows], pa.int64()),
            "size": pa.array([r[3] for r in rows], pa.uint64()),
            "aggressor_side": pa.array([ord("B")] * len(rows), pa.uint8()),
            "kind": pa.array([r[4] for r in rows], pa.utf8()).dictionary_encode(),
        }
    )


def instruments_table(tick: int = 100, days: tuple[int, ...] = (DAY,), venue: str | None = None) -> pa.Table:
    cols: dict[str, Any] = {
        "instrument_id": pa.array([1] * len(days), pa.uint32()),
        "session_date": pa.array(list(days), pa.int32()).cast(pa.date32()),
        "midnight_utc_ns": pa.array([d * DAY_NS for d in days], pa.int64()),
        "open_s": pa.array([34_200] * len(days), pa.uint32()),
        "close_s": pa.array([57_600] * len(days), pa.uint32()),
        "tick_size": pa.array([tick] * len(days), pa.int64()),
    }
    if venue is not None:
        cols["venue"] = pa.array([venue] * len(days), pa.utf8())
        cols["lot_size"] = pa.array([1] * len(days), pa.uint64())
        cols["size_exp"] = pa.array([0] * len(days), pa.int8())
    return pa.table(cols)


_RES_DEFAULTS: dict[str, Any] = {
    "instrument_id": 1,
    "side": ord("B"),
    "price": 10_000,
    "qty": 1,
    "filled_qty": 0,
    "ts_live": OPEN + 50 * S,
    "ts_full_fill": None,
    "ts_cancel_effective": None,
    "ts_planned_end": None,
    "level_qty_at_live": 100,
    "end_state": "cancelled",
    "out_of_session": False,
}


def results_table(rows: list[dict[str, Any]]) -> pa.Table:
    full = [{**_RES_DEFAULTS, **r} for r in rows]
    return pa.table(
        {
            "shadow_id": pa.array([r["shadow_id"] for r in full], pa.utf8()),
            "instrument_id": pa.array([r["instrument_id"] for r in full], pa.uint32()),
            "side": pa.array([r["side"] for r in full], pa.uint8()),
            "price": pa.array([r["price"] for r in full], pa.int64()),
            "qty": pa.array([r["qty"] for r in full], pa.uint64()),
            "ts_live": _ts([r["ts_live"] for r in full]),
            "ts_cancel_effective": _ts([r["ts_cancel_effective"] for r in full]),
            "level_qty_at_live": pa.array([r["level_qty_at_live"] for r in full], pa.uint64()),
            "ts_full_fill": _ts([r["ts_full_fill"] for r in full]),
            "filled_qty": pa.array([r["filled_qty"] for r in full], pa.uint64()),
            "end_state": pa.array([r["end_state"] for r in full], pa.utf8()).dictionary_encode(),
            "out_of_session": pa.array([r["out_of_session"] for r in full], pa.bool_()),
            "ts_planned_end": _ts([r["ts_planned_end"] for r in full]),
        }
    )


# Instrument 1: A at the open (10000), B at +100 s (11000), a cross inside the session, C after the mid grid point
# (9900), one trade before the open; instrument 2 has no instrument row.
TRADES = trades_table(
    [
        (1, OPEN - 10 * S, 10_000, 1_000, "displayed"),
        (1, OPEN, 10_000, 100, "displayed"),
        (1, OPEN + 100 * S, 11_000, 200, "hidden"),
        (1, OPEN + 200 * S, 50_000, 500, "cross"),
        (1, OPEN + HALF + 5 * S, 9_900, 300, "displayed"),
        (2, MID + 3_600 * S, 2_000, 40, "displayed"),
        (2, MID + 7_200 * S, 2_000, 60, "displayed"),
    ]
)
SIGMA = math.sqrt(math.log(1.1) ** 2 + math.log(0.9) ** 2)
V = 100 + 200 + 500 + 300  # session volume of instrument 1, the cross included


def by_id(table: pa.Table) -> dict[str, dict[str, Any]]:
    return {r["shadow_id"]: r for r in table.to_pylist()}


def daily_table(volume: int | None, sigma: float | None = None, iid: int = 1, day: int = DAY) -> pa.Table:
    return pa.table(
        {
            "instrument_id": pa.array([iid], pa.uint32()),
            "session_date": pa.array([day], pa.int32()).cast(pa.date32()),
            "daily_volume": pa.array([volume], pa.uint64()),
            "sigma_daily": pa.array([sigma], pa.float64()),
        }
    )


# ---- formulas -----------------------------------------------------------------------------------------------------


def test_impact_frac_square_root_and_linear_continuation() -> None:
    cfg = im.ImpactConfig(coefficient=0.7, linear_below=1e-4)
    q = np.array([1e-2, 1e-4, 5e-5, 0.0])
    got = im.impact_frac(q, np.full(4, 0.02), cfg)
    assert got[0] == pytest.approx(0.7 * 0.02 * 0.1)
    assert got[1] == pytest.approx(0.7 * 0.02 * 0.01)  # at the crossover both branches agree
    assert got[2] == pytest.approx(0.7 * 0.02 * 0.01 * 0.5)  # linear below: half the volume, half the impact
    assert got[3] == 0.0
    just_below = im.impact_frac(np.array([1e-4 * (1 - 1e-9)]), np.array([0.02]), cfg)[0]
    assert just_below == pytest.approx(got[1], rel=1e-6)  # continuous
    pure = im.impact_frac(np.array([5e-5]), np.array([0.02]), im.ImpactConfig(coefficient=0.7, linear_below=0.0))
    assert pure[0] == pytest.approx(0.7 * 0.02 * math.sqrt(5e-5))


def test_realized_sigma_previous_tick_grid() -> None:
    ts = np.array([OPEN, OPEN + 100 * S, OPEN + HALF + 5 * S], dtype=np.int64)
    px = np.array([10_000, 11_000, 9_900], dtype=np.int64)
    sig, n = im.realized_sigma(ts, px, OPEN, CLOSE, HALF)
    assert n == 2 and sig == pytest.approx(SIGMA)
    # trades outside the window are ignored; the open point takes the first trade's price
    ts2 = np.array([OPEN - S, OPEN + HALF - 1, OPEN + HALF + 2, CLOSE + 1], dtype=np.int64)
    px2 = np.array([1, 10_000, 10_100, 1], dtype=np.int64)
    sig2, n2 = im.realized_sigma(ts2, px2, OPEN, CLOSE, HALF)
    assert n2 == 2 and sig2 == pytest.approx(abs(math.log(10_100 / 10_000)))
    assert im.realized_sigma(ts, px, CLOSE, OPEN, HALF) == (0.0, 0)
    assert im.realized_sigma(ts[:0], px[:0], OPEN, CLOSE, HALF) == (0.0, 0)


def test_realized_sigma_counts_the_opening_interval() -> None:
    # the only move of the day is between the open and the first grid point: it must count
    ts = np.array([OPEN + S, OPEN + 2 * S], dtype=np.int64)
    px = np.array([10_000, 11_000], dtype=np.int64)
    sig, n = im.realized_sigma(ts, px, OPEN, CLOSE, HALF)
    assert n == 2 and sig == pytest.approx(math.log(1.1))  # seeded with the first trade (10000)
    seeded, n2 = im.realized_sigma(ts, px, OPEN, CLOSE, HALF, seed_price=9_000)
    assert n2 == 2 and seeded == pytest.approx(math.log(11_000 / 9_000))  # the opening cross price
    # grid intervals that end before the first trade are not counted as returns
    late = np.array([OPEN + HALF + S], dtype=np.int64)
    sig3, n3 = im.realized_sigma(late, np.array([5_000]), OPEN, CLOSE, HALF)
    assert sig3 == 0.0 and n3 == 1


def test_realized_sigma_non_positive_prices_are_not_evaluable() -> None:
    ts = np.array([OPEN, OPEN + 100 * S], dtype=np.int64)
    assert im.realized_sigma(ts, np.array([10_000, -50]), OPEN, CLOSE, HALF) == (None, 0)
    assert im.realized_sigma(ts, np.array([10_000, 0]), OPEN, CLOSE, HALF) == (None, 0)
    assert im.realized_sigma(ts, np.array([10_000, 10]), OPEN, CLOSE, HALF, seed_price=-1) == (None, 0)


# ---- per-shadow figures -------------------------------------------------------------------------------------------


def test_shadow_impact_figures_and_life_ends() -> None:
    res = results_table(
        [
            # Q/V = 0.01, 10% of the queue, fully filled at +150 s
            {"shadow_id": "s1", "qty": 11, "level_qty_at_live": 99, "filled_qty": 11, "ts_full_fill": OPEN + 150 * S},
            # live before the open: out of session (queue and volume figures only, never flagged)
            {
                "shadow_id": "s0",
                "price": None,
                "level_qty_at_live": None,
                "ts_live": OPEN - 20 * S,
                "ts_cancel_effective": OPEN + 1,
                "ts_planned_end": CLOSE,
            },
            # planned end before the cancel; no price -> last continuous trade (B, not the cross)
            {
                "shadow_id": "s2",
                "price": None,
                "ts_live": OPEN + 300 * S,
                "ts_cancel_effective": CLOSE + S,
                "ts_planned_end": OPEN + HALF + 5 * S,
                "level_qty_at_live": 0,
            },
            # no end information: the session close
            {"shadow_id": "s3", "ts_live": OPEN + 50 * S},
            # instrument without an instrument row: UTC day, no tick size
            {"shadow_id": "s4", "instrument_id": 2, "ts_live": MID + 1, "ts_planned_end": MID + 3_600 * S},
        ]
    )
    t = im.shadow_impact(res, TRADES, instruments_table(), config=CFG)
    assert t.schema.equals(SCHEMAS["qt.shadow_impact"], check_metadata=False)
    meta = {k.decode(): v.decode() for k, v in t.schema.metadata.items()}
    assert meta["qt.schema"] == "qt.shadow_impact" and meta[im.META_VOLUME_SOURCE] == "venue_session"
    assert meta[im.META_SOURCE] == "replay" and im.META_H_MAX not in meta
    assert t["shadow_id"].to_pylist() == ["s0", "s1", "s2", "s3", "s4"]  # sorted
    r = by_id(t)
    s1 = r["s1"]
    assert s1["status"] == "live" and s1["volume_source"] == "venue_session" and s1["queue_min_qty"] == 1
    assert s1["daily_volume"] == V and s1["sigma_daily"] == pytest.approx(SIGMA)
    assert s1["daily_participation"] == pytest.approx(11 / V)
    frac = SIGMA * math.sqrt(11 / V)
    assert s1["impact_frac"] == pytest.approx(frac) and s1["impact_bps"] == pytest.approx(1e4 * frac)
    assert s1["impact_ticks"] == pytest.approx(frac * 10_000 / 100) and s1["impact_ticks"] > 1
    assert s1["ref_price_source"] == "order" and s1["tick_size"] == 100
    assert s1["queue_share_at_live"] == pytest.approx(0.1) and s1["queue_share_max"] == pytest.approx(0.1)
    assert s1["end_source"] == "full_fill" and s1["life_volume"] == 200  # B only (cross excluded)
    assert s1["volume_share"] == pytest.approx(11 / 211) and not s1["fills_in_tape"]
    # venue volume only: the impact estimate is an upper bound and not flagged
    assert s1["flag_queue"] and s1["flag_volume"] and not s1["flag_impact"] and s1["material"]

    s0 = r["s0"]
    assert s0["status"] == "out_of_session" and s0["end_source"] == "cancel"
    assert s0["life_volume"] == 1_100 and s0["volume_share"] == 0.0  # pre-open trade + A
    assert s0["ref_price"] is None and s0["ref_price_source"] == "none"
    assert s0["impact_frac"] is None and s0["daily_participation"] is None and s0["daily_volume"] == V
    assert not (s0["flag_impact"] or s0["flag_queue"] or s0["flag_volume"] or s0["material"])

    s2 = r["s2"]
    assert s2["end_source"] == "planned_end" and s2["ref_price"] == 11_000 and s2["ref_price_source"] == "last_trade"
    assert s2["queue_share_at_live"] == 1.0 and s2["flag_queue"]  # alone at its price
    assert s2["life_volume"] == 300 and s2["volume_share"] == 0.0

    s3 = r["s3"]
    assert s3["end_source"] == "session_close" and s3["ts_end"].value == CLOSE
    assert s3["life_volume"] == 200 + 300

    s4 = r["s4"]
    assert s4["session_date"].toordinal() - 719_163 == DAY and s4["status"] == "live"  # UTC day: always in session
    assert s4["tick_size"] is None and s4["impact_ticks"] is None and not s4["flag_impact"]
    # whole UTC day on the 3.25 h grid: a flat price gives zero variance, so zero impact
    assert s4["daily_volume"] == 100 and s4["sigma_daily"] == 0.0 and s4["impact_frac"] == 0.0
    assert s4["life_volume"] == 40


def test_life_interval_excludes_ts_live_and_includes_ts_end() -> None:
    trades = trades_table(
        [
            (1, OPEN + 10 * S, 100, 7, "displayed"),
            (1, OPEN + 20 * S, 100, 11, "displayed"),
            (1, OPEN + 30 * S, 100, 13, "displayed"),
        ]
    )
    res = results_table([{"shadow_id": "a", "ts_live": OPEN + 10 * S, "ts_cancel_effective": OPEN + 20 * S}])
    assert by_id(im.shadow_impact(res, trades, instruments_table(), config=CFG))["a"]["life_volume"] == 11


def test_multi_day_sessions_and_utc_fallback() -> None:
    d2 = DAY + 1
    trades = trades_table(
        [
            (1, OPEN + 10 * S, 10_000, 100, "displayed"),
            (1, OPEN + 20 * S, 10_000, 200, "displayed"),
            (1, d2 * DAY_NS + 34_200 * S + 10 * S, 10_000, 4_000, "displayed"),
            (1, (DAY - 5) * DAY_NS + 40_000 * S, 10_000, 9, "displayed"),
        ]
    )
    res = results_table(
        [
            {"shadow_id": "d1", "ts_live": OPEN + 5 * S},  # no end information
            {"shadow_id": "d2", "ts_live": d2 * DAY_NS + 34_200 * S + S},
            {"shadow_id": "early", "ts_live": (DAY - 5) * DAY_NS + 39_000 * S},  # no instrument row that day
        ]
    )
    r = by_id(im.shadow_impact(res, trades, instruments_table(days=(DAY, d2)), config=CFG))
    assert r["d1"]["ts_end"].value == CLOSE and r["d1"]["life_volume"] == 300  # capped at its own session close
    assert r["d1"]["daily_volume"] == 300
    assert r["d2"]["session_date"].toordinal() - 719_163 == d2 and r["d2"]["daily_volume"] == 4_000
    e = r["early"]
    assert e["session_date"].toordinal() - 719_163 == DAY - 5 and e["tick_size"] is None
    assert e["daily_volume"] == 9 and e["ts_end"].value == (DAY - 4) * DAY_NS and e["life_volume"] == 9


def test_flags_are_inclusive_at_thresholds_and_follow_config() -> None:
    res = results_table([{"shadow_id": "a", "qty": 1, "level_qty_at_live": 9}])  # share exactly 0.1
    t = im.shadow_impact(res, TRADES, instruments_table(), config=CFG)
    assert by_id(t)["a"]["flag_queue"]
    t2 = im.shadow_impact(res, TRADES, instruments_table(), config={**CFG.model_dump(), "queue_share": 0.1000001})
    assert not by_id(t2)["a"]["flag_queue"]
    daily = daily_table(V)  # the same V, but user-supplied: the impact flag applies
    ticks = by_id(t)["a"]["impact_ticks"]
    at = im.shadow_impact(
        res, TRADES, instruments_table(), daily=daily, config={**CFG.model_dump(), "impact_ticks": ticks}
    )
    assert by_id(at)["a"]["flag_impact"] and by_id(at)["a"]["volume_source"] == "user"
    above = im.shadow_impact(
        res, TRADES, instruments_table(), daily=daily, config={**CFG.model_dump(), "impact_ticks": ticks * 1.0001}
    )
    assert not by_id(above)["a"]["flag_impact"]
    venue = im.shadow_impact(res, TRADES, instruments_table(), config={**CFG.model_dump(), "impact_ticks": ticks})
    assert not by_id(venue)["a"]["flag_impact"]  # venue volume only: not flagged by default
    opt_in = {**CFG.model_dump(), "impact_ticks": ticks, "flag_venue_volume_impact": True}
    assert by_id(im.shadow_impact(res, TRADES, instruments_table(), config=opt_in))["a"]["flag_impact"]


def test_queue_flag_needs_a_round_lot() -> None:
    res = results_table(
        [
            {"shadow_id": "odd", "qty": 1, "level_qty_at_live": 0},  # alone, but an odd lot
            {"shadow_id": "lot", "qty": 100, "level_qty_at_live": 900},  # exactly 10% with a round lot
        ]
    )
    t = im.shadow_impact(res, TRADES, instruments_table(venue="nasdaq"), config=CFG)
    r = by_id(t)
    assert r["odd"]["queue_min_qty"] == 100 and r["odd"]["queue_share_at_live"] == 1.0 and not r["odd"]["flag_queue"]
    assert r["lot"]["flag_queue"]
    s = im.summarize(t, CFG)
    assert s.n_flag_queue == 1 and s.n_queue_below_min_qty == 1 and s.n_alone_at_level == 1
    assert [x.n_flagged for x in s.sensitivity["queue_share"]] == [1, 1, 1, 0, 0]
    small = im.shadow_impact(
        res, TRADES, instruments_table(venue="nasdaq"), config={**CFG.model_dump(), "queue_min_qty": 1}
    )
    assert by_id(small)["odd"]["flag_queue"] and by_id(small)["odd"]["queue_min_qty"] == 1
    small_cfg = CFG.model_copy(update={"queue_min_qty": 1})
    assert "at live, qty >= 1: 2;" in (im.summarize(small, small_cfg).warning or "")
    other = im.shadow_impact(res, TRADES, instruments_table(venue="cme"), config=CFG)
    assert by_id(other)["odd"]["queue_min_qty"] == 1 and by_id(other)["odd"]["flag_queue"]  # lot_size elsewhere


def test_round_lot_in_size_units() -> None:
    assert im._round_lot({"venue": "generic", "source_venue": "iex", "size_exp": -2}) == 10_000
    assert im._round_lot({"venue": "nasdaq", "size_exp": 0}) == 100
    assert im._round_lot({"venue": "cme", "lot_size": 5}) == 5 and im._round_lot({"venue": "cme"}) == 1


def test_rejected_and_out_of_session_shadows_are_not_assessed() -> None:
    res = results_table(
        [
            {"shadow_id": "rej", "end_state": "rejected_would_cross", "qty": 100, "level_qty_at_live": 0},
            {"shadow_id": "oos", "out_of_session": True, "qty": 100, "level_qty_at_live": 0},
            {"shadow_id": "ok", "qty": 1, "level_qty_at_live": 10_000},
        ]
    )
    t = im.shadow_impact(res, TRADES, instruments_table(), config=CFG)
    r = by_id(t)
    assert r["rej"]["status"] == "rejected" and r["rej"]["end_source"] == "rejected"
    assert r["rej"]["queue_share_at_live"] is None and r["rej"]["life_volume"] is None
    assert r["rej"]["impact_frac"] is None and not r["rej"]["material"]
    assert r["oos"]["status"] == "out_of_session" and r["oos"]["queue_share_at_live"] == 1.0
    assert not r["oos"]["material"] and r["oos"]["impact_frac"] is None
    s = im.summarize(t, CFG)
    assert (s.n_shadows, s.n_rejected, s.n_out_of_session, s.n_assessed) == (3, 1, 1, 1)
    assert s.n_material == 0 and s.material_share == 0.0 and not s.material and s.n_alone_at_level == 0


def test_queue_share_max_is_information_only() -> None:
    res = results_table([{"shadow_id": "t", "qty": 10, "level_qty_at_live": 190, "filled_qty": 4}])
    traj = pa.table(
        {
            "shadow_id": ["t", "t", "t", "other"],
            "ts_event": _ts([OPEN + 50 * S, OPEN + 60 * S, OPEN + 120 * S, OPEN]),
            "kind": pa.array(["live", "event", "end", "end"]).dictionary_encode(),
            "level_qty_after": pa.array([190, 14, 0, 0], pa.uint64()),
            "filled_qty": pa.array([0, 4, 10, 0], pa.uint64()),
        }
    )
    r = by_id(im.shadow_impact(res, TRADES, instruments_table(), trajectory=traj, config=CFG))["t"]
    assert r["queue_share_at_live"] == pytest.approx(10 / 200)
    assert r["queue_share_max"] == pytest.approx(6 / 20)  # leaves 6 over level 14 + 6; the filled-out row skipped
    assert r["end_source"] == "trajectory_end" and r["ts_end"].value == OPEN + 120 * S
    assert r["life_volume"] == 200 and not r["flag_queue"]  # 0.3 over the life, 0.05 at live: not flagged


def test_fills_in_tape_volume_share() -> None:
    res = results_table(
        [
            {"shadow_id": "tw", "qty": 50, "filled_qty": 50, "ts_full_fill": OPEN + 150 * S},
            {"shadow_id": "nt", "qty": 50, "filled_qty": 50, "ts_full_fill": OPEN + 150 * S},
            {"shadow_id": "zero", "ts_live": OPEN + HALF + 6 * S, "ts_cancel_effective": OPEN + HALF + 7 * S},
        ]
    )
    r = by_id(im.shadow_impact(res, TRADES, instruments_table(), config=CFG, fills_in_tape={"tw", "zero"}))
    assert r["tw"]["fills_in_tape"] and r["tw"]["volume_share"] == pytest.approx(50 / 200)  # the twin's prints
    assert not r["nt"]["fills_in_tape"] and r["nt"]["volume_share"] == pytest.approx(50 / 250)
    assert r["zero"]["life_volume"] == 0 and r["zero"]["volume_share"] is None
    over = im.shadow_impact(
        results_table([{"shadow_id": "o", "qty": 500, "filled_qty": 500, "ts_full_fill": OPEN + 150 * S}]),
        TRADES,
        instruments_table(),
        config=CFG,
        fills_in_tape=True,
    )
    assert by_id(over)["o"]["volume_share"] == 1.0  # capped
    assert im.summarize(over, CFG).fills_in_tape


def test_daily_overrides_and_volume_source() -> None:
    res = results_table([{"shadow_id": "a", "qty": 11}, {"shadow_id": "b", "instrument_id": 2, "ts_live": MID + 1}])
    daily = daily_table(110_000)
    t = im.shadow_impact(res, TRADES, instruments_table(), daily=daily, config=CFG)
    r = by_id(t)
    assert r["a"]["daily_volume"] == 110_000 and r["a"]["sigma_daily"] == pytest.approx(SIGMA)
    assert r["a"]["daily_participation"] == pytest.approx(1e-4) and r["a"]["volume_source"] == "user"
    assert r["b"]["volume_source"] == "venue_session"
    assert t.schema.metadata[im.META_VOLUME_SOURCE.encode()] == b"mixed"
    only = im.shadow_impact(res.slice(0, 1), TRADES, instruments_table(), daily=daily, config=CFG)
    assert only.schema.metadata[im.META_VOLUME_SOURCE.encode()] == b"user"
    s = im.summarize(only, CFG)
    assert s.volume_source == "user" and s.days[0].volume_source == "user"
    sigma_only = daily_table(None, 0.5)
    so = im.shadow_impact(res.slice(0, 1), TRADES, instruments_table(), daily=sigma_only, config=CFG)
    assert by_id(so)["a"]["sigma_daily"] == 0.5 and by_id(so)["a"]["volume_source"] == "venue_session"


@pytest.mark.parametrize(
    ("daily", "code"),
    [
        (pa.concat_tables([daily_table(1), daily_table(1)]), "QT-INPUT-003"),
        (daily_table(1, -1.0), "QT-INPUT-002"),
        (daily_table(1).set_column(1, "session_date", pa.array(["2026-01-07"])), "QT-INPUT-002"),
        (daily_table(1).set_column(0, "instrument_id", pa.array([None], pa.uint32())), "QT-INPUT-002"),
        (daily_table(1).set_column(2, "daily_volume", pa.array([1.5])), "QT-INPUT-002"),
        (daily_table(1).drop_columns(["session_date"]), "QT-INPUT-002"),
    ],
)
def test_daily_validation(daily: pa.Table, code: str) -> None:
    with pytest.raises(QtError) as e:
        im.shadow_impact(results_table([{"shadow_id": "a"}]), TRADES, instruments_table(), daily=daily, config=CFG)
    assert e.value.code == code


def test_zero_daily_volume_leaves_impact_undefined() -> None:
    res = results_table([{"shadow_id": "a", "instrument_id": 3}])
    r = by_id(im.shadow_impact(res, TRADES, instruments_table(), config=CFG))["a"]
    assert r["daily_volume"] is None and r["impact_frac"] is None and r["life_volume"] == 0
    assert r["volume_share"] is None and r["ref_price_source"] == "order"
    assert r["sigma_daily"] is None and not r["flag_impact"] and not r["material"]


def test_too_few_returns_leaves_sigma_undefined() -> None:
    res = results_table([{"shadow_id": "a", "qty": 11}])
    cfg = CFG.model_copy(update={"min_returns": 3})  # the session grid has two returns
    r = by_id(im.shadow_impact(res, TRADES, instruments_table(), config=cfg))["a"]
    assert r["daily_volume"] == V and r["sigma_daily"] is None
    assert r["impact_frac"] is None and r["impact_ticks"] is None and not r["flag_impact"]


def test_negative_prices_leave_sigma_undefined_without_failing() -> None:
    trades = pa.concat_tables([TRADES, trades_table([(1, OPEN + 60 * S, -50, 5, "displayed")])])
    r = by_id(im.shadow_impact(results_table([{"shadow_id": "a"}]), trades, instruments_table(), config=CFG))["a"]
    assert r["sigma_daily"] is None and r["impact_frac"] is None and r["daily_volume"] == V + 5


def test_opening_cross_seeds_the_variance() -> None:
    trades = trades_table(
        [
            (1, OPEN, 9_000, 1_000, "cross"),
            (1, OPEN + S, 10_000, 10, "displayed"),
            (1, OPEN + HALF + S, 10_000, 10, "displayed"),
        ]
    )
    r = by_id(im.shadow_impact(results_table([{"shadow_id": "a"}]), trades, instruments_table(), config=CFG))["a"]
    assert r["sigma_daily"] == pytest.approx(math.log(10_000 / 9_000))


def test_uint64_sequences_above_int64_sort() -> None:
    big = 2**63 + 5
    trades = trades_table([(1, OPEN, 10_000, 10, "displayed"), (1, OPEN, 11_000, 10, "displayed")], seq=[big + 1, big])
    res = results_table([{"shadow_id": "a", "price": None, "ts_live": OPEN + S}])
    r = by_id(im.shadow_impact(res, trades, instruments_table(), config=CFG))["a"]
    assert r["ref_price"] == 10_000  # the higher sequence is the later trade


def test_input_errors() -> None:
    res = results_table([{"shadow_id": "a"}, {"shadow_id": "a"}])
    with pytest.raises(QtError) as e:
        im.shadow_impact(res, TRADES, None)
    assert e.value.code == "QT-INPUT-003"
    with pytest.raises(QtError) as e:
        im.shadow_impact(res.drop_columns(["qty"]), TRADES, None)
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        im.shadow_impact(results_table([{"shadow_id": "a"}]), TRADES.drop_columns(["size"]), None)
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        im.shadow_impact(results_table([{"shadow_id": "a"}]), TRADES, None, config={"queue_share": 0})
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        im.shadow_impact(results_table([{"shadow_id": "a"}]), TRADES, None, config={"nope": 1})
    assert e.value.code == "QT-CONFIG-001"


def test_deterministic_bytes_and_parquet_settings(tmp_path: Path) -> None:
    res = results_table([{"shadow_id": f"s{i}", "qty": i + 1} for i in range(20)][::-1])
    summaries = []
    for k in range(2):
        t = im.shadow_impact(res, TRADES, instruments_table(), config=CFG)
        summaries.append(im.write(tmp_path / str(k), t, im.summarize(t, CFG)))
    for name in ("shadow_impact.parquet", "shadow_impact_summary.json"):
        assert (tmp_path / "0" / name).read_bytes() == (tmp_path / "1" / name).read_bytes()
    md = pq.ParquetFile(tmp_path / "0" / "shadow_impact.parquet").metadata
    assert md.row_group(0).column(0).compression == "ZSTD"
    names = [md.schema.column(i).name for i in range(md.num_columns)]
    encodings = {n: md.row_group(0).column(i).encodings for i, n in enumerate(names)}
    assert "RLE_DICTIONARY" not in encodings["impact_frac"] and "PLAIN_DICTIONARY" not in encodings["impact_frac"]
    entry = summaries[0].files[0]
    assert entry.name == "shadow_impact.parquet" and entry.rows == 20
    doc = json.loads((tmp_path / "0" / "shadow_impact_summary.json").read_text())
    assert doc["files"][0]["sha256"] == entry.sha256


# ---- summary and warning ------------------------------------------------------------------------------------------


def test_summary_counts_and_warning_threshold() -> None:
    rows = [{"shadow_id": f"q{i:03d}", "qty": 1, "level_qty_at_live": 10_000} for i in range(99)]
    rows.append({"shadow_id": "big", "qty": 100, "level_qty_at_live": 0})
    cfg = CFG.model_copy(update={"volume_share": 1.0})
    t = im.shadow_impact(results_table(rows), TRADES, instruments_table(), config=cfg)
    s = im.summarize(t, cfg)
    assert s.n_shadows == 100 and s.n_assessed == 100 and s.n_flag_queue == 1 and s.n_material == 1
    assert s.n_alone_at_level == 1 and s.n_flag_impact == 0 and s.n_impact_venue_bound == 1
    assert (s.n_evaluable_impact, s.n_evaluable_queue, s.n_evaluable_volume) == (100, 100, 100)
    assert s.material_share == pytest.approx(0.01) and s.material  # exactly warn_fraction: material
    assert s.source == "replay" and s.h_max_ns is None and s.inputs == [] and s.files == []
    assert s.warning == (
        "impact: 1 of 100 shadows (1.0%) may not be independent of the book (>= 10% of the displayed queue at live, "
        "round lot or more: 1; >= 100% of traded volume over life: 0; impact >= 1 tick on venue volume only (upper "
        "bound, not flagged): 1); a fill rate over them could move by up to 1.0 pts"
    )
    assert s.warning == im.warning_line(s) and "\n" not in s.warning
    assert s.by_instrument == {"1": 1}
    assert [d.n_shadows for d in s.days] == [100] and s.days[0].session_date == "2026-01-07"
    s2 = im.summarize(t, cfg.model_copy(update={"warn_fraction": 0.0101}))
    assert not s2.material and s2.warning is None and s2.n_material == 1
    big_ticks = by_id(t)["big"]["impact_ticks"]
    small_ticks = by_id(t)["q000"]["impact_ticks"]
    expect_i = [int(big_ticks >= x) + 99 * int(small_ticks >= x) for x in im.SENSITIVITY["impact_ticks"]]
    assert [x.n_flagged for x in s.sensitivity["impact_ticks"]] == expect_i
    assert [x.n_flagged for x in s.sensitivity["queue_share"]] == [1, 1, 1, 1, 1]
    assert [x.n_flagged for x in s.sensitivity["volume_share"]] == [0, 0, 0, 0]
    d = s.distributions["queue_share_at_live"]
    expect = np.array([1 / 10_001] * 99 + [1.0])
    assert d.n == 100 and d.max == 1.0 and d.p50 == pytest.approx(float(np.quantile(expect, 0.5)))
    assert d.p99 == pytest.approx(float(np.quantile(expect, 0.99)))
    doc = json.loads(json.dumps(s.model_dump(mode="json"), allow_nan=False))
    assert doc["schema"] == "qt.shadow_impact_summary" and doc["schema_version"] == 1
    assert parse_document(doc) == s and DOC_MODELS["qt.shadow_impact_summary"] is im.ShadowImpactSummary


def test_warning_line_variants() -> None:
    res = results_table([{"shadow_id": "a", "qty": 11, "level_qty_at_live": 99}])
    user = im.shadow_impact(res, TRADES, instruments_table(), daily=daily_table(V), config=CFG)
    w = im.summarize(user, CFG).warning
    assert w is not None and "; own impact >= 1 tick: 1)" in w and "venue volume" not in w
    mixed = im.shadow_impact(
        results_table([{"shadow_id": "a", "qty": 11}, {"shadow_id": "b", "instrument_id": 2, "ts_live": MID + 1}]),
        TRADES,
        instruments_table(),
        daily=daily_table(V),
        config=CFG,
    )
    wm = im.summarize(mixed, CFG).warning
    assert wm is not None and "own impact >= 1 tick: 1; impact >= 1 tick on venue volume only" in wm
    assert "(not evaluable for 1)" in wm  # b has no tick size
    opt = CFG.model_copy(update={"flag_venue_volume_impact": True})
    venue = im.shadow_impact(res, TRADES, instruments_table(), config=opt)
    assert "; own impact >= 1 tick: 1)" in (im.summarize(venue, opt).warning or "")
    none = im.shadow_impact(res, TRADES, None, config=CFG)  # no tick sizes: impact not evaluable
    wn = im.summarize(none, CFG).warning
    assert wn is not None and "own impact: not evaluable" in wn
    assert im.warning_line(im.summarize(none, CFG), "; x") == wn + "; x"


def test_summary_of_empty_and_unflagged_tables() -> None:
    empty = im.shadow_impact(results_table([{"shadow_id": "x"}]).slice(0, 0), TRADES, instruments_table())
    s = im.summarize(empty)
    assert s.n_shadows == 0 and s.material_share is None and not s.material and s.warning is None
    assert s.distributions["impact_bps"].n == 0 and s.distributions["impact_bps"].p50 is None
    calm = im.shadow_impact(results_table([{"shadow_id": "x", "level_qty_at_live": 1_000_000}]), TRADES, None)
    assert im.summarize(calm).n_material == 0 and im.replay_warnings(None, TRADES) == []
    with pytest.raises(QtError) as e:
        im.summarize(calm.drop_columns(["material"]))
    assert e.value.code == "QT-INPUT-002"


def test_replay_warnings_one_line_and_fail_soft() -> None:
    res = results_table([{"shadow_id": "a", "qty": 50, "level_qty_at_live": 50}])
    w = im.replay_warnings(res, TRADES, instruments_table(), config=CFG)
    assert len(w) == 1 and w[0].startswith("impact: 1 of 1 shadows (100.0%)")
    assert im.replay_warnings(res, None) == [] and im.replay_warnings(res.slice(0, 0), TRADES) == []
    assert im.replay_warnings(res, TRADES.drop_columns(["size"])) == [
        "impact: not assessed (QT-INPUT-002: trades: missing column(s) size)"
    ]
    partly_null = res.set_column(res.schema.get_field_index("qty"), "qty", pa.array([None], pa.uint64()))
    assert im.replay_warnings(partly_null, TRADES)[0].startswith("impact: not assessed (QT-INPUT-002")


# ---- zoo ----------------------------------------------------------------------------------------------------------


def zoo_shadows(
    sids: list[str], cancel: list[int | None], admin: list[int | None], end: list[str | None] | None = None
) -> pa.Table:
    return pa.table(
        {
            "shadow_id": sids,
            "ts_cancel_planned": _ts(cancel),
            "ts_admin_end": _ts(admin),
            "exact_end_state": pa.array(end or [None] * len(sids), pa.utf8()),
        }
    )


def test_zoo_shadow_impact_window_and_truth(predictions: Callable[[list[dict[str, Any]]], pa.Table]) -> None:
    live = OPEN + 50 * S
    base = {"instrument_id": 1, "session_date": DAY, "ts_live": live, "qty": 10, "level_qty_at_live": 90, "p_fill": 0.5}
    pr = predictions(
        [
            {**base, "shadow_id": "z1", "horizon_ns": 10 * S, "truth_filled": False},
            {**base, "shadow_id": "z1", "horizon_ns": 100 * S, "truth_filled": True, "truth_filled_qty": 4},
            {**base, "shadow_id": "z1", "horizon_ns": 1_000 * S, "truth_filled": True},  # beyond h_max
            {**base, "shadow_id": "z2", "horizon_ns": 100 * S, "truth_filled": False},
            {**base, "shadow_id": "z3", "horizon_ns": 100 * S, "truth_filled": False},
            {**base, "shadow_id": "zr", "horizon_ns": 100 * S, "truth_filled": False},
        ]
    )
    zs = zoo_shadows(
        ["zr", "z3", "z2", "z1"],
        [None, None, live + 20 * S, None],
        [None, live + 30 * S, None, None],
        ["rejected_would_cross", None, None, "cancelled"],
    )
    t = im.zoo_shadow_impact(pr, zs, TRADES, instruments_table(), h_max_ns=150 * S, config=CFG)
    meta = t.schema.metadata
    assert meta[im.META_SOURCE.encode()] == b"zoo" and meta[im.META_H_MAX.encode()] == str(150 * S).encode()
    assert meta[im.META_HORIZON.encode()] == str(100 * S).encode()
    r = by_id(t)
    assert r["z1"]["filled_qty"] == 4 and r["z1"]["end_source"] == "horizon"
    assert r["z1"]["ts_end"].value == live + 100 * S and r["z1"]["life_volume"] == 200  # the horizon read, not h_max
    assert r["z1"]["volume_share"] == pytest.approx(4 / 204)
    assert r["z2"]["end_source"] == "cancel" and r["z2"]["ts_end"].value == live + 20 * S
    assert r["z3"]["end_source"] == "admin_end" and r["z3"]["ts_end"].value == live + 30 * S
    assert r["zr"]["status"] == "rejected" and not r["zr"]["material"]
    assert r["z1"]["queue_share_at_live"] == pytest.approx(0.1) and r["z1"]["queue_share_max"] == pytest.approx(0.1)
    assert r["z1"]["ref_price_source"] == "last_trade" and r["z1"]["ref_price"] == 10_000
    s = im.summarize(t, CFG)
    assert s.source == "zoo" and s.h_max_ns == 150 * S and s.horizon_ns == 100 * S and s.n_assessed == 3
    # no h_max: the largest horizon is the life
    t2 = im.zoo_shadow_impact(pr, zs, TRADES, instruments_table(), config=CFG)
    assert by_id(t2)["z1"]["ts_end"].value == live + 1_000 * S and im.META_H_MAX.encode() not in t2.schema.metadata
    with pytest.raises(QtError) as e:
        im.zoo_shadow_impact(pr, zs.slice(0, 2), TRADES, h_max_ns=100 * S)
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        im.zoo_shadow_impact(pr, zs, TRADES, h_max_ns=-1)
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        im.zoo_shadow_impact(pr, zs, TRADES, h_max_ns=50 * S)  # below z2's only horizon
    assert e.value.code == "QT-CONFIG-001"


def test_zoo_l2_predictions_have_no_volume_share(predictions: Callable[[list[dict[str, Any]]], pa.Table]) -> None:
    pr = predictions(
        [
            {
                "shadow_id": "z",
                "horizon_ns": 100 * S,
                "p_fill": 0.1,
                "qty": 10,
                "level_qty_at_live": None,
                "visible_level_qty_at_live": 40,
                "ts_live": OPEN + 50 * S,
            }
        ]
    )
    r = by_id(im.zoo_shadow_impact(pr, zoo_shadows(["z"], [None], [None]), TRADES, instruments_table(), config=CFG))
    z = r["z"]
    assert z["filled_qty"] is None and z["volume_share"] is None and z["life_volume"] == 200
    assert z["level_qty_at_live"] == 40 and z["queue_share_at_live"] == pytest.approx(0.2) and z["flag_queue"]


def test_zoo_warnings_on_files_and_fail_soft(
    tmp_path: Path, predictions: Callable[[list[dict[str, Any]]], pa.Table]
) -> None:
    pq.write_table(
        predictions([{"shadow_id": "z", "horizon_ns": S, "p_fill": 0.1, "qty": 10}]),
        tmp_path / "zoo_predictions.parquet",
    )
    pq.write_table(zoo_shadows(["z"], [None], [None]), tmp_path / "zoo_shadows.parquet")
    w = im.zoo_warnings(tmp_path, FIXTURE, h_max_ns=S)
    assert len(w) == 1 and w[0].startswith("impact: 1 of 1 shadows") and "volume share: not evaluable" in w[0]
    assert w[0].endswith("; per shadow: python -m queuetruth.eval.impact zoo --zoo <zoo output dir>")
    assert str(tmp_path) not in w[0]  # summary.json must not depend on the output location
    missing = im.zoo_warnings(tmp_path / "missing", FIXTURE, h_max_ns=S)
    assert len(missing) == 1 and missing[0].startswith("impact: not assessed (QT-INPUT-001: zoo output:")
    assert im.zoo_warnings(tmp_path, tmp_path / "nope")[0].startswith("impact: not assessed (QT-INPUT-001: dataset:")


def test_zoo_spec_warnings_suppressed_and_without_dataset(tmp_path: Path) -> None:
    class Ref:
        path = str(FIXTURE)

    class Spec:
        dataset = Ref()
        h_max_ns = S

    with im.suppressed():
        assert im.zoo_spec_warnings(tmp_path, Spec()) == []
    assert im.zoo_spec_warnings(tmp_path, Spec())[0].startswith("impact: not assessed (QT-INPUT-001")
    Spec.dataset = None  # type: ignore[assignment]
    assert im.zoo_spec_warnings(tmp_path, Spec()) == []


# ---- score hooks --------------------------------------------------------------------------------------------------


def test_score_warnings_count_orders_and_name_them() -> None:
    rows = [{"shadow_id": f"L{i}", "qty": 10, "level_qty_at_live": 0} for i in range(8)]
    rows.append({"shadow_id": "calm", "qty": 1, "level_qty_at_live": 1_000})
    schedule = [{"shadow_id": f"L{i}", "order_key": f"o{i // 2}"} for i in range(8)] + [
        {"shadow_id": "calm", "order_key": None}
    ]
    w = im.score_warnings(results_table(rows), TRADES, instruments_table(), schedule=schedule, config=CFG)
    assert len(w) == 1 and w[0].startswith("impact: 4 of 5 orders (80.0%)")
    assert w[0].endswith("; orders: o0, o1, o2, o3")
    many = [{"shadow_id": f"L{i}", "order_key": f"k{i}"} for i in range(8)]
    w2 = im.score_warnings(results_table(rows[:8]), TRADES, instruments_table(), schedule=many, config=CFG)
    assert w2[0].endswith("; orders: k0, k1, k2, k3, k4 (+3 more)")
    # 10 filled over a life that printed 200: 10/210 < 5% as an extra fill, 10/200 = 5% when the twin printed it
    filled = results_table(
        [{"shadow_id": "t", "qty": 10, "filled_qty": 10, "ts_full_fill": OPEN + 150 * S, "level_qty_at_live": 1_000}]
    )
    sched = [{"shadow_id": "t", "order_key": "tw"}]
    twin = im.score_warnings(filled, TRADES, instruments_table(), schedule=sched, twin_keys={"tw"}, config=CFG)
    assert len(twin) == 1 and ">= 5% of traded volume over life: 1;" in twin[0]
    assert im.score_warnings(filled, TRADES, instruments_table(), schedule=sched, config=CFG) == []
    assert im.score_warnings(None, TRADES, None, schedule=[]) == []
    assert im.score_warnings(filled, TRADES.drop_columns(["price"]), None, schedule=sched)[0].startswith(
        "impact: not assessed"
    )


def test_score_zoo_warnings_on_l2(predictions: Callable[[list[dict[str, Any]]], pa.Table]) -> None:
    pr = predictions(
        [
            {
                "shadow_id": "a",
                "horizon_ns": S,
                "p_fill": 0.1,
                "qty": 10,
                "level_qty_at_live": None,
                "visible_level_qty_at_live": 0,
                "ts_live": OPEN + S,
            },
            {
                "shadow_id": "b",
                "horizon_ns": S,
                "p_fill": 0.1,
                "qty": 1,
                "level_qty_at_live": None,
                "visible_level_qty_at_live": 1_000,
                "ts_live": OPEN + S,
            },
        ]
    )
    zs = zoo_shadows(["a", "b"], [None, None], [None, None])
    sched = [{"shadow_id": "a", "order_key": "A"}, {"shadow_id": "b", "order_key": "B"}]
    w = im.score_zoo_warnings(pr, zs, TRADES, instruments_table(), h_max_ns=S, schedule=sched, config=CFG)
    assert len(w) == 1 and w[0].startswith("impact: 1 of 2 orders (50.0%)") and w[0].endswith("; orders: A")
    assert "volume share: not evaluable" in w[0]
    assert im.score_zoo_warnings(pr, None, TRADES, None, h_max_ns=S, schedule=sched) == []


# ---- dataset access -----------------------------------------------------------------------------------------------


def test_dataset_trades_sources_agree_and_prune(tmp_path: Path) -> None:
    src = im.dataset_trades(FIXTURE)
    assert src.column_names == ["ts_event", "instrument_id", "sequence", "price", "size", "kind"]
    copy = tmp_path / "ds"
    shutil.copytree(FIXTURE / "mbo", copy / "mbo")
    mbo = im.dataset_trades(copy)
    key = ["instrument_id", "ts_event", "sequence"]
    a = src.select([*key, "price", "size"]).sort_by([(k, "ascending") for k in key])
    b = mbo.select([*key, "price", "size"]).sort_by([(k, "ascending") for k in key])
    assert a.num_rows > 0 and a.equals(b)
    iids = sorted(set(src["instrument_id"].to_pylist()))
    dates = sorted({p.name.split("=", 1)[1] for p in (FIXTURE / "trades_source").glob("date=*")})
    one = im.dataset_trades(FIXTURE, {(iids[0], dates[0])})
    assert 0 < one.num_rows < src.num_rows and set(one["instrument_id"].to_pylist()) == {iids[0]}
    none = im.dataset_trades(copy, {(iids[0], "1999-01-01")})
    assert none.num_rows == 0 and none.schema.equals(im.TRADE_COLUMNS)
    with pytest.raises(QtError) as e:
        im.dataset_trades(tmp_path / "nope")
    assert e.value.code == "QT-INPUT-001"
    (tmp_path / "empty").mkdir()
    with pytest.raises(QtError) as e:
        im.dataset_trades(tmp_path / "empty")
    assert e.value.code == "QT-INPUT-001"
    assert im.dataset_instruments(tmp_path / "empty") is None
    assert im.dataset_instruments(FIXTURE) is not None


def test_read_parts_pruning(tmp_path: Path) -> None:
    for iid, date in [(1, "2026-01-07"), (2, "2026-01-07"), (1, "2026-01-08")]:
        p = tmp_path / f"date={date}" / f"instrument_id={iid}"
        p.mkdir(parents=True)
        pq.write_table(pa.table({"instrument_id": pa.array([iid], pa.uint32()), "x": [date]}), p / "part.parquet")
    flat = tmp_path / "flat.parquet"
    pq.write_table(pa.table({"instrument_id": pa.array([1, 2], pa.uint32()), "x": ["f1", "f2"]}), flat)
    got = im._read_parts(tmp_path, ["instrument_id", "x"], {(1, "2026-01-07")})
    assert sorted(x for t in got for x in t["x"].to_pylist()) == ["2026-01-07", "f1"]
    assert sum(t.num_rows for t in im._read_parts(tmp_path, ["instrument_id", "x"], None)) == 5


def test_dataset_trades_from_l2(tmp_path: Path) -> None:
    part = tmp_path / "ds" / "l2" / "date=2026-01-07" / "instrument_id=1"
    part.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "ts_event": _ts([OPEN, OPEN + 1, OPEN + 2]),
                "instrument_id": pa.array([1, 1, 1], pa.uint32()),
                "sequence": pa.array([1, 2, 3], pa.uint64()),
                "kind": pa.array(["depth", "trade", "trade"]).dictionary_encode(),
                "price": pa.array([100, 101, 102], pa.int64()),
                "trade_qty": pa.array([0, 7, 0], pa.uint64()),
            }
        ),
        part / "part-00000.parquet",
    )
    t = im.dataset_trades(tmp_path / "ds")
    assert t.num_rows == 1 and t["size"].to_pylist() == [7] and t["price"].to_pylist() == [101]
    assert im.dataset_trades(tmp_path / "ds", {(2, "2026-01-07")}).num_rows == 0


# ---- module CLI ---------------------------------------------------------------------------------------------------


def _dataset(tmp_path: Path) -> Path:
    ds = tmp_path / "ds"
    ds.mkdir()
    pq.write_table(instruments_table(), ds / "instruments.parquet")
    part = ds / "trades_source" / "date=2026-01-07" / "instrument_id=1"
    part.mkdir(parents=True)
    pq.write_table(TRADES.filter(pa.compute.equal(TRADES["instrument_id"], 1)), part / "part.parquet")
    return ds


def test_main_replay_writes_table_and_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ds = _dataset(tmp_path)
    rep = tmp_path / "rep"
    rep.mkdir()
    pq.write_table(
        results_table([{"shadow_id": "a", "qty": 50, "level_qty_at_live": 50}]), rep / "shadow_results.parquet"
    )
    (rep / "summary.json").write_text(
        json.dumps(
            {"spec": {"dataset": {"path": str(ds)}}, "shadows": {"n": 1}, "shadow_diagnostics": {"twins_linked": 1}}
        )
    )
    out = tmp_path / "out"
    cfg = json.dumps({"rv_interval_ns": HALF, "min_returns": 2})
    assert im.main(["replay", "--replay", str(rep), "--out", str(out), "--config", cfg]) == 0
    io = capsys.readouterr()
    doc = json.loads(io.out)
    assert doc["n_shadows"] == 1 and doc["material"] and io.err.startswith("warning: impact: 1 of 1")
    assert doc["dataset"] == str(ds) and doc["fills_in_tape"]  # every shadow twin-linked in the replay summary
    assert [f["name"] for f in doc["inputs"]] == ["shadow_results.parquet"]  # trades from the dataset
    assert doc["files"][0]["name"] == "shadow_impact.parquet"
    t = pq.read_table(out / "shadow_impact.parquet")
    assert t.schema.metadata[b"qt.schema"] == b"qt.shadow_impact" and t.num_rows == 1
    assert json.loads((out / "shadow_impact_summary.json").read_text()) == doc
    assert parse_document(doc).n_shadows == 1
    # no dataset anywhere: QT-INPUT-001
    (rep / "summary.json").unlink()
    assert im.main(["replay", "--replay", str(rep), "--out", str(out)]) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["schema"] == "qt.error" and err["code"] == "QT-INPUT-001" and err["hint"] == "pass --dataset DIR"
    assert im.main(["replay", "--replay", str(rep), "--dataset", str(ds), "--out", str(out), "--config", "{bad"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "QT-INPUT-002"
    bad = tmp_path / "daily.parquet"
    pq.write_table(daily_table(1).set_column(1, "session_date", pa.array(["2019-12-30"])), bad)
    assert im.main(["replay", "--replay", str(rep), "--dataset", str(ds), "--out", str(out), "--daily", str(bad)]) == 2
    e = json.loads(capsys.readouterr().out)
    assert e["code"] == "QT-INPUT-002" and "session_date" in e["message"]
    good = tmp_path / "good.parquet"
    pq.write_table(daily_table(1_000_000), good)
    argv = ["replay", "--replay", str(rep), "--dataset", str(ds), "--out", str(out), "--daily", str(good), "--twins"]
    assert im.main(argv) == 0
    d2 = json.loads(capsys.readouterr().out)
    assert d2["volume_source"] == "user" and [f["name"] for f in d2["inputs"]] == [
        "good.parquet",
        "shadow_results.parquet",
    ]


def test_main_zoo_reads_its_summary_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], predictions: Callable[[list[dict[str, Any]]], pa.Table]
) -> None:
    ds = _dataset(tmp_path)
    zoo = tmp_path / "zoo"
    zoo.mkdir()
    base = {"instrument_id": 1, "session_date": DAY, "ts_live": OPEN + 50 * S, "qty": 10, "level_qty_at_live": 90}
    pq.write_table(
        predictions(
            [
                {**base, "shadow_id": "z", "horizon_ns": 100 * S, "p_fill": 0.5, "truth_filled": True},
                {**base, "shadow_id": "z", "horizon_ns": 1_000 * S, "p_fill": 0.5, "truth_filled": True},
            ]
        ),
        zoo / "zoo_predictions.parquet",
    )
    pq.write_table(zoo_shadows(["z"], [None], [None]), zoo / "zoo_shadows.parquet")
    (zoo / "summary.json").write_text(json.dumps({"spec": {"dataset": {"path": str(ds)}, "h_max_ns": 200 * S}}))
    assert im.main(["zoo", "--zoo", str(zoo), "--out", str(tmp_path / "o")]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["h_max_ns"] == 200 * S and doc["horizon_ns"] == 100 * S and doc["source"] == "zoo"
    assert [f["name"] for f in doc["inputs"]] == ["zoo_predictions.parquet", "zoo_shadows.parquet"]
    assert im.main(["zoo", "--zoo", str(zoo), "--h-max-ns", str(10 * S), "--out", str(tmp_path / "o")]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "QT-CONFIG-001"
    (zoo / "summary.json").write_text("{not json")
    assert im.main(["zoo", "--zoo", str(zoo), "--out", str(tmp_path / "o")]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "QT-INPUT-002"


# ---- integration --------------------------------------------------------------------------------------------------


@pytest.mark.requires_core
def test_run_zoo_carries_the_impact_warning(tmp_path: Path) -> None:
    import queuetruth.api as api

    spec = {
        "dataset": {"path": str(FIXTURE)},
        "shadows": {"path": str(FIXTURE / "truth" / "shadows.parquet")},
        "out": str(tmp_path / "zoo"),
        "models": ["front_of_queue"],
        "bootstrap_b": 5,
    }
    summ = api.run_zoo(spec)
    lines = [w for w in summ.warnings if w.startswith("impact: ")]
    assert lines == [FIXTURE_ZOO_LINE]
    # the population: every zoo shadow that rested, live inside its regular session
    zs = pq.read_table(tmp_path / "zoo" / "zoo_shadows.parquet").to_pylist()
    inst = {
        (r["instrument_id"], r["session_date"]): (
            r["midnight_utc_ns"] + r["open_s"] * S,
            r["midnight_utc_ns"] + r["close_s"] * S,
        )
        for r in pq.read_table(FIXTURE / "instruments.parquet").to_pylist()
    }
    assessed = [
        r
        for r in zs
        if not str(r["exact_end_state"] or "").startswith("rejected")
        and inst[(r["instrument_id"], r["session_date"])][0]
        <= r["ts_live"].value
        < inst[(r["instrument_id"], r["session_date"])][1]
    ]
    assert len(zs) == 1000 and len(assessed) == 880


#: The fixture's zoo warning (pinned: synth_small, default `ImpactConfig`, h_max 300 s).
FIXTURE_ZOO_LINE = (
    "impact: 640 of 880 shadows (72.7%) may not be independent of the book (>= 10% of the displayed queue at live, "
    "round lot or more: 637; >= 5% of traded volume over life: 57; impact >= 1 tick on venue volume only (upper bound, "
    "not flagged): 0); a fill rate over them could move by up to 72.7 pts; per shadow: python -m "
    "queuetruth.eval.impact zoo --zoo <zoo output dir>"
)


@pytest.mark.requires_core
def test_run_zoo_survives_an_impact_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import queuetruth.api as api

    def boom(*a: Any, **k: Any) -> pa.Table:
        raise MemoryError

    monkeypatch.setattr(im, "dataset_trades", boom)
    summ = api.run_zoo(
        {
            "dataset": {"path": str(FIXTURE)},
            "shadows": {"path": str(FIXTURE / "truth" / "shadows.parquet")},
            "out": str(tmp_path / "zoo"),
            "models": ["front_of_queue"],
            "bootstrap_b": 5,
        }
    )
    assert [w for w in summ.warnings if w.startswith("impact: ")] == [
        "impact: not assessed (QT-INPUT-002: MemoryError)"
    ]
    assert (tmp_path / "zoo" / "zoo_errors.parquet").exists() and summ.leaderboard


def _score(tmp_path: Path, trades: list[dict[str, Any]] | None = None) -> Any:
    from score.qt_score_helpers import T0, FakeRunner, log_from, make_dataset, order_events

    from queuetruth.score import build_scorecard

    px = 100_000_000_000
    live = log_from(
        [
            order_events("o1", "buy", px, 10, T0, fills=[(T0 + 2 * S, 10)]),
            order_events("o2", "buy", px, 10, T0 + S, cancel_at=T0 + 30 * S),
        ],
        tmp_path / "live",
    )
    end = [{"ts_event": T0 + 3600 * S, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1}]
    ds = make_dataset(tmp_path / "ds", end)
    bbo = [{"ts_event": T0 - S, "bid_px": px, "bid_sz": 5, "ask_px": px + 10_000_000, "ask_sz": 5}]
    # the fake engine reports every shadow alone at its price (level_qty_at_live = 0): queue share 1
    fr = FakeRunner(sim_fills={"o1": [(T0 + 3 * S, 10, "queue_depleted")]}, bbo=bbo, trades=trades or [])
    spec = {"live": {"path": str(live)}, "sim": {"kind": "exact"}, "dataset": {"path": str(ds)}}
    return build_scorecard({**spec, "bootstrap": {"b": 20}, "perm_b": 20}, runner=fr)


def test_score_carries_the_impact_warning(tmp_path: Path) -> None:
    s = _score(tmp_path)
    lines = [w for w in s.warnings if w.startswith("impact: ")]
    assert lines == [
        "impact: 2 of 2 orders (100.0%) may not be independent of the book (>= 10% of the displayed queue at live, "
        "round lot or more: 2; >= 5% of traded volume over life: 1; own impact: not evaluable); a fill rate over them "
        "could move by up to 100.0 pts; orders: o1, o2"
    ]


def test_score_survives_a_negative_price_trade(tmp_path: Path) -> None:
    from score.qt_score_helpers import T0

    s = _score(tmp_path, [{"ts_event": T0 + 5 * S, "price": -50, "size": 3}])
    lines = [w for w in s.warnings if w.startswith("impact: ")]
    assert len(lines) == 1 and lines[0].startswith("impact: 2 of 2 orders")


def test_schema_registration() -> None:
    assert im.SCHEMA.equals(SCHEMAS["qt.shadow_impact"], check_metadata=True)
    assert (REPO / "schemas" / "arrow" / "qt.shadow_impact.json").exists()
    assert (REPO / "schemas" / "json" / "qt.shadow_impact_summary.schema.json").exists()


@pytest.mark.requires_core
def test_score_on_l2_assesses_the_zoo_arm(tmp_path: Path) -> None:
    from score.test_score_l2_own_scorecard import _score as score_l2

    s = score_l2(tmp_path, "free")
    lines = [w for w in s.warnings if w.startswith("impact: ")]
    assert lines == [L2_SCORE_LINE]


#: `score` on the hand-built L2 tape of `test_score_l2_own.py` (orders A, C, D; L2: no volume share).
L2_SCORE_LINE = (
    "impact: 3 of 3 orders (100.0%) may not be independent of the book (>= 10% of the displayed queue at live, round "
    "lot or more: 3; volume share: not evaluable; own impact: not evaluable); a fill rate over them could move by up "
    "to 100.0 pts; orders: A, C, D"
)
