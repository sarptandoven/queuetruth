"""build_scorecard with an injected fake engine runner: arms, outputs, determinism, and the audit rulings A1
(self-exclusion), A5 (taker fills), A8 (instruction-sized legs), A9 (evaluation window), fills-only coverage."""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator

from queuetruth.errors import QtError
from queuetruth.score import build_scorecard
from queuetruth.score._io import read_parquet, rows
from score.qt_score_helpers import (
    MS,
    T0,
    FakeRunner,
    S,
    log_from,
    make_dataset,
    make_table,
    order_events,
    sha,
    simple_schedule,
)

PX = 100_000_000_000
BBO = [{"ts_event": T0 - S, "bid_px": PX, "bid_sz": 5, "ask_px": PX + 10_000_000, "ask_sz": 5}]


def _spec(live, ds=None, out=None, **kw):
    d = {"live": {"path": str(live)}, "sim": {"kind": "exact"}, "bootstrap": {"b": 50}, "perm_b": 50}
    if ds is not None:
        d["dataset"] = {"path": str(ds)}
    if out is not None:
        d["out"] = str(out)
    d.update(kw)
    return d


def _end_mbo():
    return [{"ts_event": T0 + 3600 * S, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1}]


@pytest.fixture
def basic(tmp_path):
    oe = [
        order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 10)]),
        order_events("o2", "buy", PX, 10, T0 + S, cancel_at=T0 + 30 * S),
        order_events("o3", "sell", PX + 10_000_000, 5, T0 + 2 * S, fills=[(T0 + 5 * S, 5)]),
        order_events("o4", "buy", PX, 3, T0 + 3 * S),
    ]
    live = log_from(oe, tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(
        sim_fills={"o1": [(T0 + 3 * S, 10, "queue_depleted")], "o4": [(T0 + 9 * S, 3, "queue_depleted")]},
        bbo=BBO,
        queue_ahead={"o1": 40, "o2": 7},
    )
    return live, ds, fr


def test_exact_arm_outputs_and_schema(basic, tmp_path, repo_root):
    live, ds, fr = basic
    out = tmp_path / "out"
    s = build_scorecard(_spec(live, ds, out), runner=fr)
    assert s.two_by_two.model_dump() == {"both": 1, "neither": 1, "live_only": 1, "sim_only": 1}
    assert s.n_orders == 4 and s.n_matched == 4 and s.sim_source.kind == "exact"
    names = sorted(f.name for f in s.files)
    assert names == [
        "schedule.parquet",
        "score_fills.parquet",
        "score_markouts.parquet",
        "score_orders.parquet",
        "score_position.parquet",
        "score_shortfall.parquet",
        "score_tests.parquet",
    ]
    for f in s.files:
        assert sha(out / f.name) == f.sha256
    so = {r["order_key"]: r for r in rows(read_parquet(out / "score_orders.parquet"))}
    assert so["o1"]["queue_ahead_at_entry"] == 40 and so["o1"]["queue_ahead_source"] == "exact"
    assert so["o1"]["ttf_live_ns"] == 2 * S and so["o1"]["ttf_sim_ns"] == 3 * S
    assert so["o3"]["class"] == "live_only" and so["o4"]["class"] == "sim_only"
    assert so["o2"]["live_end"] == "cancelled"
    doc = json.loads((out / "summary.json").read_text())
    schema = json.loads((repo_root / "schemas" / "json" / "qt.scorecard_summary.schema.json").read_text())
    Draft202012Validator(schema).validate(doc)
    tests = read_parquet(out / "score_tests.parquet")
    assert tests.schema.metadata[b"qt.schema"] == b"qt.score_tests"
    mk = rows(read_parquet(out / "score_markouts.parquet"))
    assert {r["arm"] for r in mk} == {"live", "sim"}


def test_deterministic_outputs(basic, tmp_path):
    live, ds, fr = basic
    a = build_scorecard(_spec(live, ds, tmp_path / "a"), runner=fr)
    b = build_scorecard(_spec(live, ds, tmp_path / "b"), runner=fr)
    assert [f.sha256 for f in a.files] == [f.sha256 for f in b.files]
    assert (tmp_path / "a" / "summary.json").read_bytes() == (tmp_path / "b" / "summary.json").read_bytes()
    assert "out" not in a.spec and a.run_id == b.run_id


def test_exchange_ack_rejects_nonzero_latency(basic):
    live, ds, fr = basic
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live, ds, latency={"gateway_ns": 5}), runner=fr)
    assert e.value.code == "QT-CONFIG-001"


