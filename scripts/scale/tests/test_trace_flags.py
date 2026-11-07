"""Per-symbol exactness and flag tracing in the W1 scale pipeline (`symbol_exactness.py`, `itch_ref.py`,
`trace_flags.py`, `unit.py`, `atlas.py`), on a synthetic ITCH day whose flags have known causes (`flag_itch.py`).

- the independent reference book counts every crossed event and price-priority violation and gives each its cause;
- the engine agrees with it per symbol, and the per-symbol replays add up to the unit's counters;
- a flagged unit's leaf names its flagged symbols (an exact unit's leaf is unchanged), reduce-day marks the
  symbol-days (`exact_ok`, `flagged_symbols`), combine labels or excludes them;
- `trace_flags.py` reproduces the shards, attributes and classifies, and its sidecar attributes shards written
  before units did; a sidecar for another leaf is reported, and an unattributed flagged unit flags all its symbols.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
from pathlib import Path

import common as K
import flag_itch as F
import itch_ref as R
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import symbol_exactness as SE

DATE = "01022020"


@pytest.fixture(scope="module")
def day(tmp_path_factory) -> tuple[Path, bytes]:
    p = tmp_path_factory.mktemp("flagday") / f"{DATE}.NASDAQ_ITCH50.gz"
    return p, F.write_flag_day(p)


def test_reference_scan_counts_and_classifies(day) -> None:
    sc = R.scan(day[0], symbols=F.SYMBOLS)
    by = {t.symbol: t for t in sc.symbols.values()}
    assert set(by) == set(F.SYMBOLS)
    for sym, (crossed, neg, ppv, causes) in F.EXPECTED.items():
        t = by[sym]
        assert (t.crossed, t.negative, t.ppv) == (crossed, neg, ppv), sym
        merged: dict[str, int] = {}
        for k, v in (*t.crossed_by_cause.items(), *t.ppv_by_cause.items()):
            merged[k] = merged.get(k, 0) + v
        assert merged == causes, sym
        assert sum(e["events"] for e in t.episodes) == t.crossed
    assert sc.sequence == sum(t.events for t in sc.symbols.values())
    assert sc.session["Q"] == 9 * 3600 * R.NS + 30 * 60 * R.NS
    h = by["HALTD"]
    assert [e["cause"] for e in h.ppv_events] == ["cross_execution"] * 2
    assert [e["printable"] for e in h.ppv_events] == ["N", "N"]
    assert h.ppv_events[0]["resting_price"] == "10.0200" and h.ppv_events[0]["best_same_side"] == "10.0500"
    assert h.ppv_events[1]["side"] == "S" and h.ppv_events[1]["best_same_side"] == "10.0000"
    ep = h.episodes[0]
    assert ep["cause"] == "halt_or_pause_quoting" and ep["reason"] == "T1" and ep["events"] == 3
    assert ep["start"]["type"] == "A" and ep["start"]["price"] == "10.0500"
    assert ep["end"]["type"] == "C" and ep["end"]["resting_price"] == "10.0500" and ep["max_width"] == "0.0500"
    assert [c["cross_type"] for c in h.crosses] == ["H"]
    t0, tc = 10 * F.H_ + 5 * F.M_, 10 * F.H_ + 10 * F.M_
    assert h.halt_spans == [[t0, tc + 6]]
    assert h.crossed_spans == [[t0 + 2 * F.M_, tc + 1], [tc + 2 * F.M_ + 1, tc + 2 * F.M_ + 2]]
    assert R.in_spans(t0, h.halt_spans) and not R.in_spans(tc + 6, h.halt_spans)
    assert R.in_spans(5, [[0, None]]) and not R.in_spans(5, [])
    assert [x["state"] for x in h.timeline if x["type"] == "H"] == ["T", "H", "T"]
    n = by["NEWIP"]
    assert n.directory["ipo_flag"] == "Y" and n.ipo and n.episodes[0]["reason"] == "IPOQ"
    # the IPO cross is reported after the resumption: the event in state T keeps the episode's cause
    assert [(e["events"], e["events_in_state_T"]) for e in n.episodes] == [(4, 1)]
    assert n.episodes[0]["last"]["state"] == "T" and n.episodes[0]["end"]["type"] == "C"
    assert [e["cross"]["cross_type"] for e in h.ppv_events] == ["H", "H"]
    assert [x["type"] for x in n.timeline] == ["H", "K", "H", "H"]
    assert n.halt_spans == [[4 * F.H_ + 103, 11 * F.H_ + 30 * F.M_ + 1]]  # H/IPO1 and Q/IPOQ until T
    assert by["ZTSTX"].directory["authenticity"] == "T"
    lk = by["LOCKD"]
    assert [e["negative"] for e in lk.episodes] == [0, 0, 0] and lk.ppv_events[0]["type"] == "E"
    # the JSON form keeps the evidence and caps the lists
    js = R.symbol_json(h, 1, 1)
    assert js["episodes_total"] == 2 and len(js["episodes"]) == 1 and js["ppv_events_total"] == 2
    json.dumps(sc.to_json())


def test_reference_scan_by_locate_and_partial_frame(tmp_path, day) -> None:
    sc = R.scan(day[0], locates={"HALTD": 2})
    assert [t.symbol for t in sc.symbols.values()] == ["HALTD"]
    assert sc.sequence == sc.symbols[2].events  # numbering covers the selected locates only
    assert R.scan(day[0], locates={"HALTD": 3}).symbols == {}  # a locate that is not the symbol's
    bad = tmp_path / "bad.itch50.gz"
    bad.write_bytes(gzip.compress(gzip.decompress(day[0].read_bytes())[:-3], mtime=0))
    with pytest.raises(ValueError, match="partial frame"):
        R.scan(bad, symbols=["CALM"])
    assert R.hms(None) is None and R.hms(34_200 * R.NS + 5) == "09:30:00.000000005" and R.px4(100_500) == "10.0500"


def test_flags_and_sum_check() -> None:
    ex = {
        "stats": {"records": 10, "events": 5, "trades": 1},
        "invariants": dict.fromkeys(SE.INV8, 0) | {"crossed_books_after_last": 2},
        "executions_checked": 3,
        "executions_unknown_order": 0,
        "price_priority_violations": 1,
        "fifo_rank_histogram": [3, 0, 0, 0, 0, 0],
        "parity": {"trades_compared": 1, "trades_mismatches": 0, "trades_extra_derived": 1},
    }
    assert SE.flags_of(ex) == {"crossed_books_after_last": 2, "price_priority_violations": 1, "trades_extra_derived": 1}
    assert SE.is_flagged(ex) and not SE.is_flagged(None) and SE.flags_of({}) == {}
    half = json.loads(json.dumps(ex))
    half["stats"] = {"records": 5, "events": 2, "trades": 1}
    other = json.loads(json.dumps(ex))
    other["stats"] = {"records": 5, "events": 3, "trades": 0}
    other["invariants"]["crossed_books_after_last"] = 0
    assert SE.sum_check(ex, {"a": half, "b": other}) == [
        "executions_checked: unit 3 != sum 6",
        "fifo_rank_histogram[0]: unit 3 != sum 6",
        "parity.trades_compared: unit 1 != sum 2",
        "parity.trades_extra_derived: unit 1 != sum 2",
        "price_priority_violations: unit 1 != sum 2",
    ]
    assert SE.sum_check(ex, {"a": ex}) == []


def _fake_ds(root: Path, iids: list[int]) -> Path:
    ds = root / "ds"
    parts = []
    for iid in iids:
        rel = f"mbo/date=2020-01-02/instrument_id={iid}/part-00000.parquet"
        (ds / rel).parent.mkdir(parents=True)
        (ds / rel).write_bytes(b"x")
        parts.append({"instrument_id": iid, "session_date": 18263, "files": [{"path": rel}], "rows": 1})
    (ds / "instruments.parquet").write_bytes(b"x")
    (ds / "manifest.json").write_text(json.dumps({"schema": "qt.dataset_manifest", "partitions": parts}))
    return ds


def test_attribute_views_and_incomplete_sums(tmp_path) -> None:
    ds = _fake_ds(tmp_path, [1, 2])
    seen = []

    def fake(view: Path, out: Path, threads: int) -> dict:
        man = json.loads((view / "manifest.json").read_text())
        (iid,) = [p["instrument_id"] for p in man["partitions"]]
        seen.append((iid, os.path.islink(view / man["partitions"][0]["files"][0]["path"].rsplit("/", 1)[0])))
        assert (view / "instruments.parquet").is_symlink() and not (view / "trades_source").exists()
        crossed = 4 if iid == 2 else 0
        return {"stats": {"events": 7}, "invariants": {"crossed_books_after_last": crossed}, "parity": {}}

    unit = {"stats": {"events": 14}, "invariants": {"crossed_books_after_last": 4}, "parity": {}}
    a = SE.attribute(ds, unit, {1: "AAA", 2: "BBB"}, work=tmp_path, replay=fake, full=True)
    assert seen == [(1, True), (2, True)]
    assert (
        a["complete"]
        and a["symbols"] == {"BBB": {"crossed_books_after_last": 4}}
        and set(a["counts"]) == {"AAA", "BBB"}
    )
    unit["invariants"]["crossed_books_after_last"] = 5  # the parts do not add up: incomplete, reported
    b = SE.attribute(ds, unit, {1: "AAA", 2: "BBB"}, work=tmp_path, replay=fake)
    assert not b["complete"] and b["sum_mismatches"] == ["invariants.crossed_books_after_last: unit 5 != sum 4"]
    assert "counts" not in b and not [p for p in tmp_path.iterdir() if p.name.startswith("qt-attr-")]
    dup = _fake_ds(tmp_path / "dup", [1, 2])
    with pytest.raises(ValueError, match="more than one partition"):
        SE.attribute(dup, unit, {1: "AAA", 2: "AAA"}, work=tmp_path, replay=fake)
    # one partition: the unit's counts are the symbol's, nothing is replayed again
    one = _fake_ds(tmp_path / "one", [2])
    seen.clear()
    c = SE.attribute(one, unit, {2: "BBB"}, work=tmp_path, replay=fake, full=True)
    assert seen == [] and c["complete"] and c["symbols"] == {"BBB": {"crossed_books_after_last": 5}}
    assert c["counts"] == {"BBB": unit}
    assert SE.failed_attribution(OSError("/tmp/x: gone")) == {
        "method": "per_partition_replay",
        "complete": False,
        "sum_mismatches": [],
        "symbols": {},
        "error": "OSError",
    }


def test_primary_cause_and_summary() -> None:
    import trace_flags as TF

    assert TF.primary_cause({}) is None
    assert TF.primary_cause({"regular_session": 3, "halt_or_pause_quoting": 3}) == "halt_or_pause_quoting"
    assert TF.primary_cause({"cross_execution": 1, "ipo_quoting_period": 9}) == "ipo_quoting_period"
    md = TF.summary_md(
        [
            {
                "unit_id": "u1",
                "symbols": {
                    "X": {
                        "engine_flags": {"crossed_books_after_last": 2},
                        "primary_cause": "test_symbol",
                        "causes": {"test_symbol": 2},
                        "engine_matches_reference": True,
                    }
                },
            }
        ]
    )
    assert "| u1 | X | 2 | 0 | 0 | test_symbol | test_symbol 2 | yes |" in md


@pytest.mark.requires_core
def test_engine_matches_reference_per_symbol(tmp_path, day) -> None:
    import trace_flags as TF

    import queuetruth as qt

    ds = tmp_path / "ds"
    qt.ingest(
        {"format": "itch", "inputs": [{"path": str(day[0])}], "out": str(ds), "symbols": F.SYMBOLS, **K.INGEST_DEFAULTS}
    )
    whole = SE.replay_counts(ds, tmp_path / "r")
    assert SE.flags_of(whole) == {"crossed_books_after_last": 12, "negative_spreads": 9, "price_priority_violations": 3}
    a = SE.attribute(ds, whole, {i + 1: s for i, s in enumerate(F.SYMBOLS)}, work=tmp_path, full=True)
    counts = a.pop("counts")
    assert a["complete"] and a["sum_mismatches"] == []
    want = {}
    for s, (c, n, p, _) in F.EXPECTED.items():
        f = {"crossed_books_after_last": c, "negative_spreads": n, "price_priority_violations": p}
        if any(f.values()):
            want[s] = {k: v for k, v in f.items() if v}
    assert a["symbols"] == want
    sc = R.scan(day[0], symbols=F.SYMBOLS)
    assert TF.compare(counts, sc) == {}
    # a disagreement is reported per symbol and counter
    counts["CALM"]["invariants"]["crossed_books_after_last"] = 1
    assert TF.compare(counts, sc) == {"CALM": ["invariants.crossed_books_after_last: engine 1 != reference 0"]}
    counts["CALM"]["invariants"]["crossed_books_after_last"] = 0
    counts["CALM"]["executions_unknown_order"] = 2  # the reference yields no record for an unknown order
    assert TF.compare(counts, sc) == {"CALM": ["executions_unknown_order: engine 2 != reference 0"]}
    assert (
        TF._find({"a": [{"b": 1}, {"source_specific": {"itch_unknown_order_ref": 3}}]}, "itch_unknown_order_ref") == 3
    )
    del counts["CALM"]
    assert TF.compare(counts, sc)["CALM"] == ["missing from the engine"]
    # a symbol without records has no partition and agrees (2020-01-30 CTEST.O: a directory entry only)
    calm = next(t for t in sc.symbols.values() if t.symbol == "CALM")
    calm.events = 0
    assert "CALM" not in TF.compare(counts, sc)


def _strip_attribution(shard_dir: Path) -> None:
    """Turns a shard into one written before units attributed their flags (valid leaf, no `exactness_by_symbol`)."""
    p = shard_dir / "shard_manifest.json"
    m = json.loads(p.read_text())
    m["leaf"].pop("exactness_by_symbol", None)
    m["leaf_hash"] = K.leaf_hash(m["leaf"])
    p.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")


@pytest.mark.requires_core
def test_pipeline_attributes_labels_excludes_and_traces(tmp_path, day, monkeypatch, capsys) -> None:
    import atlas
    import itch_split
    import plan as P
    import trace_flags as TF
    import unit as U
    from unit import run_unit

    a = argparse.Namespace(
        date=DATE,
        url=f"file://{day[0]}",
        buckets=5,  # buckets: CALM alone (an exact unit), HALTD alone, NEWIP + LOCKD + PAUSD, ZTSTX (excluded)
        out=tmp_path / "split",
        symbols=None,
        allow_missing=False,
        level=1,
        min_free_gib=0.0,
    )
    assert itch_split.run_split(a) == 0
    split = tmp_path / "split" / DATE
    sm = json.loads((split / "split_manifest.json").read_text())
    pause = [F.PAUSE[0], F.PAUSE[1], "P", "LUDP"]
    assert set(sm["trading_state"]["non_t_spans"]) == {"HALTD", "NEWIP", "PAUSD"}
    assert sm["trading_state"]["non_t_spans"]["PAUSD"] == [pause] and sm["trading_state"]["symbols_with_h"] == 6
    pl = tmp_path / "plan.json"
    args = [
        "--split", str(split), "--out", str(pl), "--quota-rule", "fixed", "--fixed-n", "20",
        "--populations", "standard", "--modes", "faithful", "--unit-core-s", "1e9",
    ]  # fmt: skip
    P.main(args)
    plan = json.loads(pl.read_text())
    # the test issue is known from its Stock Directory entry and left out of the plan
    assert sm["routing"]["test_issues"] == ["ZTSTX"]
    assert {"date": DATE, "symbol": "ZTSTX", "reason": "test issue (Stock Directory authenticity T)"} in plan[
        "exclusions"
    ]
    assert "ZTSTX" not in {u["symbol"] for u in plan["universe"]}
    # the trading state travels in the universe; the exclusions enter the run id
    src = plan["sources"][0]
    assert src["trading_state_recorded"] is True and len(src["exclusions_sha256"]) == 64
    assert {u["symbol"]: u.get("non_t_spans") for u in plan["universe"]}["PAUSD"] == [pause]
    P.main([*args[:3], str(tmp_path / "plan_x.json"), *args[4:], "--exclude", "CALM"])
    assert json.loads((tmp_path / "plan_x.json").read_text())["run_id"] != plan["run_id"]
    assert [sorted(u["symbols"]) for u in plan["units"]] == [["CALM"], ["LOCKD", "NEWIP", "PAUSD"], ["HALTD"]]

    shards = tmp_path / "shards"
    flagged_units, exact_units = {}, []
    for u in plan["units"]:
        m = run_unit(u, split, shards / u["unit_id"], 1)
        ex = m["leaf"]["exactness"]
        if SE.is_flagged(ex):
            att = m["leaf"]["exactness_by_symbol"]
            assert att["complete"] and att["method"] == "per_partition_replay"
            flagged_units[u["unit_id"]] = att["symbols"]
        else:
            assert "exactness_by_symbol" not in m["leaf"]
            exact_units.append((u, m["leaf_hash"]))
        assert m["leaf_hash"] == K.leaf_hash(m["leaf"])
    named = {s: f for v in flagged_units.values() for s, f in v.items()}
    assert set(named) == {"HALTD", "NEWIP", "LOCKD"}
    assert named["HALTD"] == {"crossed_books_after_last": 4, "negative_spreads": 4, "price_priority_violations": 2}
    # an exact unit never enters the attribution: its leaf is the one computed without that code path
    assert [u["symbols"] for u, _ in exact_units] == [["CALM"]]

    def no_attribution(*_a, **_k):
        raise AssertionError("attribution ran for an exact unit")

    with monkeypatch.context() as mp:
        mp.setattr(SE, "attribute", no_attribution)
        mp.setattr(SE, "is_flagged", lambda ex: False)
        u0, h0 = exact_units[0]
        assert run_unit(u0, split, tmp_path / "exact_again", 1)["leaf_hash"] == h0
        # a flagged unit whose attribution raises still completes; its leaf says the attribution is incomplete
        mp.setattr(SE, "is_flagged", lambda ex: True)
        uh = next(u for u in plan["units"] if u["symbols"] == ["HALTD"])
        mh = U.run_unit(uh, split, tmp_path / "attr_fails", 1)
        assert mh["leaf"]["exactness_by_symbol"] == {
            "method": "per_partition_replay", "complete": False, "sum_mismatches": [], "symbols": {},
            "error": "AssertionError",
        }  # fmt: skip
    assert "per-symbol attribution failed" in capsys.readouterr().err

    day_dir = tmp_path / "day"
    r = atlas.reduce_day(plan, shards, day_dir)
    assert r["flagged_symbols"] == 4 and r["replay_flagged_symbols"] == 3 and r["symbols_not_trading"] == 3
    assert r["units_valid"] == len(plan["units"]) and r["trading_state_known"]
    dm = json.loads((day_dir / "day_manifest.json").read_text())
    fs = dm["flagged_symbols"]
    assert {s: v["attribution"] for s, v in fs.items()} == {**dict.fromkeys(named, "leaf"), "PAUSD": None}
    assert fs["HALTD"]["flags"] == named["HALTD"] and dm["symbol_flags_problems"] == []
    # PAUSD: clean replay checks, but paused in the session (finding F2): not exact
    assert fs["PAUSD"] == {
        "unit_id": plan["units"][1]["unit_id"], "replay_ok": True, "trading_ok": False, "flags": {},
        "attribution": None, "primary_cause": None, "causes": {}, "non_t_spans": [pause], "planned_shadows": 20,
    }  # fmt: skip
    assert fs["LOCKD"]["trading_ok"] and not fs["LOCKD"]["replay_ok"] and not fs["HALTD"]["trading_ok"]
    ts = dm["trading_state"]
    assert ts["known"] and ts["source"] == "plan" and ts["symbols_not_trading"] == 3
    assert ts["symbols_not_trading_replay_ok"] == 1 and ts["session_ns"] == [34_200 * R.NS, 57_600 * R.NS]
    assert all(fu["flagged_symbols"] for fu in dm["flagged_units"])
    cells = pq.read_table(day_dir / "cells_all.parquet").to_pylist()
    assert {c["symbol"] for c in cells if c["exact_ok"]} == {"CALM"}
    assert all(c["exact_ok"] == (c["replay_ok"] and c["trading_ok"]) for c in cells)
    assert {c["symbol"] for c in cells if c["replay_ok"] and not c["trading_ok"]} == {"PAUSD"}
    flagged_with_cells = {c["symbol"] for c in cells if not c["exact_ok"]}
    assert flagged_with_cells == {"HALTD", "NEWIP", "LOCKD", "PAUSD"}, "the flagged symbols need shadows"
    strata = pq.read_table(day_dir / "strata_by_tier.parquet")
    assert set(strata.column("exact_ok").to_pylist()) == {True, False}

    lab = atlas.combine([day_dir], tmp_path / "atlas_label", bootstrap_b=50)
    assert lab["flag_policy"] == "label" and lab["flagged_symbol_days"] == 4
    per = pq.read_table(tmp_path / "atlas_label" / "per_symbol.parquet").to_pylist()
    lab_rows = {r["symbol"]: r for r in per}
    assert lab_rows["CALM"]["n_flagged_days"] == 0 and lab_rows["CALM"]["flags"] is None
    for s in flagged_with_cells:
        assert lab_rows[s]["n_flagged_days"] == 1 and lab_rows[s]["flags"].startswith(f"{DATE}: ")
    assert lab_rows["HALTD"]["flags"] == (
        f"{DATE}: crossed_books_after_last=4, negative_spreads=4, price_priority_violations=2, "
        "not trading 10:05:00-10:10:00 (H T1)"
    )
    assert lab_rows["PAUSD"]["flags"] == f"{DATE}: not trading 13:30:00-13:35:00 (P LUDP)"
    lb_lab = pq.read_table(tmp_path / "atlas_label" / "leaderboard_by_tier.parquet").to_pylist()
    for row in lb_lab:
        assert row["flagged_n"] <= row["n"] and row["flagged_share"] == row["flagged_n"] / row["n"]
    alls = [r for r in lb_lab if r["tier"] == "ALL"]
    assert {r["n_flagged_symbol_days"] for r in alls} == {4} and all(0 < r["flagged_share"] < 1 for r in alls)
    maps_lab = pq.read_table(tmp_path / "atlas_label" / "maps_by_tier.parquet").to_pylist()
    assert any(m["flagged_n"] for m in maps_lab) and all(m["flagged_n"] <= m["n"] for m in maps_lab)
    am = json.loads((tmp_path / "atlas_label" / "atlas_manifest.json").read_text())
    assert {f["symbol"] for f in am["flagged_symbol_days"]} == {*named, "PAUSD"} and am["flag_policy"] == "label"
    assert am["days_without_symbol_exactness"] == [] and am["days_without_trading_state"] == []
    exc = atlas.combine([day_dir], tmp_path / "atlas_ex", bootstrap_b=50, flag_policy="exclude")
    assert exc["flag_policy"] == "exclude"
    assert {r["symbol"] for r in pq.read_table(tmp_path / "atlas_ex" / "per_symbol.parquet").to_pylist()} == {"CALM"}
    lb = pq.read_table(tmp_path / "atlas_ex" / "leaderboard_by_tier.parquet").to_pylist()
    assert {r["n_symbols"] for r in lb if r["tier"] == "ALL"} == {1}
    assert {(r["n_flagged_symbol_days"], r["flagged_n"]) for r in lb} == {(0, 0)}
    maps_ex = pq.read_table(tmp_path / "atlas_ex" / "maps_by_tier.parquet").to_pylist()
    assert sum(m["n"] for m in maps_ex) < sum(m["n"] for m in maps_lab)
    with pytest.raises(ValueError, match="flag_policy"):
        atlas.combine([day_dir], tmp_path / "x", flag_policy="drop")
    # a day whose every symbol-day with cells is flagged leaves nothing to combine under exclude
    allbad = tmp_path / "day_allbad"
    shutil.copytree(day_dir, allbad)
    t = pq.read_table(allbad / "cells_all.parquet")
    pq.write_table(t.set_column(t.column_names.index("exact_ok"), "exact_ok", pa.array([False] * t.num_rows)),
                   allbad / "cells_all.parquet")  # fmt: skip
    with pytest.raises(ValueError, match="nothing left"):
        atlas.combine([allbad], tmp_path / "y", flag_policy="exclude")

    # a plan made before plans carried the trading state: unknown, so exclude refuses the day; the day's split
    # manifest supplies it (checked against the plan's day and file)
    old_plan = json.loads(json.dumps(plan))
    old_plan["sources"][0]["trading_state_recorded"] = False
    for u in old_plan["universe"]:
        u.pop("non_t_spans", None)
    atlas.reduce_day(old_plan, shards, tmp_path / "day_nots")
    dm_nots = json.loads((tmp_path / "day_nots" / "day_manifest.json").read_text())
    assert not dm_nots["trading_state"]["known"] and set(dm_nots["flagged_symbols"]) == set(named)
    with pytest.raises(ValueError, match="trading state"):
        atlas.combine([tmp_path / "day_nots"], tmp_path / "z", flag_policy="exclude")
    atlas.reduce_day(old_plan, shards, tmp_path / "day_sm", split_manifest=sm)
    dm_sm = json.loads((tmp_path / "day_sm" / "day_manifest.json").read_text())
    assert dm_sm["trading_state"]["source"] == "split_manifest" and dm_sm["flagged_symbols"] == fs
    other = {**sm, "date": "01032020"}
    atlas.reduce_day(old_plan, shards, tmp_path / "day_other", split_manifest=other)
    ts_other = json.loads((tmp_path / "day_other" / "day_manifest.json").read_text())["trading_state"]
    assert not ts_other["known"] and "not the plan's day" in ts_other["problem"]

    # a day directory from before exact_ok: exclude refuses it unless allowed, then drops its flagged units' symbols
    legacy = tmp_path / "day_legacy"
    shutil.copytree(day_dir, legacy)
    for name in ("cells_all", "strata_by_tier"):
        t = pq.read_table(legacy / f"{name}.parquet")
        t = t.drop_columns([c for c in ("exact_ok", "replay_ok", "trading_ok") if c in t.column_names])
        pq.write_table(t, legacy / f"{name}.parquet")
    lm = json.loads((legacy / "day_manifest.json").read_text())
    del lm["flagged_symbols"], lm["trading_state"]
    for fu in lm["flagged_units"]:
        del fu["flags"], fu["flagged_symbols"]
    (legacy / "day_manifest.json").write_text(json.dumps(lm))
    with pytest.raises(ValueError, match="allow_legacy_days"):
        atlas.combine([legacy], tmp_path / "atlas_legacy", bootstrap_b=50, flag_policy="exclude")
    leg = atlas.combine([legacy], tmp_path / "atlas_legacy", bootstrap_b=50, flag_policy="exclude",
                        allow_legacy_days=True)  # fmt: skip
    unit_syms = {s for fu in lm["flagged_units"] for s in fu["symbols"]}
    assert unit_syms == {"HALTD", "NEWIP", "LOCKD", "PAUSD"} and leg["flagged_symbol_days"] == len(unit_syms)
    leg_m = json.loads((tmp_path / "atlas_legacy" / "atlas_manifest.json").read_text())
    assert leg_m["days_without_symbol_exactness"] == [DATE] == leg_m["days_without_trading_state"]
    assert {f["attribution"] for f in leg_m["flagged_symbol_days"]} == {"unit"}
    assert {r["symbol"] for r in pq.read_table(tmp_path / "atlas_legacy" / "per_symbol.parquet").to_pylist()} == {
        "CALM"
    }

    # shards from before per-symbol attribution: the whole flagged unit is labelled, unless a trace attributes it
    for uid in flagged_units:
        _strip_attribution(shards / uid)
    old = atlas.reduce_day(plan, shards, tmp_path / "day_old")
    dm_old = json.loads((tmp_path / "day_old" / "day_manifest.json").read_text())
    assert set(dm_old["flagged_symbols"]) == unit_syms and old["flagged_symbols"] == len(unit_syms)
    assert {v["attribution"] for v in dm_old["flagged_symbols"].values()} == {"unit"}
    assert dm_old["flagged_symbols"]["LOCKD"]["flags"] == {
        "unit:crossed_books_after_last": 7, "unit:negative_spreads": 4, "unit:price_priority_violations": 1,
    }  # fmt: skip
    # a root published before attribution still verifies a re-run flagged unit (its pre-attribution leaf)
    import verify_unit

    uid0 = sorted(flagged_units)[0]
    common = ["--plan", str(pl), "--unit-id", uid0, "--split", str(split)]
    assert verify_unit.main([*common, "--atlas", str(tmp_path / "day_old" / "day_manifest.json")]) == 0
    assert verify_unit.main([*common, "--atlas", str(day_dir / "day_manifest.json")]) == 0

    # the re-stream command names every symbol of the flagged units' buckets and their expected bytes
    capsys.readouterr()
    rs = ["--plan", str(pl), "--shards", str(shards), "--restream-command", str(split / "split_manifest.json")]
    assert TF.main([*rs, "--out", "NEW"]) == 0
    cmd = json.loads(capsys.readouterr().out)
    files = {plan["units"][i]["bucket"]["file"]: plan["units"][i]["bucket"]["file_sha256"] for i in (1, 2)}
    assert cmd["expected_sha256"] == files and cmd["symbols"] == 4  # HALTD; NEWIP, LOCKD, PAUSD
    assert cmd["command"].endswith("--out NEW --symbols HALTD,LOCKD,NEWIP,PAUSD")

    out = tmp_path / "trace"
    assert TF.main(["--plan", str(pl), "--shards", str(shards), "--split", str(split), "--out", str(out)]) == 0
    sf = json.loads((out / "symbol_flags.json").read_text())
    assert sf["schema"] == TF.SYMBOL_FLAGS_SCHEMA == atlas.TF_SCHEMA and set(sf["units"]) == set(flagged_units)
    assert {s: v["primary_cause"] for s, v in sf["symbols"].items()} == {
        "HALTD": "halt_or_pause_quoting",
        "NEWIP": "ipo_quoting_period",
        "LOCKD": "regular_session",
    }
    assert all(v["engine_matches_reference"] for v in sf["symbols"].values())
    tr = json.loads((out / f"{sf['symbols']['HALTD']['unit_id']}.json").read_text())
    assert tr["reproduced"] and tr["bucket"]["matches_unit_spec"] and tr["engine_vs_reference_differences"] == {}
    ex_h = tr["symbols"]["HALTD"]["shadow_exposure"]["standard"]
    assert ex_h["shadows"] == 20 and 0 <= ex_h["in_crossed_book"] <= ex_h["in_halt"] <= 20
    tr_n = json.loads((out / f"{sf['symbols']['NEWIP']['unit_id']}.json").read_text())
    ex_n = tr_n["symbols"]["NEWIP"]["shadow_exposure"]["standard"]
    assert ex_n["shadows"] == 20 and 0 < ex_n["in_halt"] < 20  # not trading until 11:30 of a 09:35-15:50 window
    for e in (ex_h, ex_n):
        assert e["first_fill_in_halt"] == sum(e["first_fill_in_halt_by_reason"].values()) <= e["filled"] <= 20
    assert "| HALTD | 4 | 4 | 2 | halt_or_pause_quoting |" in (out / "summary.md").read_text()

    atlas.reduce_day(plan, shards, tmp_path / "day_traced", sf)
    dm_tr = json.loads((tmp_path / "day_traced" / "day_manifest.json").read_text())
    assert set(dm_tr["flagged_symbols"]) == {*named, "PAUSD"} and dm_tr["symbol_flags_problems"] == []
    assert {s: v["attribution"] for s, v in dm_tr["flagged_symbols"].items()} == {
        **dict.fromkeys(named, "symbol_flags"),
        "PAUSD": None,
    }
    assert dm_tr["flagged_symbols"]["NEWIP"]["causes"] == {"ipo_quoting_period": 4}
    # a sidecar traced from another leaf is not used and is reported
    uid = sf["symbols"]["HALTD"]["unit_id"]
    bad_sf = json.loads(json.dumps(sf))
    bad_sf["units"][uid]["shard_leaf_hash"] = "0" * 64
    atlas.reduce_day(plan, shards, tmp_path / "day_bad", bad_sf)
    dm_bad = json.loads((tmp_path / "day_bad" / "day_manifest.json").read_text())
    assert dm_bad["symbol_flags_problems"] == [f"{uid}: symbol_flags traced another leaf ({'0' * 64})"]
    assert dm_bad["flagged_symbols"]["HALTD"]["attribution"] == "unit"
    assert dm_bad["flagged_symbols"]["HALTD"]["causes"] == {}
    # a sidecar of another day (or run) is not used at all
    atlas.reduce_day(plan, shards, tmp_path / "day_wrongday", {**sf, "date": "01032020"})
    dm_wd = json.loads((tmp_path / "day_wrongday" / "day_manifest.json").read_text())
    assert dm_wd["symbol_flags_problems"] == ["symbol_flags date '01032020' != '01022020': sidecar not used"]
    assert {v["attribution"] for s, v in dm_wd["flagged_symbols"].items() if s in named} == {"unit"}

    # an exact unit asked for by name traces cleanly; a unit the plan does not hold is an error
    t2 = ["--plan", str(pl), "--shards", str(shards), "--split", str(split), "--out", str(tmp_path / "t2")]
    assert TF.main([*t2, "--unit", exact_units[0][0]["unit_id"]]) == 0
    with pytest.raises(SystemExit):
        TF.main(
            ["--plan", str(pl), "--shards", str(shards), "--split", str(split), "--out", str(out), "--unit", "nope"]
        )
    # a bucket that differs from the unit spec is refused
    b = split / plan["units"][0]["bucket"]["file"]
    b.write_bytes(gzip.compress(b"", mtime=0))
    with pytest.raises(ValueError, match="sha256"):
        TF.trace_unit(plan["units"][0], {}, split, tmp_path, 1)
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.requires_core
def test_halt_shadow_fill_reproduction() -> None:
    """Finding F2 is fixed: ingest records the ITCH trading-state spans and replay censors shadows there (spec 1.2,
    `docs/design/extensions/halt-censoring.md`), so the reproduction exits 0."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "findings" / "repro_halt_shadow_fill.py"
    spec = importlib.util.spec_from_file_location("repro_halt_shadow_fill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main([])
    if rc not in (0, 1):  # a broken reproduction (setup error) must not pass as the expected F2 failure
        pytest.fail(f"repro_halt_shadow_fill.py exited {rc} (setup error)")
    assert rc == 0  # 1 if shadows S1 and S2 were filled during the halt instead of ending censored_gap


def test_atlas_symbol_exactness_sources() -> None:
    import atlas

    def shard(uid: str, flags: dict, attr: dict | None, syms: list[str]) -> dict:
        ex = {"invariants": flags, "parity": {}}
        leaf = {"exactness": ex, "inputs": {"partitions": [{"symbol": s} for s in syms]}}
        if attr is not None:
            leaf["exactness_by_symbol"] = attr
        return {"unit_id": uid, "leaf": leaf, "leaf_hash": K.leaf_hash(leaf)}

    crossed = {"crossed_books_after_last": 3}
    done = {"method": "per_partition_replay", "complete": True, "sum_mismatches": [], "symbols": {"B": crossed}}
    ok = [
        shard("u0", {}, None, ["X"]),  # exact: nothing
        shard("u1", crossed, done, ["A", "B"]),  # attributed in the leaf
        shard("u2", crossed, {**done, "complete": False, "symbols": {"D": crossed}}, ["C", "D"]),  # incomplete
        shard("u3", crossed, None, ["E", "F"]),  # no attribution: the sidecar's, if it traced this leaf
        shard("u4", crossed, None, ["G"]),  # the sidecar did not reproduce this shard
    ]
    cause = {"primary_cause": "halt_or_pause_quoting", "causes": {"halt_or_pause_quoting": 3}}
    side = {
        "units": {
            "u1": {
                "shard_leaf_hash": ok[1]["leaf_hash"],
                "reproduced": True,
                "exactness_by_symbol": {**done, "symbols": {"A": crossed}},
            },
            "u3": {
                "shard_leaf_hash": ok[3]["leaf_hash"],
                "reproduced": True,
                "exactness_by_symbol": {**done, "symbols": {"F": crossed}},
            },
            "u4": {
                "shard_leaf_hash": ok[4]["leaf_hash"],
                "reproduced": False,
                "exactness_by_symbol": {**done, "symbols": {"G": crossed}},
            },
        },
        "symbols": {
            "F": {"unit_id": "u3", **cause},
            "B": {"unit_id": "u3", **cause},  # B is u1's: a cause from another unit is not attached
        },
    }
    st, problems = atlas.symbol_exactness(ok, side)
    assert {s: v["attribution"] for s, v in st.items()} == {
        "B": "leaf", "C": "unit", "D": "unit", "F": "symbol_flags", "G": "unit"
    }  # fmt: skip
    assert (
        st["C"]["flags"] == {"unit:crossed_books_after_last": 3} and st["F"]["primary_cause"] == "halt_or_pause_quoting"
    )
    assert st["B"]["causes"] == {} and st["B"]["primary_cause"] is None
    assert problems == [
        "u1: symbol_flags attribution differs from the leaf",
        "u4: symbol_flags did not reproduce the shard",
    ]
    st0, p0 = atlas.symbol_exactness(ok)
    assert set(st0) == {"B", "C", "D", "E", "F", "G"} and p0 == []
    # a sidecar for another leaf of a unit that attributes itself: its causes are not attached
    wrong = {
        "units": {"u1": {**side["units"]["u1"], "shard_leaf_hash": "0" * 64}},
        "symbols": {"B": {"unit_id": "u1", **cause}},
    }
    st1, p1 = atlas.symbol_exactness(ok[:2], wrong)
    assert (
        st1["B"]["attribution"] == "leaf"
        and st1["B"]["causes"] == {}
        and p1 == [f"u1: symbol_flags traced another leaf ({'0' * 64})"]
    )
    # a sidecar of another day holds no entry for these units: no causes, no attribution, nothing silent
    st2, p2 = atlas.symbol_exactness(ok[:2], {"units": {}, "symbols": {"B": {"unit_id": "u1", **cause}}})
    assert st2["B"]["causes"] == {} and p2 == []
    plan = {"run_id": "r1", "sources": [{"date": "01022020"}]}
    good = {"schema": "qt.scale.symbol_flags", "run_id": "r1", "date": "01022020"}
    assert atlas.check_symbol_flags(plan, good) == [] and atlas.check_symbol_flags(plan, None) == []
    assert atlas.check_symbol_flags(plan, {**good, "run_id": "r0", "schema": "x"}) == [
        "symbol_flags schema 'x' != 'qt.scale.symbol_flags': sidecar not used",
        "symbol_flags run_id 'r0' != 'r1': sidecar not used",
    ]


def test_trading_state_window_and_labels() -> None:
    import atlas
    import itch_split

    acts = {
        "A": [(4 * F.H_, "T", ""), (10 * F.H_, "H", "T1"), (10 * F.H_ + F.M_, "T", "")],
        "B": [(4 * F.H_, "H", "IPO1"), (8 * F.H_, "T", "")],  # pre-open only: trades normally in the session
        "C": [(4 * F.H_, "T", ""), (15 * F.H_ + 59 * F.M_, "P", "LUDP")],  # paused to the end of the day
        "D": [(4 * F.H_, "T", "")],
    }
    spans = itch_split.non_t_spans(acts)
    assert spans == {
        "A": [[10 * F.H_, 10 * F.H_ + F.M_, "H", "T1"]],
        "B": [[4 * F.H_, 8 * F.H_, "H", "IPO1"]],
        "C": [[15 * F.H_ + 59 * F.M_, None, "P", "LUDP"]],
    }
    uni = [{"symbol": s, "non_t_spans": spans[s]} if s in spans else {"symbol": s} for s in "ABCD"]
    plan = {"sources": [{"date": DATE, "trading_state_recorded": True}], "universe": uni}
    info, bad = atlas.trading_state(plan)
    assert info["known"] and info["source"] == "plan" and set(bad) == {"A", "C"}
    # a half day closes at 13:00: C's pause is after the close
    plan["sources"][0]["session_hours"] = {"open_s": 34_200, "close_s": 46_800}
    assert set(atlas.trading_state(plan)[1]) == {"A"}
    assert atlas.trading_state({"sources": [{"date": DATE}], "universe": uni})[0]["known"] is False
    lab = atlas._label({"date": DATE, "flags": {}, "non_t_spans": spans["C"]})
    assert lab == f"{DATE}: not trading 15:59:00-end (P LUDP)"
