"""Self-exclusion (audit ruling A1) regressions: native `exclude_order_ids`, the filtered-copy fallback (sequence
holes closed), the twin-place activation of legs entered at their twin's timestamp, and sim-log detection by the
`sim:` venue prefix (E38 OC-9).

Root causes reproduced here (W3 gate): O-SCORE-4 / own_orders showed live-only discordance because (1) the filtered
copy dropped the twin's `F` executions, which under the exact fill rule are the shadow's only fill trigger once
nothing rests behind it, and dropped whole events, leaving `sequence` holes the engine reads as feed gaps
(`censored_gap`); (2) strict activation (`ts_event > ts_live`) put a leg entered at its twin's add timestamp after a
sweep sharing that nanosecond.

RD-11 (real Nasdaq days, RD-11): identical legs entered in one nanosecond (the opening-cross release) must each
link to their own twin (`order_twin_ties`); the engine-side twin semantics are tested in
`crates/replay/tests/twins.rs`."""

from __future__ import annotations

import pyarrow as pa
import pytest

from queuetruth.score import build_scorecard
from queuetruth.score._io import sim_label
from queuetruth.score.runner import DefaultRunner
from queuetruth.score.selfexclude import TwinMatch, align_to_twins, filter_mbo, order_twin_ties, twin_entry_points
from score.qt_score_helpers import MS, T0, FakeRunner, S, log_from, make_dataset, order_events

PX = 100_000_000_000
B, A, F, C, T, LAST = ord("B"), ord("A"), ord("F"), ord("C"), ord("T"), 128


def _rec(ts, action, side, size, oid, seq, flags=LAST, px=PX):
    return {
        "ts_event": ts,
        "action": action,
        "side": side,
        "price": px,
        "size": size,
        "order_id": oid,
        "flags": flags,
        "sequence": seq,
    }


def _tape(t_add: int, t_sweep: int) -> list[dict]:
    """o1 (10) rests ahead of the twin 777 (5); one sweep executes both; nothing rests behind the twin."""
    return [
        _rec(T0, ord("A"), A, 50, 9, 1, px=PX + 10_000_000),
        _rec(T0, ord("A"), B, 10, 1, 2),
        _rec(t_add, ord("A"), B, 5, 777, 3),
        _rec(t_sweep, T, A, 15, 0, 4, flags=0),
        _rec(t_sweep, F, B, 10, 1, 4, flags=0),
        _rec(t_sweep, C, B, 10, 1, 4, flags=0),
        _rec(t_sweep, F, B, 5, 777, 4, flags=0),
        _rec(t_sweep, C, B, 5, 777, 4),
        _rec(T0 + 3600 * S, ord("A"), A, 1, 10, 5, px=PX + 20_000_000),
    ]


def _live(tmp_path, t_add: int, t_fill: int):
    oe = [order_events("me", "buy", PX, 5, t_add, fills=[(t_fill, 5)], venue_order_id="777", venue="cme")]
    return log_from(oe, tmp_path / "live")


def _spec(live, ds):
    return {
        "live": {"path": str(live)},
        "sim": {"kind": "exact"},
        "dataset": {"path": str(ds)},
        "bootstrap": {"b": 20},
        "perm_b": 20,
    }


# ---- unit ---------------------------------------------------------------------------------------------------------


def test_filter_mbo_closes_sequence_holes_only():
    t = pa.table(
        {
            "order_id": pa.array([1, 777, 2, 3, 777], pa.uint64()),
            "action": pa.array([ord("A"), ord("A"), ord("A"), ord("A"), ord("C")], pa.uint8()),
            "flags": pa.array([LAST] * 5, pa.uint8()),
            "sequence": pa.array([1, 2, 3, 7, 8], pa.uint32()),
            "channel_id": pa.array([0] * 5, pa.uint8()),
        }
    )
    out = filter_mbo(t, {777})
    # seq 2 and 8 were the twin's own events: 3 -> 2 closes the hole; the tape's own gap 3 -> 7 stays (2 -> 6)
    assert out["order_id"].to_pylist() == [1, 2, 3] and out["sequence"].to_pylist() == [1, 2, 6]