def test_a5_crossed_sim_fill_is_taker(tmp_path):
    live = log_from([order_events("o1", "buy", PX, 10, T0)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(sim_fills={"o1": [(T0 + S, 10, "crossed_at_live")]}, bbo=BBO)
    s = build_scorecard(_spec(live, ds, tmp_path / "out"), runner=fr)
    assert s.two_by_two.neither == 1
    fills = rows(read_parquet(tmp_path / "out" / "score_fills.parquet"))
    assert [(f["arm"], f["liquidity"]) for f in fills] == [("sim", "taker")]
    assert any(w.startswith("taker_fills: live 0, sim 1") for w in s.warnings)


def test_a9_window_ignores_late_fills_in_both_arms(tmp_path):
    oe = [
        order_events("late", "buy", PX, 10, T0, fills=[(T0 + 70 * S, 10)]),
        order_events(
            "cxl",
            "buy",
            PX,
            10,
            T0,
            fills=[(T0 + 10 * S + 200 * MS, 10)],
            cancel_at=T0 + 10 * S + 500 * MS,
            cancel_sent=T0 + 10 * S,
        ),
    ]
    live = log_from(oe, tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(
        sim_fills={
            "late": [(T0 + 65 * S, 10, "queue_depleted")],
            "cxl": [(T0 + 10 * S + 400 * MS, 10, "queue_depleted")],
        },
        bbo=BBO,
    )
    s = build_scorecard(_spec(live, ds, tmp_path / "out"), runner=fr)
    assert s.two_by_two.neither == 2
    s2 = build_scorecard(_spec(live, ds), runner=fr, window_ns=120 * S)
    so = s2.two_by_two
    assert so.both == 1 and so.neither == 1


def _two_leg_schedule(live_dir, *, entry, latency_ns):
    t, _ = simple_schedule(live_dir, entry=entry, latency_ns=latency_ns)
    r = rows(t)[0]
    leg1 = dict(r, ts_cancel=T0 + 5 * S)
    leg2 = dict(r, shadow_id="o1.2", price=PX - 10_000_000, qty=6, ts_submit=T0 + 5 * S, ts_cancel=None)
    return make_table("qt.shadow_spec", [leg1, leg2]), make_table("qt.shadow_amend", [])


def test_a8_legs_follow_instructions_and_aggregate(tmp_path):
    o, ev = order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 4), (T0 + 8 * S, 6)])
    ev.insert(
        3,
        {
            "order_key": "o1",
            "event_type": "amend_ack",
            "ts_exchange": T0 + 5 * S,
            "qty": 10,
            "px": PX - 10_000_000,
            "ts_exchange_kind": "match_time",
            "ts_precision": "ns",
            "source": "synthetic",
            "derived": False,
        },
    )
    for i, e in enumerate(ev, start=1):
        e["event_seq"] = i
    live = log_from([(o, ev)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(
        sim_fills={"o1": [(T0 + S, 3, "queue_depleted")], "o1.2": [(T0 + 6 * S, 10, "queue_depleted")]},
        bbo=BBO,
        schedule_fn=_two_leg_schedule,
    )
    build_scorecard(_spec(live, ds, tmp_path / "out"), runner=fr)
    sent = fr.schedules[0]
    assert [r["qty"] for r in sent] == [10, 10]  # instruction total, not 10 - live cum (6)
    so = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))[0]
    assert so["legs"] == 2 and so["qty"] == 10
    assert so["sim_filled_qty"] == 10  # 3 on leg 1, leg 2 truncated to 10 - 3
    assert so["live_filled_qty"] == 10 and so["class"] == "both"


def test_a1_self_exclusion_by_venue_order_id(tmp_path):
    oe = [order_events("o1", "buy", PX, 10, T0 + MS, venue_order_id="777")]
    live = log_from(oe, tmp_path / "live")
    mbo = [
        {"ts_event": T0, "action": ord("A"), "side": ord("B"), "price": PX, "size": 10, "order_id": 777, "flags": 128},
        {"ts_event": T0 + S, "action": ord("A"), "side": ord("B"), "price": PX, "size": 4, "order_id": 5, "flags": 0},
        {
            "ts_event": T0 + S,
            "action": ord("C"),
            "side": ord("B"),
            "price": PX,
            "size": 10,
            "order_id": 777,
            "flags": 128,
        },
        *_end_mbo(),
    ]
    ds = make_dataset(tmp_path / "ds", mbo)
    fr = FakeRunner(bbo=BBO)
    s = build_scorecard(_spec(live, ds), runner=fr)
    assert "self_exclusion: matched 1/1 live orders to MBO order ids (100.0%; by id 1, heuristic 0)" in s.warnings
    used = fr.replay_datasets[0]
    assert used != str(ds)
    t = fr.mbo_seen[0]
    assert 777 not in t["order_id"]
    assert t["order_id"][0] == 5 and t["flags"][0] == 128  # LAST of the dropped cancel moved to the kept add (was 0)
    fr2 = FakeRunner(bbo=BBO)
    build_scorecard(_spec(live, ds), runner=fr2, self_exclude=False)
    assert fr2.replay_datasets[0] == str(ds)


