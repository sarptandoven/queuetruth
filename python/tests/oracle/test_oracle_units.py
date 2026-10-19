"""Unit tests of the oracle's building blocks: venue facts, tape book, readers, lifecycle, comparison."""

from __future__ import annotations

import json

import pytest
from qt_oracle.compare import build_report, categorize, read_json_expectations, render_markdown
from qt_oracle.engine import OracleConfig, PartitionRunner, run_partition
from qt_oracle.runner import read_amends, read_order_ids, read_shadows, route_shadows, run_oracle
from qt_oracle.scenarios import amend, build_tape, meta_for, shadow
from qt_oracle.tape import FLAG_LAST, FLAG_MAYBE_BAD_BOOK, Rec, TapeBook, group_events, read_et_csv
from qt_oracle.venue import modify_keeps_priority, venue_facts

E0 = (10, ["A o1 B 100 60", "A o2 B 100 40", "A o9 A 101 500"])


@pytest.mark.parametrize(
    ("venue", "old", "pending", "new_px", "new", "keeps"),
    [
        ("cme", 60, 0, 100, 50, True),  # strict decrease keeps
        ("cme", 60, 0, 100, 60, False),  # same size (iceberg refresh / account change) loses (A6)
        ("cme", 60, 0, 100, 70, False),  # increase loses
        ("cme", 60, 0, 101, 50, False),  # price change loses
        ("cme", 60, 20, 100, 40, True),  # execution update after F keeps
        ("cme", 60, 20, 100, 30, True),  # execution + reduction keeps
        ("cme", 60, 20, 100, 50, False),  # above the post-fill remaining: an increase, loses
        ("cme", 60, 60, 100, 60, False),  # iceberg slice refreshed after full execution loses
        ("generic", 60, 0, 100, 60, True),
        ("generic", 60, 0, 100, 61, False),
    ],
)
def test_modify_priority_truth_table(venue, old, pending, new_px, new, keeps) -> None:
    assert modify_keeps_priority(venue_facts(venue), 100, old, pending, new_px, new) is keeps


def test_venue_aliases_and_cross_windows() -> None:
    f = venue_facts("XNAS.ITCH", open_s=34_200, close_s=57_600)
    assert f.name == "nasdaq" and f.hidden_orders and not f.modify_present
    assert f.cross_windows_s == ((34_200.0, 34_201.0), (57_600.0, 57_601.0))
    assert venue_facts("glbx").name == "cme"
    with pytest.raises(ValueError):
        venue_facts("nyse")


def test_tape_book_priority_and_queries() -> None:
    tb = TapeBook(venue_facts("cme"))
    tb.add(1, "B", 100, 60)
    tb.add(2, "B", 100, 40)
    tb.add(3, "A", 101, 5)
    assert tb.best("B") == 100 and tb.best("A") == 101 and not tb.is_crossed()
    tb.modify(1, "B", 100, 60, keeps=tb.keeps_priority(1, 100, 60, 0))  # same size: to the back
    assert list(tb.levels["B"][100]) == [2, 1]
    tb.modify(2, "B", 100, 10, keeps=tb.keeps_priority(2, 100, 10, 0))
    assert list(tb.levels["B"][100]) == [2, 1] and tb.level_total("B", 100) == 70
    assert tb.crosses("B", 101) and not tb.crosses("B", 100)
    assert tb.displayed_at_or_better("B", 99) == 70 and tb.displayed_at_or_better("A", 100) == 0
    tb.cancel(2, 99)
    assert list(tb.levels["B"][100]) == [1]
    tb.clear()
    assert tb.best("B") is None


def test_group_events_closes_unterminated_tail() -> None:
    recs = [Rec(1, 1, "A", "B", 1, 1, 1, FLAG_LAST, 1), Rec(2, 1, "A", "B", 1, 1, 2, 0, 2)]
    assert [len(e) for e in group_events(recs)] == [1, 1]