def test_twin_entry_points_and_alignment(tmp_path):
    ds = make_dataset(tmp_path / "ds", _tape(T0 + MS, T0 + MS))
    pts = twin_entry_points(ds, {1: {777}})
    assert pts == {(1, 777): {T0 + MS: True}}
    tw = TwinMatch(ids={1: {777}}, by_key={"me": (1, 777)})
    sched = [{"shadow_id": "me", "order_key": "me", "ts_submit": T0 + MS, "latency_ns": 0}]
    moved, n = align_to_twins(sched, tw, pts)
    assert n == 1 and moved[0]["ts_submit"] == T0 + MS - 1 and sched[0]["ts_submit"] == T0 + MS
    # a record sharing the twin's timestamp before it: the leg is left as is (conservative)
    tape = _tape(T0 + MS, T0 + 2 * MS)
    tape.insert(2, _rec(T0 + MS, ord("A"), B, 3, 5, 3))
    for i, r in enumerate(tape):
        r["sequence"] = i + 1
    ds2 = make_dataset(tmp_path / "ds2", tape)
    assert twin_entry_points(ds2, {1: {777}}) == {(1, 777): {T0 + MS: False}}
    assert align_to_twins(sched, tw, twin_entry_points(ds2, {1: {777}}))[1] == 0


def _burst() -> list[dict]:
    """o1 (10) rests; in one nanosecond an ask, the twins 777 (5), o2 (3) and 778 (5) are added (four events, so
    neither twin opens its timestamp and no leg is aligned); a sweep executes o1 and 777 only; 778 is cancelled."""
    t1 = T0 + MS
    recs = [
        _rec(T0, ord("A"), A, 50, 9, 1, px=PX + 10_000_000),
        _rec(T0, ord("A"), B, 10, 1, 2),
        _rec(t1, ord("A"), A, 7, 3, 3, px=PX + 30_000_000),
        _rec(t1, ord("A"), B, 5, 777, 4),
        _rec(t1, ord("A"), B, 3, 2, 5),
        _rec(t1, ord("A"), B, 5, 778, 6),
        _rec(T0 + 2 * S, T, A, 15, 0, 7, flags=0),
        _rec(T0 + 2 * S, F, B, 10, 1, 7, flags=0),
        _rec(T0 + 2 * S, C, B, 10, 1, 7, flags=0),
        _rec(T0 + 2 * S, F, B, 5, 777, 7, flags=0),
        _rec(T0 + 2 * S, C, B, 5, 777, 7),
        _rec(T0 + 3 * S, C, B, 5, 778, 8),
        _rec(T0 + 3600 * S, ord("A"), A, 1, 10, 9, px=PX + 20_000_000),
    ]
    return recs


def test_identical_legs_follow_their_twins_tape_order(tmp_path):
    ds = make_dataset(tmp_path / "ds", _burst())
    tw = TwinMatch(ids={1: {777, 778}}, by_key={"zz": (1, 777), "aa": (1, 778)})
    leg = {"instrument_id": 1, "ts_submit": T0 + MS, "side": B, "price": PX, "qty": 5, "latency_ns": 0}
    other = {**leg, "shadow_id": "x", "order_key": "x", "qty": 3}
    sched = [{**leg, "shadow_id": "aa", "order_key": "aa"}, other, {**leg, "shadow_id": "zz", "order_key": "zz"}]
    out, n = order_twin_ties(ds, sched, tw)
    # the tied legs swap places (777 is first on the tape); the untwinned leg keeps its position
    assert n == 2 and [r["shadow_id"] for r in out] == ["zz", "x", "aa"]
    assert [r["shadow_id"] for r in sched] == ["aa", "x", "zz"]
    assert order_twin_ties(ds, [sched[0]], tw) == ([sched[0]], 0)


@pytest.mark.requires_core
def test_identical_legs_in_one_nanosecond_link_their_own_twins(tmp_path):
    """RD-11 5291:4009027 / 4009511 / 4009523 (opening-cross release): twins added among other orders of the same
    nanosecond activate their shadows in their own queue places; the filled twin's shadow fills, the cancelled
    twin's does not, whatever the schedule order of the identical legs."""
    ds = make_dataset(tmp_path / "ds", _burst())
    oe = [
        order_events("aa", "buy", PX, 5, T0 + MS, cancel_at=T0 + 3 * S, venue_order_id="778", venue="cme"),
        order_events("zz", "buy", PX, 5, T0 + MS, fills=[(T0 + 2 * S, 5)], venue_order_id="777", venue="cme"),
    ]
    live = log_from(oe, tmp_path / "live")
    s = build_scorecard(_spec(live, ds))
    assert s.two_by_two.both == 1 and s.two_by_two.neither == 1
    assert s.two_by_two.live_only == 0 and s.two_by_two.sim_only == 0
    assert any("ordered by their twins' tape order" in w for w in s.warnings)


