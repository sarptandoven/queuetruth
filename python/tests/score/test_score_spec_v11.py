"""Schema 1.1 wiring of `build_scorecard` (SPEC 3.11 #8, #9, #12, #21; E39/40; review F12): every
ScorecardSpec 1.1 field is honoured (window_ns, match_window_ns, self_exclude, markout_past_end), keyword overrides
fold into the effective spec (echo + run_id, OC-34), the 1.1 summary fields are filled natively, the zoo arm gets
native exclusion and amends, and `qt.score_fills` is written in the 1.1 layout."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator

import queuetruth.score.scorecard as sc
from queuetruth.errors import QtError
from queuetruth.schemas.arrow import FULL_SCHEMAS, conform
from queuetruth.score import build_scorecard, compute_markouts
from queuetruth.score._io import make_table, read_parquet, rows
from queuetruth.score.runner import DefaultRunner
from score.qt_score_helpers import MS, T0, TICK, FakeRunner, S, log_from, make_dataset, order_events, simple_schedule

PX = 100_000_000_000
BBO = [{"ts_event": T0 - S, "bid_px": PX, "bid_sz": 5, "ask_px": PX + TICK, "ask_sz": 5}]
B, A, F, C, T, LAST = ord("B"), ord("A"), ord("F"), ord("C"), ord("T"), 128


def _end_mbo(t: int = T0 + 3600 * S):
    return [{"ts_event": t, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1}]


def _spec(live, ds=None, out=None, **kw):
    d = {"live": {"path": str(live)}, "sim": {"kind": "exact"}, "bootstrap": {"b": 30}, "perm_b": 30}
    if ds is not None:
        d["dataset"] = {"path": str(ds)}
    if out is not None:
        d["out"] = str(out)
    d.update(kw)
    return d


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


def _twin_tape(t_add: int, t_sweep: int) -> list[dict]:
    """o1 (10) rests ahead of the twin 777 (5); one sweep executes both (same tape as test_score_selfexclude)."""
    return [
        _rec(T0, A, A, 50, 9, 1, px=PX + TICK),
        _rec(T0, A, B, 10, 1, 2),
        _rec(t_add, A, B, 5, 777, 3),
        _rec(t_sweep, T, A, 15, 0, 4, flags=0),
        _rec(t_sweep, F, B, 10, 1, 4, flags=0),
        _rec(t_sweep, C, B, 10, 1, 4, flags=0),
        _rec(t_sweep, F, B, 5, 777, 4, flags=0),
        _rec(t_sweep, C, B, 5, 777, 4),
        _rec(T0 + 3600 * S, A, A, 1, 10, 5, px=PX + 2 * TICK),
    ]


def _twin_live(tmp_path: Path, t_ack: int, t_fill: int) -> Path:
    oe = [order_events("me", "buy", PX, 5, t_ack, fills=[(t_fill, 5)], venue_order_id="777", venue="cme")]
    return log_from(oe, tmp_path / "live")


@pytest.fixture
def late_fill(tmp_path):
    """o4: buy 3 entered at T0+3 s, never filled live; the sim fills it 6 s after entry (review F12 repro)."""
    live = log_from([order_events("o4", "buy", PX, 3, T0 + 3 * S)], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(sim_fills={"o4": [(T0 + 9 * S, 3, "queue_depleted")]}, bbo=BBO)
    return live, ds, fr


# ---- window_ns ------------------------------------------------------------------------------------------------------


def test_window_ns_from_spec_is_the_evaluation_window(late_fill, tmp_path):
    """Review F12 (`exp_window_spec_ignored.py`): spec window_ns = 5 s, sim fill 6 s after entry -> not counted."""
    live, ds, fr = late_fill
    s = build_scorecard(_spec(live, ds, tmp_path / "out", window_ns=5 * S), runner=fr)
    o = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))[0]
    assert s.spec["window_ns"] == 5 * S
    assert o["sim_filled"] is False and o["class"] == "neither"
    assert "within 5 s" in s.verdict.headline
    d = build_scorecard(_spec(live, ds), runner=fr)
    assert d.two_by_two.sim_only == 1 and d.spec["window_ns"] == 60 * S


def test_keyword_overrides_fold_into_echo_and_run_id(late_fill):
    live, ds, fr = late_fill
    over = build_scorecard(_spec(live, ds, window_ns=5 * S), runner=fr, window_ns=10 * S, self_exclude=False)
    assert over.spec["window_ns"] == 10 * S and over.spec["self_exclude"] is False
    assert over.two_by_two.sim_only == 1  # H = 10 s from the keyword, not 5 s from the spec
    direct = build_scorecard(_spec(live, ds, window_ns=10 * S, self_exclude=False), runner=fr)
    assert over.run_id == direct.run_id
    five = build_scorecard(_spec(live, ds, window_ns=5 * S, self_exclude=False), runner=fr)
    assert five.run_id != over.run_id
    default = build_scorecard(_spec(live, ds), runner=fr)
    kw_default = build_scorecard(_spec(live, ds), runner=fr, window_ns=60 * S, match_window_ns=MS, self_exclude=True)
    assert default.run_id == kw_default.run_id  # 1.1 defaults leave the v0.1 run_id unchanged


def test_effective_spec_revalidates_overrides(late_fill):
    live, ds, fr = late_fill
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live, ds), runner=fr, window_ns=0)
    assert e.value.code == "QT-CONFIG-001" and "window_ns" in str(e.value)
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live, ds), runner=fr, match_window_ns=-1)
    assert e.value.code == "QT-CONFIG-001" and "match_window_ns" in str(e.value)


# ---- match_window_ns / self_exclude ---------------------------------------------------------------------------------


def _heuristic_case(tmp_path):
    """Live sell 7 acked 0.5 ms after an anonymous MBO add of the same side, price and size (heuristic twin)."""
    live = log_from([order_events("o1", "sell", PX, 7, T0 + 500_000)], tmp_path / "live")
    mbo = [_rec(T0, A, A, 7, 42, 1), *_end_mbo()]
    for i, r in enumerate(mbo):
        r["sequence"] = i + 1
    return live, make_dataset(tmp_path / "ds", mbo)


def test_match_window_ns_reaches_match_twins(tmp_path, monkeypatch):
    live, ds = _heuristic_case(tmp_path)
    seen: list[int] = []
    real = sc.match_twins

    def spy(root, orders, window_ns):
        seen.append(window_ns)
        return real(root, orders, window_ns)

    monkeypatch.setattr(sc, "match_twins", spy)
    wide = build_scorecard(_spec(live, ds), runner=FakeRunner(bbo=BBO))
    assert seen == [1_000_000] and wide.n_twin_unmatched == 0 and wide.twin_match_rate == 1.0
    narrow = build_scorecard(_spec(live, ds, match_window_ns=100_000), runner=FakeRunner(bbo=BBO))
    assert seen[-1] == 100_000 and narrow.n_twin_unmatched == 1 and narrow.twin_match_rate == 0.0
    assert narrow.spec["match_window_ns"] == 100_000


def test_self_exclude_false_disables_twin_matching(tmp_path):
    ds = make_dataset(tmp_path / "ds", _twin_tape(T0 + MS, T0 + 2 * S))
    live = _twin_live(tmp_path, T0 + MS, T0 + 2 * S)
    on = FakeRunner(native_replay_options=True, bbo=BBO)
    s_on = build_scorecard(_spec(live, ds), runner=on)
    assert on.replay_opts[0] == {
        "exclude_order_ids": [{"instrument_id": 1, "order_id": 777, "session_date": "2026-01-01"}]
    }
    assert s_on.n_twin_unmatched == 0 and s_on.twin_match_rate == 1.0
    off = FakeRunner(native_replay_options=True, bbo=BBO)
    s_off = build_scorecard(_spec(live, ds, self_exclude=False), runner=off)
    assert off.replay_opts[0] == {} and off.replay_datasets[0] == str(ds)
    assert s_off.n_twin_unmatched is None and s_off.twin_match_rate is None
    assert not any(w.startswith("self_exclusion") for w in s_off.warnings)
    assert s_off.spec["self_exclude"] is False


# ---- markout_past_end -----------------------------------------------------------------------------------------------


def _past_end_case(tmp_path):
    live = log_from([order_events("o1", "buy", PX, 2, T0, fills=[(T0 + 2 * S, 2)])], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo(T0 + 10 * S))  # end of data 10 s after entry
    bbo = [*BBO, {"ts_event": T0 + 5 * S, "bid_px": PX + TICK, "bid_sz": 5, "ask_px": PX + 2 * TICK, "ask_sz": 5}]
    fr = FakeRunner(sim_fills={"o1": [(T0 + 3 * S, 2, "queue_depleted")]}, bbo=bbo)
    return live, ds, fr


def test_markout_past_end_last_quote(tmp_path):
    live, ds, fr = _past_end_case(tmp_path)
    kw = {"markout_horizons_ns": [S, 60 * S]}
    build_scorecard(_spec(live, ds, tmp_path / "a", **kw), runner=fr)
    build_scorecard(_spec(live, ds, tmp_path / "b", markout_past_end="last_quote", **kw), runner=fr)

    def mo(d):
        return {(r["arm"], r["horizon_ns"]): r["mo_ticks"] for r in rows(read_parquet(d / "score_markouts.parquet"))}

    a, b = mo(tmp_path / "a"), mo(tmp_path / "b")
    assert a[("live", 60 * S)] is None and a[("sim", 60 * S)] is None
    assert b[("live", 60 * S)] == 1.5 and b[("sim", 60 * S)] == 1.5  # last quote mid PX + 1.5 ticks
    assert a[("live", S)] == b[("live", S)] == 0.5
    summ = json.loads((tmp_path / "b" / "summary.json").read_text())
    assert summ["spec"]["markout_past_end"] == "last_quote"


def test_compute_markouts_past_end_unit():
    inst = make_table(
        "qt.instruments",
        [
            {
                "instrument_id": 1,
                "session_date": 0,
                "symbol": "X",
                "venue": "generic",
                "source_venue": "x",
                "price_exp": -2,
                "tick_size": 1,
                "lot_size": 1,
                "size_exp": 0,
                "contract_multiplier": 1.0,
                "currency": "USD",
                "session_tz": "UTC",
                "midnight_utc_ns": 0,
                "open_s": 0,
                "close_s": 86_400,
                "source": "t",
            }
        ],
    )
    bbo = pa.table(
        {"ts_event": [0, 50], "bid_px": [100, 102], "bid_sz": [1, 1], "ask_px": [102, 104], "ask_sz": [1, 1]}
    )
    fills = pa.table({"order_key": ["k"], "arm": ["live"], "fill_seq": [1], "ts": [10], "qty": [1], "px": [101]})
    fills = fills.append_column("side", pa.array(["buy"]))
    hz = [20, 1_000]
    plain = rows(compute_markouts(fills, bbo, inst, hz, ["mid"]))
    last = rows(compute_markouts(fills, bbo, inst, hz, ["mid"], past_end="last_quote"))
    assert [r["mo_ticks"] for r in plain] == [0.0, None]
    assert [r["mo_ticks"] for r in last] == [0.0, 2.0]
    with pytest.raises(QtError) as e:
        compute_markouts(fills, bbo, inst, hz, ["mid"], past_end="next_quote")
    assert e.value.code == "QT-CONFIG-001"


# ---- summary 1.1 fields ---------------------------------------------------------------------------------------------


@pytest.fixture
def basic(tmp_path):
    oe = [
        order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 10)]),
        order_events("o2", "buy", PX, 10, T0 + S, cancel_at=T0 + 30 * S),
        order_events("o3", "sell", PX + TICK, 5, T0 + 2 * S, fills=[(T0 + 5 * S, 5)]),
        order_events("o4", "buy", PX, 3, T0 + 3 * S),
    ]
    live = log_from(oe, tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(
        sim_fills={"o1": [(T0 + 3 * S, 10, "queue_depleted")], "o4": [(T0 + 9 * S, 3, "crossed_at_live")]},
        bbo=BBO,
    )
    return live, ds, fr


def test_summary_v11_fields_native(basic, tmp_path, repo_root):
    live, ds, fr = basic
    out = tmp_path / "out"
    s = build_scorecard(_spec(live, ds, out), runner=fr)
    assert s.fills_only is False
    assert s.n_taker_fills is not None and s.n_taker_fills.model_dump() == {"live": 0, "sim": 1}
    assert s.n_twin_unmatched == 4 and s.twin_match_rate == 0.0  # MBO + self_exclude: matching ran, no twins
    dc = s.discordance_clustered
    row = next(r for r in rows(read_parquet(out / "score_tests.parquet")) if r["test_id"] == "discordance_clustered")
    assert dc is not None and dc.method == "block_bootstrap" and dc.n_clusters == 1
    assert (dc.estimate, dc.p_value, dc.ci_lo, dc.ci_hi) == (row["value"], row["p_value"], row["ci_lo"], row["ci_hi"])
    assert any(w.startswith("clusters: 1") for w in s.warnings)  # v0.1 readers keep their warning
    doc = json.loads((out / "summary.json").read_text())
    schema = json.loads((repo_root / "schemas" / "json" / "qt.scorecard_summary.schema.json").read_text())
    Draft202012Validator(schema).validate(doc)
    assert doc["fills_only"] is False and doc["discordance_clustered"]["n_clusters"] == 1


def test_summary_fills_only_and_no_dataset(tmp_path):
    oe = [order_events("o1", "buy", PX, 10, T0, fills=[(T0 + S, 10)])]
    live = log_from(oe, tmp_path / "live", manifest_extra={"coverage": "fills_only"})
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(sim_fills={"o1": [(T0 + 2 * S, 10, "queue_depleted")]}, bbo=BBO)
    s = build_scorecard(_spec(live, ds), runner=fr)
    assert s.fills_only is True and s.discordance_clustered is None
    sim = tmp_path / "sim"
    log_from([order_events("o1", "buy", PX, 10, T0, fills=[(T0 + S, 10)], venue="sim:hbt")], sim)
    ext = build_scorecard(_spec(live, sim={"kind": "external", "log": {"path": str(sim)}}), runner=fr)
    assert ext.n_twin_unmatched is None and ext.twin_match_rate is None  # no MBO dataset: no twin matching


# ---- score_fills 1.1 layout -----------------------------------------------------------------------------------------


def test_score_fills_full_layout(basic, tmp_path):
    live, ds, fr = basic
    build_scorecard(_spec(live, ds, tmp_path / "out"), runner=fr)
    t = pq.read_table(tmp_path / "out" / "score_fills.parquet")
    assert t.schema.names == FULL_SCHEMAS["qt.score_fills"].names
    assert t.schema.metadata[b"qt.schema"] == b"qt.score_fills"
    conform(t, "qt.score_fills")  # readers accept the layout
    got = {(r["order_key"], r["arm"]): (r["side"], r["instrument_id"]) for r in rows(t)}
    assert got[("o3", "live")] == ("sell", 1) and got[("o1", "sim")] == ("buy", 1)


# ---- zoo arm: native exclusion and amends ---------------------------------------------------------------------------


def _with_amend(live_dir, *, entry, latency_ns):
    t, _ = simple_schedule(live_dir, entry=entry, latency_ns=latency_ns)
    am = [{"shadow_id": "me", "ts_send": T0 + S, "new_qty": 3}]
    return t, make_table("qt.shadow_amend", am)


class _AmendSpy(FakeRunner):
    """Fake runner that reads the amends file handed to the zoo while it exists."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.zoo_amends: list[list[dict]] = []

    def run_zoo(self, spec):
        self.zoo_amends.append([] if spec.amends is None else rows(read_parquet(spec.amends.path)))
        return super().run_zoo(spec)