def test_amend_to_filled_and_session_end_states() -> None:
    ev = [
        E0,
        (20, ["A o3 B 100 30"]),
        (30, ["T A 100 105", "F o1 60", "C o1 60", "F o2 40", "C o2 40", "F o3 5", "C o3 5"]),
    ]
    recs = build_tape(ev)
    res = run_partition(meta_for(close_s=0), recs, [shadow("a", qty=10)], {"a": [amend("a", 35, 5)]})["results"][0]
    assert res["filled_qty"] == 5 and res["end_state"] == "censored_session_end"  # amend after the last event
    res = run_partition(
        meta_for(), recs + build_tape([(50, ["A o8 B 90 1"])]), [shadow("a", qty=10)], {"a": [amend("a", 35, 5)]}
    )["results"][0]
    assert res["end_state"] == "amended_to_filled"


def test_cancel_at_or_before_ts_live_never_goes_live() -> None:
    recs = build_tape([E0, (20, ["A o3 B 100 30"])])
    r = run_partition(meta_for(), recs, [shadow(ts_cancel=15)])["results"][0]
    assert r["end_state"] == "cancelled" and r["ts_activated_event"] is None
    r = run_partition(meta_for(), recs, [shadow(ts_cancel=16)])["results"][0]
    assert r["end_state"] == "cancelled" and r["ts_activated_event"] == 20 and r["queue_ahead_at_live"] == 100


def test_maybe_bad_book_censors() -> None:
    recs = build_tape([E0, (20, ["A o3 B 100 30"]), (30, ["A o4 B 99 1"])])
    recs[-1].flags |= FLAG_MAYBE_BAD_BOOK
    r = run_partition(meta_for(), recs, [shadow()])["results"][0]
    assert r["end_state"] == "censored_gap" and r["ts_end"] == 30


def test_readers_and_routing(tmp_path) -> None:
    csv_path = tmp_path / "mbo.csv"
    rows = ["ts_event,ts_recv,instrument_id,action,side,price,size,order_id,flags,sequence,channel_id"]
    for i, (a, s, p, q, o) in enumerate([("A", "B", 100, 60, 1), ("A", "A", 101, 5, 9)], 1):
        rows.append(f"{10 + i},{10 + i},7,{a},{s},{p},{q},{o},128,{i},0")
    rows += ["30,30,7,T,A,100,10,0,0,3,0", "30,30,7,F,B,100,10,1,0,3,0", "30,30,7,C,B,100,10,1,128,3,0"]
    csv_path.write_text("\n".join(rows) + "\n")
    parts = read_et_csv(csv_path, venue="nasdaq", midnight_utc_ns=0, open_s=34_200, close_s=57_600, tick_size=1)
    assert len(parts) == 1 and len(parts[0].records) == 5
    sh_csv = tmp_path / "shadows.csv"
    sh_csv.write_text(
        "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel\n"
        "x,7,B,100,10,20,0,\ny,8,A,101,1,20,0,25\n"
    )
    shadows = read_shadows(sh_csv)
    assert shadows[0].side == "B" and shadows[1].ts_cancel == 25
    routed, orphans = route_shadows(parts, shadows)
    assert [s.shadow_id for s in orphans] == ["y"] and len(routed[0]) == 1
    res = run_oracle(parts, shadows)
    by = {r["shadow_id"]: r for r in res["results"]}
    assert by["x"]["queue_ahead_at_live"] == 60 and by["x"]["filled_qty"] == 0
    assert [t["queue_ahead_after"] for t in by["x"]["trajectory"]] == [60, 50, 50]
    assert by["y"]["end_state"] == "censored_gap"
    js = tmp_path / "shadows.json"
    js.write_text(
        json.dumps(
            [
                {
                    "shadow_id": "z",
                    "instrument_id": 7,
                    "side": "A",
                    "price_mode": "touch",
                    "offset_ticks": -1,
                    "qty": 3,
                    "ts_submit": 5,
                }
            ]
        )
    )
    z = read_shadows(js)[0]
    assert z.price is None and z.offset_ticks == -1
    am = tmp_path / "amends.csv"
    am.write_text("shadow_id,ts_send,new_qty\nx,25,4\n")
    assert read_amends(am)["x"][0].new_qty == 4
    ids = tmp_path / "ids.txt"
    ids.write_text("5\n6\n")
    assert read_order_ids(ids) == frozenset({5, 6})