def test_native_exclusion_is_passed_to_the_runner(tmp_path):
    ds = make_dataset(tmp_path / "ds", _tape(T0 + MS, T0 + 2 * S))
    live = _live(tmp_path, T0 + MS, T0 + 2 * S)
    fr = FakeRunner(native_replay_options=True)
    s = build_scorecard(_spec(live, ds), runner=fr)
    # review F7: the item carries the twin's session date (ITCH order ids restart daily)
    assert fr.replay_opts[0] == {
        "exclude_order_ids": [{"instrument_id": 1, "order_id": 777, "session_date": "2026-01-01"}]
    }
    assert fr.replay_datasets[0] == str(ds)  # no filtered copy
    assert "self_exclusion: applied via the replay option exclude_order_ids" in s.warnings


def test_sim_label_by_venue_prefix():
    assert sim_label([{"venue": "sim:hftbacktest"}], [{"source": "synthetic"}]) == "sim:hftbacktest"
    assert sim_label([{"venue": "binance"}], [{"source": "sim:nautilus"}]) == "sim:nautilus"
    assert sim_label([{"venue": "binance"}], [{"source": "synthetic"}]) is None
    assert sim_label([{"venue": "sim:"}], []) is None


# ---- real engine ----------------------------------------------------------------------------------------------------


class _Fallback(DefaultRunner):
    native_replay_options = False


@pytest.mark.requires_core
def test_twin_executed_with_nothing_behind(tmp_path):
    """Minimal O-SCORE-4 regression: the twin's own execution is the shadow's only fill trigger. Native exclusion
    credits it (zero discordance); the filtered copy loses it (the original live-only discordance)."""
    ds = make_dataset(tmp_path / "ds", _tape(T0 + MS, T0 + 2 * S))
    live = _live(tmp_path, T0 + MS, T0 + 2 * S)
    s = build_scorecard(_spec(live, ds))
    assert s.two_by_two.both == 1 and s.two_by_two.live_only == 0 and s.two_by_two.sim_only == 0
    fb = build_scorecard(_spec(live, ds), runner=_Fallback())
    assert fb.two_by_two.live_only == 1
    assert any("filtered dataset copy (fallback" in w for w in fb.warnings)


@pytest.mark.requires_core
def test_twin_swept_in_its_add_nanosecond(tmp_path):
    """Entry tie: the twin is added and swept at the same `ts_event` (two events). The leg activates in the twin's
    place (1 ns before its add) and fills as maker like the live order."""
    ds = make_dataset(tmp_path / "ds", _tape(T0 + MS, T0 + MS))
    live = _live(tmp_path, T0 + MS, T0 + MS)
    s = build_scorecard(_spec(live, ds))
    assert "self_exclusion: 1 leg(s) activated in their twin's queue place" in s.warnings
    assert s.two_by_two.both == 1 and s.two_by_two.live_only == 0


def test_referee_names_simulator_from_venue_prefix():
    from queuetruth.score.referee import load_sim

    orders = pa.Table.from_pylist([{"order_key": "k", "venue": "sim:hftbacktest", "orig_qty": 5}])
    events = pa.Table.from_pylist(
        [
            {
                "order_key": "k",
                "event_type": "fill",
                "qty": 5,
                "px": PX,
                "fill_id": "1",
                "ts_exchange": T0,
                "source": "synthetic",
            }
        ]
    )
    sim, name = load_sim((orders, events))
    assert name == "hftbacktest" and sim == {"k": (5, T0)}


# ---- RD-11 follow-up: dated items, by-id time check, display-slid twins (review F7), race-free 2x2 (M15) --------

TICK_PX = 10_000_000


def _slid_tape() -> list[dict]:
    """An ask o1 (50) rests at PX + 1 tick; the twin 777 (sell, limit PX) is displayed at PX + 1 tick behind it
    (display-price sliding); a buyer at PX executes the twin at its limit while o1 is still displayed ahead."""
    up = PX + TICK_PX
    return [
        _rec(T0, ord("A"), B, 50, 9, 1, px=PX - 5 * TICK_PX),
        _rec(T0, ord("A"), A, 50, 1, 2, px=up),
        _rec(T0 + MS, ord("A"), A, 5, 777, 3, px=up),
        _rec(T0 + 2 * S, T, B, 5, 0, 4, flags=0, px=PX),
        _rec(T0 + 2 * S, F, A, 5, 777, 4, flags=0, px=PX),
        _rec(T0 + 2 * S, C, A, 5, 777, 4, px=up),
        _rec(T0 + 3600 * S, ord("A"), B, 1, 10, 5, px=PX - 6 * TICK_PX),
    ]