def test_zoo_arm_native_exclusion_and_amends(tmp_path):
    ds = make_dataset(tmp_path / "ds", _twin_tape(T0 + MS, T0 + 2 * S))
    live = _twin_live(tmp_path, T0 + MS, T0 + 2 * S)
    fr = _AmendSpy(bbo=BBO, schedule_fn=_with_amend, sim_fills={"me": [(T0 + 2 * S, 5, "queue_depleted")]})
    s = build_scorecard(_spec(live, ds, sim={"kind": "zoo", "model": "front_of_queue"}), runner=fr)
    zs = fr.zoo_specs[0]
    assert zs.dataset.path == str(ds)  # the original tape, not a filtered copy
    assert [x.model_dump() for x in zs.exclude_order_ids] == [
        {"instrument_id": 1, "order_id": 777, "session_date": "2026-01-01"}
    ]
    assert [(a["shadow_id"], a["ts_send"], a["new_qty"]) for a in fr.zoo_amends[0]] == [("me", T0 + S, 3)]
    assert "self_exclusion: zoo arm applies exclude_order_ids natively" in s.warnings
    assert not any("amends are not applied" in w for w in s.warnings)
    # the exact arm of a runner without native replay options still uses the filtered copy (fallback)
    assert fr.replay_datasets[0] != str(ds)
    off = FakeRunner(bbo=BBO, schedule_fn=_with_amend)
    build_scorecard(_spec(live, ds, sim={"kind": "zoo", "model": "front_of_queue"}, self_exclude=False), runner=off)
    assert off.zoo_specs[0].exclude_order_ids == [] and off.zoo_specs[0].amends is not None