def test_categorize_prefers_notes_at_the_divergence() -> None:
    notes = [
        {"ts_event": 10, "category": "crosses", "detail": "x"},
        {"ts_event": 20, "category": "hidden_prints", "detail": "y"},
        {"ts_event": 20, "category": "cme_modify", "detail": "z"},
    ]
    assert categorize(notes, 20) == ("cme_modify", ["cme_modify", "hidden_prints"], False)
    assert categorize(notes, 15) == ("crosses", ["crosses"], True)
    assert categorize(notes, 5)[0] == "unexplained"


def test_report_json_expectations_and_markdown(tmp_path) -> None:
    recs = build_tape([E0, (20, ["A o3 B 100 30"]), (30, ["T A 100 60", "F o1 60", "C o1 60"])])
    res = run_partition(meta_for(), recs, [shadow()])["results"]
    p = tmp_path / "exp.json"
    p.write_text(json.dumps({"shadows": {"X": {"filled_qty": 5, "ts_first_fill": 30}}}))
    rep = build_report(res, read_json_expectations(p), reference_kind="expectations")
    assert rep["summary"]["n_disagree"] == 1 and rep["summary"]["by_category"] == {"unexplained": 1}
    md = render_markdown(rep)
    assert "By category" in md and "`X`" in md


def test_queue_ahead_never_increases_on_fifo_tapes() -> None:
    from oracle_scenarios import audit_scenarios, spec_scenarios

    for sc in spec_scenarios() + audit_scenarios():
        runner = PartitionRunner(sc.meta(), sc.shadows, sc.amends, OracleConfig(exclude_order_ids=sc.exclude))
        for r in runner.run(sc.records()):
            qa = [t["queue_ahead_after"] for t in r["trajectory"]]
            assert qa == sorted(qa, reverse=True), sc.name
            assert sum(f["fill_qty"] for f in r["fills"]) == r["filled_qty"] <= r["qty"]
            cums = [f["cum_qty"] for f in r["fills"]]
            assert cums == sorted(set(cums))


def test_twin_linking_is_per_shadow_and_can_be_switched_off() -> None:
    """Ruling A1: with twin linking (default) an excluded order stays on the tape for every shadow but its own;
    without it the order is removed for everyone (the counterfactual before per-shadow twins)."""
    from oracle_scenarios import audit_scenarios

    by_name = {s.name: s for s in audit_scenarios()}

    def run(name: str, link: bool):
        sc = by_name[name]
        runner = PartitionRunner(
            sc.meta(), sc.shadows, sc.amends, OracleConfig(exclude_order_ids=sc.exclude, twin_linking=link)
        )
        return {r["shadow_id"]: r for r in runner.run(sc.records())}, runner.stats

    res, stats = run("S20f_another_twin_stays_in_the_queue", True)
    assert stats["twins_linked"] == 2 and stats["excluded_records_dropped"] == 0
    assert res["B"]["filled_qty"] == 0 and res["B"]["queue_ahead_at_live"] == 10
    res, stats = run("S20f_another_twin_stays_in_the_queue", False)
    assert stats["twins_linked"] == 0 and stats["excluded_records_dropped"] > 0
    assert res["B"]["filled_qty"] == 10 and res["B"]["queue_ahead_at_live"] == 0  # A's twin removed for B too
    # The off-display execution is the twin's shadow's fill only when it stands for the twin.
    res, _ = run("S20b_twin_executed_off_display", False)
    assert res["X"]["filled_qty"] == 0
    # An excluded order no shadow enters at is removed for everyone.
    sc = by_name["S20b_twin_executed_off_display"]
    runner = PartitionRunner(
        sc.meta(), [shadow(side="A", price=101, qty=30, ts_submit=25)], {}, OracleConfig(exclude_order_ids=sc.exclude)
    )
    (r,) = runner.run(sc.records())
    assert runner.stats["twins_linked"] == 0 and r["queue_ahead_at_live"] == 50 and r["filled_qty"] == 0