def test_by_id_match_at_another_price_needs_the_time_window(tmp_path):
    from queuetruth.score.selfexclude import match_twins, twin_display_prices

    ds = make_dataset(tmp_path / "ds", _slid_tape())
    order = {"order_key": "me", "instrument_id": 1, "side": A, "price": PX, "qty": 5, "venue_order_id": "777"}
    tw = match_twins(ds, [{**order, "ts_entry": T0 + MS}])
    assert tw.by_id == 1 and tw.display_px == {"me": PX + TICK_PX} and tw.days == {(1, 20454): {777}}
    sched = [{"shadow_id": "me", "order_key": "me", "ts_submit": T0 + MS, "price": PX, "price_mode": "abs"}]
    moved, n = twin_display_prices(sched, tw)
    assert n == 1 and moved[0]["price"] == PX + TICK_PX and sched[0]["price"] == PX
    # at another price and outside the match window the id alone is not trusted (review F7)
    far = match_twins(ds, [{**order, "ts_entry": T0 + S}])
    assert far.by_id == 0 and far.unmatched == 1 and not far.display_px
    # at the order's own price the id match needs no time agreement (send_plus_latency entries differ from the add)
    same = match_twins(ds, [{**order, "price": PX + TICK_PX, "ts_entry": T0 + S}])
    assert same.by_id == 1 and not same.display_px


@pytest.mark.requires_core
def test_display_slid_order_fills_like_the_live_order(tmp_path):
    """RD-11 13:246882941 and eight more real orders: the live limit (PX) is not the displayed price (PX + 1 tick);
    matched by id, the leg rests at the displayed price, links to its twin and is filled by the twin's execution at
    the limit although o1 is still displayed ahead."""
    ds = make_dataset(tmp_path / "ds", _slid_tape())
    oe = [order_events("me", "sell", PX, 5, T0 + MS, fills=[(T0 + 2 * S, 5)], venue_order_id="777", venue="cme")]
    s = build_scorecard(_spec(log_from(oe, tmp_path / "live"), ds))
    assert s.two_by_two.both == 1 and s.two_by_two.live_only == 0 and s.two_by_two.sim_only == 0
    assert any("display-slid" in w for w in s.warnings)


@pytest.mark.requires_core
def test_race_free_two_by_two_and_v11_columns(tmp_path):
    """M15: with the twin left in the tape the shadow activates 0.5 ms after its entry (race window = match window,
    1 ms), so the order is race-flagged and the race-free 2x2 leaves it out. E52: the 1.1 columns are written."""
    from queuetruth.score._io import read_parquet, rows

    ds = make_dataset(tmp_path / "ds", _tape(T0 + MS, T0 + MS + MS // 2))
    live = _live(tmp_path, T0 + MS, T0 + MS + MS // 2)
    sp = {**_spec(live, ds), "out": str(tmp_path / "out")}
    s = build_scorecard(sp, self_exclude=False)
    assert s.two_by_two.live_only == 1
    race = [w for w in s.warnings if w.startswith("race: ")]
    assert race == [
        "race: 1 of 1 order(s) race-flagged (exact-arm ambiguous_race); 2x2 without them "
        "both/neither/live_only/sim_only = 0/0/0/0 (McNemar p = 1)"
    ]
    tests = rows(read_parquet(tmp_path / "out" / "score_tests.parquet"))
    assert any(t["test_id"] == "mcnemar_race_free" and t["stratum"] == "race_free" for t in tests)
    so = read_parquet(tmp_path / "out" / "score_orders.parquet")
    assert so.column_names[-2:] == ["price_exp", "size_exp"] and rows(so)[0]["price_exp"] == -9
    sf = rows(read_parquet(tmp_path / "out" / "score_fills.parquet"))
    assert sf and all(f["price_exp"] == -9 and f["size_exp"] == 0 for f in sf)
    mk = read_parquet(tmp_path / "out" / "score_markouts.parquet")
    assert mk.column_names[-2:] == ["mo_bps", "mo_net_ccy"]
    # self-excluded, the twin's shadow is placed exactly: no race flag
    s2 = build_scorecard({**sp, "out": None})
    assert s2.two_by_two.both == 1 and any(w.startswith("race: 0 of 1") for w in s2.warnings)