def test_a1_self_exclusion_heuristic_match(tmp_path):
    oe = [order_events("o1", "sell", PX, 7, T0 + 500_000)]
    live = log_from(oe, tmp_path / "live")
    mbo = [
        {"ts_event": T0 - 5 * MS, "action": ord("A"), "side": ord("A"), "price": PX, "size": 7, "order_id": 41},
        {"ts_event": T0, "action": ord("A"), "side": ord("A"), "price": PX, "size": 7, "order_id": 42},
        *_end_mbo(),
    ]
    ds = make_dataset(tmp_path / "ds", mbo)
    fr = FakeRunner(bbo=BBO)
    s = build_scorecard(_spec(live, ds), runner=fr)
    assert any("by id 0, heuristic 1" in w for w in s.warnings)
    assert fr.mbo_seen[0]["order_id"][:1] == [41] and 42 not in fr.mbo_seen[0]["order_id"]


def test_fills_only_source(tmp_path):
    oe = [order_events("o1", "buy", PX, 10, T0, fills=[(T0 + S, 10)])]
    live = log_from(oe, tmp_path / "live", manifest_extra={"coverage": "fills_only"})
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(sim_fills={"o1": [(T0 + 2 * S, 10, "queue_depleted")]}, bbo=BBO)
    s = build_scorecard(_spec(live, ds, gates={"max_abs_fill_rate_bias": 0.1}), runner=fr)
    assert s.bias.fill_rate_bias is None and s.mcnemar.p_exact is None
    assert "not computable: fills-only source" in s.verdict.headline
    assert s.verdict.gates_passed is False
    assert s.ttf.n_joint == 1 and s.ttf.median_log_ratio is not None
    assert any(m.n > 0 for m in s.markouts)


def test_external_arm_canonical_sim_log_without_engine_columns(basic, tmp_path):
    live, ds, fr = basic
    sim = tmp_path / "sim"
    sim.mkdir()
    pq.write_table(pa.table({"order_key": ["o1", "o3", "zz"]}), sim / "orders.parquet")
    pq.write_table(
        pa.table(
            {
                "order_key": ["o1", "o3", "zz"],
                "event_type": ["fill", "fill", "fill"],
                "qty": [10, 5, 1],
                "px": [PX, PX, PX],
                "fill_id": ["a", "b", "c"],
                "ts_exchange": [T0 + 4 * S, T0 + 6 * S, T0],
                "source": ["sim:hbt"] * 3,
            }
        ),
        sim / "events.parquet",
    )
    s = build_scorecard(_spec(live, ds, sim={"kind": "external", "log": {"path": str(sim)}}), runner=fr)
    assert s.n_unmatched_sim == 1 and s.n_unmatched_live == 2 and s.n_matched == 2
    assert s.two_by_two.both == 2
    assert s.sim_source.kind == "external" and s.sim_source.path == str(sim)


def test_zoo_arm(basic, tmp_path):
    live, ds, fr = basic
    s = build_scorecard(_spec(live, ds, tmp_path / "out", sim={"kind": "zoo", "model": "front_of_queue"}), runner=fr)
    assert s.sim_source.model_id == "front_of_queue"
    zs = fr.zoo_specs[0]
    assert zs.models == ["front_of_queue"] and zs.write_fills
    so = {r["order_key"]: r for r in rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))}
    assert so["o1"]["trade_vol_60s"] == 3 and so["o1"]["sim_model_id"] == "front_of_queue"
    assert s.two_by_two.both == 1 and s.two_by_two.sim_only == 1


def test_sim_exact_requires_dataset(basic):
    live, _, fr = basic
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live), runner=fr)
    assert e.value.code == "QT-CONFIG-001"


def test_live_fill_bust_and_correct_and_dedupe(tmp_path):
    o, ev = order_events("o1", "buy", PX, 10, T0, fills=[(T0 + S, 4), (T0 + 2 * S, 6)])
    extra = [
        {"event_type": "fill_bust", "ref_fill_id": "o1-f0", "ts_exchange": T0 + 3 * S},
        {"event_type": "fill_correct", "ref_fill_id": "o1-f1", "qty": 5, "ts_exchange": T0 + 3 * S},
        {"event_type": "fill", "fill_id": "o1-f1", "qty": 6, "px": PX, "ts_exchange": T0 + 4 * S},
    ]
    for e in extra:
        e.update(order_key="o1", ts_exchange_kind="match_time", ts_precision="ns", source="synthetic", derived=False)
    ev += extra
    for i, e in enumerate(ev, start=1):
        e["event_seq"] = i
    live = log_from([(o, ev)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    s = build_scorecard(_spec(live, ds, tmp_path / "out"), runner=FakeRunner(bbo=BBO))
    so = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))[0]
    assert so["live_filled_qty"] == 5 and so["live_ts_first_fill"] == T0 + 2 * S
    assert any("fill_dedupe: 1" in w for w in s.warnings)
