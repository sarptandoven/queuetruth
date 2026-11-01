"""`prepare` and `score` on the synthetic dataset, with hftbacktest outputs whose answer is known (built from the
exact fills by `hbtkit.exact_as_hbt`), so no hftbacktest install is needed here."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest
import queuetruth as qt
from hbtkit import exact_as_hbt, rows

from queuetruth_hftbacktest import HbtScore, Prepared, QtHbtError, prepare, score
from queuetruth_hftbacktest.replay import HORIZONS_NS, order_key


def test_prepare_writes_schedule_truth_npz_map_and_script(prepared: Prepared, shadows: Path) -> None:
    p = prepared
    assert p.n_orders + sum(p.skipped.values()) == 200
    assert p.n_orders > 150
    sched = rows(p.schedule)
    assert len(sched) == p.n_orders
    assert {r["price_mode"] for r in sched} == {"abs"}
    assert all(r["order_key"] == order_key(r["shadow_id"]) for r in sched)
    assert all(r["latency_ns"] == 0 for r in sched)
    # one hftbacktest event file per instrument-day with orders: 2 instruments x 2 days here
    assert {k: [x.name for x in v] for k, v in p.npz.items()} == {
        1: ["1_2026-06-15.npz", "1_2026-06-16.npz"],
        2: ["2_2026-06-15.npz", "2_2026-06-16.npz"],
    }
    assert all(x.stat().st_size > 0 for v in p.npz.values() for x in v)
    imap = (p.out / "instrument_map.csv").read_text().splitlines()
    assert imap[0] == "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size"
    assert imap[1] == "qthbt,QTAU6,1,2026-06-15,-9,0,0.25,1"
    text = p.script.read_text()
    assert "hftbacktest 2.4.4 schedule replay" in text and f"{p.n_orders} orders" in text
    compile(text, str(p.script), "exec")
    cmd = p.command("/hbt/bin/python", snippet="asset.py")
    assert cmd[:2] == ["/hbt/bin/python", str(p.script)] and "--snippet" in cmd and cmd[-2] == "--out"


def test_absolute_schedule_has_the_same_exact_outcome_as_the_orders(prepared: Prepared) -> None:
    """The schedule hftbacktest replays (resolved prices, entry at ts_live) is the same order in the exact engine:
    the truth `score` uses equals the replay of the original orders, order by order."""
    a = {r["shadow_id"]: r for r in rows(prepared.out / "resolve" / "shadow_results.parquet")}
    b = {r["shadow_id"]: r for r in rows(prepared.exact / "shadow_results.parquet")}
    assert set(b) <= set(a)
    keys = ("price", "ts_live", "ts_first_fill", "ts_full_fill", "filled_qty", "n_fills", "queue_ahead_at_live")
    assert [{k: b[s][k] for k in keys} for s in sorted(b)] == [{k: a[s][k] for k in keys} for s in sorted(b)]


def test_prepare_leaves_out_orders_that_would_cross(synth: Path, shadows: Path, tmp_path: Path) -> None:
    t = qt.read_table(str(shadows))
    r = rows(shadows)[0]  # timestamps as int ns (no pandas needed)
    # a touch-relative order 20 ticks inside the touch would cross on arrival: the exact engine rejects it
    cross = dict(r, shadow_id="crosser", price_mode="touch", price=None, offset_ticks=20)
    both = pa.Table.from_pylist([r, cross], schema=t.schema)
    path = tmp_path / "orders.parquet"
    import pyarrow.parquet as pq

    pq.write_table(both, path)
    p = prepare(synth, path, tmp_path / "prep")
    assert p.n_orders == 1 and p.skipped == {"rejected_would_cross": 1}


def test_score_of_an_exact_hftbacktest_output_is_zero(prepared: Prepared, tmp_path: Path) -> None:
    src = exact_as_hbt(prepared, tmp_path / "hbt.jsonl")
    s = score(prepared, src, out=tmp_path / "s")
    assert isinstance(s, HbtScore) and s.n_scored == s.n_orders == prepared.n_orders
    assert [h.horizon_ns for h in s.horizons] == list(HORIZONS_NS)
    for h in s.horizons:
        assert h.bias == 0.0 and h.exact_only == h.hbt_only == 0 and h.n > 0
        assert h.exact_fill_rate == h.hbt_fill_rate
    ref = s.referee
    assert ref["pass"] is True and ref["discordant"] == {"sim_only": 0, "exact_only": 0} and ref["qty_mismatch"] == 0
    assert ref["max_abs_ttf_err_ns"] == 0 and ref["n_orders"] == prepared.n_orders
    doc = json.loads((tmp_path / "s" / "hbt_score.json").read_text())
    assert doc["schema"] == "qthbt.score" and doc["n_scored"] == prepared.n_orders
    assert (tmp_path / "s" / "referee_report.json").exists() and (tmp_path / "s" / "hbt_log").is_dir()
    assert "0 filled only in the exact queue, 0 only in hftbacktest" in s.text()


def test_score_counts_missed_fills_as_underfill(prepared: Prepared, tmp_path: Path) -> None:
    """hftbacktest missing every fill of some orders: the bias at each horizon is minus their share of the orders
    that fill within it, and the referee names them `exact_only`."""
    res = rows(prepared.exact / "shadow_results.parquet")
    filled = sorted(r["shadow_id"] for r in res if r["ts_first_fill"] is not None)
    drop = set(filled[::4])
    s = score(prepared, exact_as_hbt(prepared, tmp_path / "hbt.jsonl", drop=drop), out=tmp_path / "s")
    by = {r["shadow_id"]: r for r in res}
    for h in s.horizons:
        ids = [
            x
            for x in by
            if by[x]["ts_planned_end"] is None or by[x]["ts_planned_end"] >= by[x]["ts_live"] + h.horizon_ns
        ]
        lost = [x for x in ids if x in drop and by[x]["ts_first_fill"] - by[x]["ts_live"] <= h.horizon_ns]
        assert h.n == len(ids) and h.exact_only == len(lost) and h.hbt_only == 0
        assert h.bias == pytest.approx(-len(lost) / len(ids))
        if lost:
            assert h.ci_hi <= 0 and h.relative < 0
    assert s.referee["discordant"]["exact_only"] == len(drop)
    assert s.referee["pass"] is False


def test_late_fills_beyond_one_step_fail_the_referee_but_not_long_horizons(prepared: Prepared, tmp_path: Path) -> None:
    late = exact_as_hbt(prepared, tmp_path / "late.jsonl", delay_ns=50_000_000)  # 50 ms, five replay steps
    s = score(prepared, late, out=tmp_path / "late")
    assert s.referee["max_abs_ttf_err_ns"] == 50_000_000 and s.referee["pass"] is False
    assert s.referee["tolerance"]["max_abs_ttf_err_ns"] == prepared.step_ns
    ok = score(prepared, late, out=tmp_path / "tol", ttf_tolerance_ns=50_000_000)
    assert ok.referee["pass"] is True


def test_a_stopped_backtest_is_scored_before_its_stop(prepared: Prepared, tmp_path: Path) -> None:
    """hftbacktest #312 stops the whole backtest; only orders entered before the stop and horizons ending before it
    are scored, and the warning says so."""
    res = sorted(rows(prepared.exact / "shadow_results.parquet"), key=lambda r: r["ts_live"])
    stop = res[len(res) // 2]["ts_live"]
    s = score(prepared, exact_as_hbt(prepared, tmp_path / "hbt.jsonl", stop_at=stop), out=tmp_path / "s")
    before = [r for r in res if r["ts_live"] < stop]
    assert s.n_scored == len(before) < s.n_orders
    for h in s.horizons:
        assert h.bias in (0.0, None)  # every fill before the stop is there
        assert h.n == sum(
            1
            for r in before
            if r["ts_live"] + h.horizon_ns <= stop
            and (r["ts_planned_end"] is None or r["ts_planned_end"] >= r["ts_live"] + h.horizon_ns)
        )
    assert any("stopped with error 14" in w for w in s.warnings)
    assert "also those after the stop" in s.text()


def test_errors(prepared: Prepared, synth: Path, tmp_path: Path) -> None:
    with pytest.raises(QtHbtError) as e:
        Prepared.load(tmp_path)
    assert e.value.code == "QT-INPUT-001"
    (tmp_path / "prepared.json").write_text("{}")
    with pytest.raises(QtHbtError) as e:
        Prepared.load(tmp_path)
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtHbtError) as e:
        score(prepared, tmp_path / "missing.jsonl")
    assert e.value.code == "QT-INPUT-001"
    with pytest.raises(QtHbtError) as e:
        score(prepared, exact_as_hbt(prepared, tmp_path / "h.jsonl"), out=tmp_path / "s", horizons_ns=[0])
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtHbtError) as e:
        prepare(tmp_path, tmp_path / "x.parquet", tmp_path / "o")
    assert e.value.code == "QT-INPUT-001"


def test_window_caps_the_schedule_so_truth_and_hftbacktest_end_together(
    synth: Path, shadows: Path, tmp_path: Path
) -> None:
    """With a 60 s window the replay cancels each order 60 s after entry, and so does the schedule the exact truth
    replays: no horizon beyond the window is scored, and an exact copy of the truth still scores zero."""
    p = prepare(synth, shadows, tmp_path / "prep", window_ns=60_000_000_000)
    assert "\nWINDOW_NS = 60000000000\n" in p.script.read_text()
    sched = {r["shadow_id"]: r for r in rows(p.schedule)}
    assert all(r["ts_cancel"] <= r["ts_submit"] + 60_000_000_000 for r in sched.values())
    exact = rows(p.exact / "shadow_results.parquet")
    assert all(r["ts_planned_end"] <= r["ts_live"] + 60_000_000_000 for r in exact)
    assert max(r["ts_last_fill"] - r["ts_live"] for r in exact if r["ts_last_fill"] is not None) <= 60_000_000_000
    s = score(p, exact_as_hbt(p, tmp_path / "hbt.jsonl"), out=tmp_path / "s")
    by = {h.horizon_ns: h for h in s.horizons}
    assert by[300_000_000_000].n == 0 and by[300_000_000_000].bias is None
    assert by[60_000_000_000].n > 0 and by[60_000_000_000].bias == 0.0
    assert s.referee["pass"] is True


def test_orders_without_a_cancel_get_the_default_window(synth: Path, shadows: Path, tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    t = qt.read_table(str(shadows))
    r = [dict(x, ts_cancel=None) for x in rows(shadows)[:5]]
    path = tmp_path / "open.parquet"
    pq.write_table(pa.Table.from_pylist(r, schema=t.schema), path)
    p = prepare(synth, path, tmp_path / "prep")
    assert "\nWINDOW_NS = 300000000000\n" in p.script.read_text()
    assert all(x["ts_cancel"] == x["ts_submit"] + 300_000_000_000 for x in rows(p.schedule))
    p0 = prepare(synth, path, tmp_path / "prep0", window_ns=0)  # 0: no window, the orders stay open
    assert all(x["ts_cancel"] is None for x in rows(p0.schedule))
    with pytest.raises(QtHbtError) as e:
        prepare(synth, path, tmp_path / "bad", window_ns=-1)
    assert e.value.code == "QT-CONFIG-001"