class _L2Runner(FakeRunner):
    def bbo_from_l2(self, dataset: str, out: str) -> None:
        o = Path(out)
        o.mkdir(parents=True, exist_ok=True)
        bbo = [{"sequence": 0, "bid_ct": 1, "ask_ct": 1, "instrument_id": 1, **b} for b in self.bbo]
        from queuetruth.schemas.io import write_table

        write_table(make_table("qt.bbo", bbo), o / "bbo.parquet")


def test_l2_dataset_has_no_exclusion(tmp_path):
    ds = make_dataset(tmp_path / "ds", _twin_tape(T0 + MS, T0 + 2 * S))
    man = json.loads((ds / "manifest.json").read_text())
    man["kind"] = "l2"
    (ds / "manifest.json").write_text(json.dumps(man))
    live = _twin_live(tmp_path, T0 + MS, T0 + 2 * S)
    fr = _L2Runner(bbo=BBO, sim_fills={"me": [(T0 + 2 * S, 5, "queue_depleted")]})
    s = build_scorecard(_spec(live, ds, sim={"kind": "zoo", "model": "front_of_queue"}), runner=fr)
    assert fr.zoo_specs[0].exclude_order_ids == [] and fr.zoo_specs[0].dataset.path == str(ds)
    assert s.n_twin_unmatched is None and s.twin_match_rate is None
    # the relabelled MBO files are not qt.l2 tables: own-order removal fails, so the scorecard is blocked (E2)
    assert "own_orders_in_l2_depth" in s.verdict.failed_gates
    assert any("removal failed: QT-INPUT-002" in w for w in s.warnings), s.warnings


# ---- real engine ----------------------------------------------------------------------------------------------------


class _ZooSpy(DefaultRunner):
    """Real runner that keeps the zoo predictions of each run."""

    def __init__(self) -> None:
        self.preds: list[list[dict]] = []

    def run_zoo(self, spec):
        out = super().run_zoo(spec)
        self.preds.append(rows(pq.read_table(Path(spec.out) / "zoo_predictions.parquet")))
        return out


@pytest.mark.requires_core
def test_zoo_arm_real_engine_accepts_exclusion_and_amends(tmp_path):
    """The real zoo runner accepts `exclude_order_ids` and `amends` (deserialized by `_core`) without self-exclusion
    changing anything when self_exclude is off: the shadow joins behind o1 (10) and the twin (5)."""
    ds = make_dataset(tmp_path / "ds", _twin_tape(T0, T0 + 2 * S))
    live = _twin_live(tmp_path, T0 + MS, T0 + 2 * S)
    rn = _ZooSpy()
    build_scorecard(_spec(live, ds, sim={"kind": "zoo", "model": "front_of_queue"}, self_exclude=False), runner=rn)
    vis = {r["visible_level_qty_at_live"] for r in rn.preds[0]}
    assert vis == {15}


@pytest.mark.requires_core
@pytest.mark.integration
def test_zoo_arm_real_engine_excludes_twin(tmp_path):
    """Needs the zoo engine that applies `ZooSpec.exclude_order_ids` (a current build of the extension): with
    self-exclusion the twin's 5 lots leave the L2 view, so the shadow sees 10 ahead at its price instead of 15."""
    ds = make_dataset(tmp_path / "ds", _twin_tape(T0, T0 + 2 * S))
    live = _twin_live(tmp_path, T0 + MS, T0 + 2 * S)
    rn = _ZooSpy()
    s = build_scorecard(_spec(live, ds, sim={"kind": "zoo", "model": "front_of_queue"}), runner=rn)
    assert s.twin_match_rate == 1.0
    assert {r["visible_level_qty_at_live"] for r in rn.preds[0]} == {10}
