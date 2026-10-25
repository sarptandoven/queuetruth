"""`reconcile.py` logic on small hand-made inputs: orders, sidecars, label rules, parity and realism tables."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import reconcile as rc

from queuetruth.errors import QtError

T0 = 1_577_716_200_000_000_000  # 2019-12-30 14:30 UTC
S = 1_000_000_000
DATE = dt.date(2019, 12, 30)


def _ts(values: list[int | None]) -> pa.Array:
    return pa.array(values, pa.int64()).cast(pa.timestamp("ns", tz="UTC"))


def _write_inputs(tmp: Path) -> tuple[Path, Path, dict]:
    exact = tmp / "exact"
    zoo = tmp / "zoo"
    exact.mkdir()
    zoo.mkdir()
    ids = ["s1", "s2", "s3", "s4", "s5"]
    pq.write_table(
        pa.table(
            {
                "shadow_id": ids,
                "instrument_id": pa.array([13, 13, 13, 13, 13], pa.uint32()),
                "side": pa.array([66, 65, 66, 66, 66], pa.uint8()),
                "price": pa.array([300_120_000_000, 300_140_000_000, None, 300_100_000_000, 300_110_000_000]),
                "qty": pa.array([100, 1, 5, 7, 3], pa.uint64()),
                "ts_live": _ts([T0 + 10 * S, T0 + 5 * S, T0 + 1 * S, T0 + 2 * S, T0 + 3 * S]),
                "end_state": ["filled", "cancelled", "open_at_end", "rejected_would_cross", "filled"],
                "ts_first_fill": _ts([T0 + 10 * S + S // 2, None, None, None, T0 + 3 * S + 20 * S]),
                "ts_full_fill": _ts([T0 + 12 * S, None, None, None, T0 + 3 * S + 20 * S]),
                "filled_qty": pa.array([100, 0, 0, 0, 3], pa.uint64()),
            }
        ),
        exact / "shadow_results.parquet",
    )
    close = T0 + 6 * 3600 * S
    pq.write_table(
        pa.table(
            {
                "shadow_id": ["s1", "s2", "s3", "s4", "s5"],
                "ts_cancel_planned": _ts([T0 + 310 * S, T0 + 7 * S, None, T0 + 302 * S, None]),
                "ts_admin_end": _ts([None, None, None, None, T0 + 13 * S]),
                "instrument_id": pa.array([13] * 5, pa.uint32()),
                "session_date": pa.array([DATE] * 5, pa.date32()),
                "ts_live": _ts([T0 + 10 * S, T0 + 5 * S, T0 + S, T0 + 2 * S, T0 + 3 * S]),
                "session_close": _ts([close] * 5),
            }
        ),
        zoo / "zoo_shadows.parquet",
    )
    inst = {
        (13, "2019-12-30"): {
            "price_exp": -9,
            "size_exp": 0,
            "tick_size": 10_000_000,
            "lot_size": 1,
            "midnight_utc_ns": T0 - 34_200 * S,
            "open_s": 34_200,
        },
    }
    return exact, zoo, inst


def test_build_orders_and_sidecar(tmp_path: Path) -> None:
    exact, zoo, inst = _write_inputs(tmp_path)
    plan = rc.build_orders(exact, zoo, inst)
    assert plan["skipped"] == {"no_price": 1, "rejected_would_cross": 1}
    assert plan["session_open_ns"] == {"13_2019-12-30": T0}
    (orders,) = plan["per_day"].values()
    assert list(plan["per_day"]) == ["13_2019-12-30"]
    # sorted by ts_live: s5 (3 s), s2 (5 s), s1 (10 s); order ids 1..N in that order
    assert [(o.order_id, o.shadow_id, o.side) for o in orders] == [(1, "s5", "B"), (2, "s2", "A"), (3, "s1", "B")]
    assert orders[2].px == pytest.approx(300.12)
    assert orders[2].qty == 100.0
    assert orders[2].ts_end == T0 + 310 * S
    assert orders[0].ts_end == T0 + 6 * 3600 * S  # no planned cancel: the session close
    side = rc.sidecar(orders, inst[(13, "2019-12-30")])
    assert side["tick_size"] == pytest.approx(0.01)
    assert side["lot_size"] == 1.0
    assert side["shadow_ids"] == {"1": "s5", "2": "s2", "3": "s1"}
    kinds = [(a["ts"], a["kind"], a["order_id"]) for a in side["actions"]]
    assert kinds == sorted(kinds, key=lambda k: (k[0], 0 if k[1] == "cancel" else 1, k[2]))
    assert {a["tif"] for a in side["actions"] if a["kind"] == "submit"} == {"GTX"}
    assert sum(a["kind"] == "cancel" for a in side["actions"]) == 3


def test_build_orders_needs_an_instruments_row(tmp_path: Path) -> None:
    exact, zoo, _ = _write_inputs(tmp_path)
    with pytest.raises(QtError, match="QT-DATA-001"):
        rc.build_orders(exact, zoo, {})


def test_fill_key_aggregates_per_timestamp() -> None:
    assert rc.fill_key([(5, 1.0), (3, 2.0), (5, 0.5)]) == ((3, 2.0), (5, 1.5))
    assert rc.fill_key([]) == ()
    assert rc.fill_key_all([{"order_id": 2, "ts": 9, "qty": 1.0}, {"order_id": 1, "ts": 4, "qty": 2.0}], 9) == (
        (1, 4, 2.0),
    )


def test_label_defined_uses_planned_and_admin_end() -> None:
    o = rc.Order(1, "s", 13, "2019-12-30", "B", 1.0, 1.0, ts_live=100, ts_end=200)
    assert rc.label_defined(o, {"ts_admin_end": None}, 100)
    assert not rc.label_defined(o, {"ts_admin_end": None}, 101)
    assert not rc.label_defined(o, {"ts_admin_end": 150}, 60)
    assert rc.label_defined(o, {"ts_admin_end": 150}, 50)
    open_ended = rc.Order(1, "s", 13, "2019-12-30", "B", 1.0, 1.0, ts_live=100, ts_end=None)
    assert rc.label_defined(open_ended, {"ts_admin_end": None}, 10**12)


def test_realism_two_by_two_and_bias() -> None:
    orders = [rc.Order(i + 1, f"s{i}", 13, "2019-12-30", "B", 1.0, 1.0, T0 + i * S, None) for i in range(6)]
    zoo = {o.shadow_id: {"ts_admin_end": None} for o in orders}
    # exact fills s0..s3 within 1 s; the model fills s0, s1 and s4 within 1 s, s2 after 2 s
    exact = {"s0": T0 + S // 2, "s1": T0 + S + S // 2, "s2": T0 + 2 * S + 1, "s3": T0 + 3 * S + 1}
    model = {"s0": T0 + 1, "s1": T0 + S + 1, "s2": T0 + 4 * S + 1, "s4": T0 + 4 * S + 7}
    out = rc.realism(orders, zoo, exact, model, (S,), seed=0)
    c = out[str(S)]
    assert (c["n"], c["both"], c["exact_only"], c["model_only"], c["neither"]) == (6, 2, 2, 1, 1)
    assert c["exact_fill_rate"] == pytest.approx(4 / 6)
    assert c["model_fill_rate"] == pytest.approx(3 / 6)
    assert c["bias"] == pytest.approx(-1 / 6)
    assert c["ci_lo"] <= c["bias"] <= c["ci_hi"]
    assert 0.0 < c["p_mcnemar_exact"] <= 1.0
    empty = rc.realism([], {}, {}, {}, (S,), seed=0)[str(S)]
    assert empty["n"] == 0 and empty["exact_fill_rate"] is None


def _hbt_run(fills: list[tuple[int, int, float]], error: dict | None = None, qty: dict | None = None) -> dict:
    """Driver output for fills (order_id, ts, qty); `qty` = order_id -> order size (default ids 1, 2, 3: 3, 1, 100)."""
    size = qty or {1: 3.0, 2: 1.0, 3: 100.0}
    done: dict[int, float] = defaultdict(float)
    out = []
    for o, t, q in fills:
        done[o] += q
        out.append({"order_id": o, "ts": t, "qty": q, "leaves_after": size[o] - done[o]})
    return {"fills": out, "error": error, "wall_s": 0.0}


def test_compare_parity_stops_and_reference(tmp_path: Path) -> None:
    exact, zoo, inst = _write_inputs(tmp_path)
    plan = rc.build_orders(exact, zoo, inst)
    mid = "hbt_power3[exch=partial;mode=faithful;n=2]"
    # queuetruth faithful: s1 fills 60 then 40 (a #312 zombie fill), s5 fills once
    pq.write_table(
        pa.table(
            {
                "model_id": [mid, mid, mid],
                "shadow_id": ["s1", "s1", "s5"],
                "ts_event": _ts([T0 + 11 * S, T0 + 12 * S, T0 + 4 * S]),
                "fill_qty": [60.0, 40.0, 3.0],
                "quirk": ["none", "hbt_312", "none"],
            }
        ),
        zoo / "zoo_fills.parquet",
    )
    key = "13_2019-12-30"
    patched = _hbt_run([(3, T0 + 11 * S, 60.0), (3, T0 + 12 * S, 40.0), (1, T0 + 4 * S, 3.0)])
    stock = _hbt_run([(1, T0 + 4 * S, 3.0), (3, T0 + 11 * S, 60.0)], {"code": 14, "ts": T0 + 12 * S, "live": []})
    runs = {"patched": {key: {mid: patched}}, "stock": {key: {mid: stock}}}
    rows: list[dict] = []
    res = rc.compare(plan, zoo, exact, runs, [mid], (S, 10 * S), "patched", seed=0, per_order=rows)
    eng = res["models"][mid]["engines"]
    assert eng["patched"]["parity_with_qt_faithful"] == {
        "orders": 3,
        "orders_compared": 3,
        "orders_with_fills": 2,  # s2 never fills on either side and is not counted as a "same first fill"
        "identical_fills": 3,
        "same_first_fill": 2,
        "differ": [],
    }
    day = eng["stock"]["days"][key]
    assert day["error"]["code"] == 14
    assert day["first_qt_312_zombie_fill_ts"] == T0 + 12 * S
    assert day["qt_312_zombies"] == 1
    assert day["fills_before_stop_equal_reference"] is True
    # the stopped engine (stop 12 s) is compared on the orders live before it, on their fills before it
    assert eng["stock"]["parity_with_qt_faithful"]["identical_fills"] == 3
    assert day["orders_live_before_stop"] == 3
    assert "realism" not in eng["stock"]
    # full-fill realism and the first-fill delta: s1 exact first 10.5 s, hbt 11 s; s5 exact 23 s, hbt 4 s
    ff = eng["patched"]["first_fill_delta"]
    assert (ff["n_both"], ff["n_neither"], ff["n_model_earlier"], ff["n_model_later"]) == (2, 1, 1, 1)
    assert ff["quantiles_ns"]["p50"] == pytest.approx(((S // 2) + (-19 * S)) / 2)
    full1 = eng["patched"]["realism_full_fill"][str(S)]
    assert (full1["both"], full1["model_only"]) == (0, 1)  # s1 completes at 12 s (2 s after live); s5 at 4 s
    assert set(eng["patched"]["realism_by_date"]) == {"2019-12-30"}
    # per-order rows: one per model, build and order
    assert len(rows) == 6
    r = next(x for x in rows if x["engine"] == "patched" and x["shadow_id"] == "s1")
    assert (r["hbt_ts_first_fill"], r["hbt_ts_full_fill"], r["hbt_filled_qty"]) == (T0 + 11 * S, T0 + 12 * S, 100.0)
    assert (r["exact_ts_full_fill"], r["exact_filled_qty"], r["d_filled_qty"]) == (T0 + 12 * S, 100.0, 0.0)
    assert r["d_first_fill_ns"] == S // 2
    rs = next(x for x in rows if x["engine"] == "stock" and x["shadow_id"] == "s1")
    assert (rs["engine_stop_ts"], rs["hbt_filled_qty"], rs["hbt_ts_full_fill"]) == (T0 + 12 * S, 60.0, None)
    # realism of the full run at 1 s: s1 fills 1 s after live (exact 0.5 s): both; s5 fills 1 s after live (exact
    # 20 s): model only; s2 never fills
    r1 = eng["patched"]["realism"][str(S)]
    assert (r1["n"], r1["both"], r1["exact_only"], r1["model_only"], r1["neither"]) == (3, 1, 0, 1, 1)
    r10 = eng["patched"]["realism"][str(10 * S)]
    assert r10["n"] == 2  # s2 (planned end 2 s after live) is not labelled at 10 s
    md = rc.markdown({"dataset": "d", "n_orders": 3, **res})
    assert "| `hbt_power3[exch=partial;mode=faithful;n=2]` | patched | 3 | 3 | 2 | 3 | 2 | 0 of 1 |" in md
    # the stop table states minutes after the open, the first zombie fill and the reference check
    assert (
        "| `hbt_power3[exch=partial;mode=faithful;n=2]` | stock | 13_2019-12-30 | 14 | 0.2 | 0.2 | 3 of 3 | yes |" in md
    )


def test_stopped_build_counts_only_orders_live_before_its_stop(tmp_path: Path) -> None:
    """An order submitted after the stop has no fills on either side before it: it must not count as identical."""
    exact, zoo, inst = _write_inputs(tmp_path)
    plan = rc.build_orders(exact, zoo, inst)
    mid = "hbt_log[exch=partial;mode=faithful]"
    pq.write_table(
        pa.table(
            {
                "model_id": [mid, mid],
                "shadow_id": ["s5", "s1"],
                "ts_event": _ts([T0 + 4 * S, T0 + 11 * S]),
                "fill_qty": [3.0, 100.0],
                "quirk": ["none", "none"],
            }
        ),
        zoo / "zoo_fills.parquet",
    )
    key = "13_2019-12-30"
    # stops at 6 s: s5 (live 3 s) and s2 (live 5 s) were live before it, s1 (live 10 s) was not
    stock = _hbt_run([(1, T0 + 4 * S, 3.0)], {"code": 14, "ts": T0 + 6 * S, "live_order_ids": [2]})
    res = rc.compare(plan, zoo, exact, {"stock": {key: {mid: stock}}}, [mid], (S,), None, seed=0)
    par = res["models"][mid]["engines"]["stock"]["parity_with_qt_faithful"]
    assert (par["orders"], par["orders_compared"], par["orders_with_fills"]) == (3, 2, 1)
    assert (par["identical_fills"], par["same_first_fill"]) == (2, 1)
    assert res["models"][mid]["engines"]["stock"]["days"][key]["orders_live_before_stop"] == 2
    md = rc.markdown({"dataset": "d", "n_orders": 3, **res})
    assert "No build ran to the end: realism not computed" in md


def test_quantities_are_compared_in_real_units(tmp_path: Path) -> None:
    """size_exp = -3: the zoo's fill_qty is in raw units (lots of 0.001), hftbacktest's in real units."""
    exact, zoo, inst = _write_inputs(tmp_path)
    inst[(13, "2019-12-30")] |= {"size_exp": -3}
    plan = rc.build_orders(exact, zoo, inst)
    (orders,) = plan["per_day"].values()
    assert orders[2].qty == pytest.approx(0.1) and orders[2].lot == pytest.approx(0.001)
    assert rc.sidecar(orders, inst[(13, "2019-12-30")])["lot_size"] == pytest.approx(0.001)
    mid = "hbt_log[exch=partial;mode=faithful]"
    pq.write_table(
        pa.table(
            {
                "model_id": [mid, mid],
                "shadow_id": ["s1", "s1"],
                "ts_event": _ts([T0 + 11 * S, T0 + 12 * S]),
                "fill_qty": [7.0, 93.0],
                "quirk": ["none", "none"],
            }
        ),
        zoo / "zoo_fills.parquet",
    )
    run = _hbt_run([(3, T0 + 11 * S, 0.007), (3, T0 + 12 * S, 0.093)], qty={1: 0.003, 2: 0.001, 3: 0.1})
    rows: list[dict] = []
    res = rc.compare(plan, zoo, exact, {"x": {"13_2019-12-30": {mid: run}}}, [mid], (S,), None, 0, rows)
    par = res["models"][mid]["engines"]["x"]["parity_with_qt_faithful"]
    assert par["identical_fills"] == 3 and par["differ"] == []
    r = next(x for x in rows if x["shadow_id"] == "s1")
    assert r["hbt_ts_full_fill"] == T0 + 12 * S  # leaves 0.1 - 0.007 - 0.093 round to 0 lots of 0.001
    assert r["exact_filled_qty"] == pytest.approx(0.1)


def test_zero_life_orders_are_skipped(tmp_path: Path) -> None:
    exact, zoo, inst = _write_inputs(tmp_path)
    t = pq.read_table(zoo / "zoo_shadows.parquet").to_pydict()
    t["ts_cancel_planned"][1] = t["ts_live"][1]  # s2: planned cancel at its activation
    pq.write_table(pa.table(t, schema=pq.read_schema(zoo / "zoo_shadows.parquet")), zoo / "zoo_shadows.parquet")
    plan = rc.build_orders(exact, zoo, inst)
    assert plan["skipped"]["zero_life"] == 1
    assert [o.shadow_id for o in plan["per_day"]["13_2019-12-30"]] == ["s5", "s1"]


def test_compare_reports_differences(tmp_path: Path) -> None:
    exact, zoo, inst = _write_inputs(tmp_path)
    plan = rc.build_orders(exact, zoo, inst)
    mid = "hbt_log[exch=partial;mode=faithful]"
    pq.write_table(
        pa.table(
            {
                "model_id": [mid],
                "shadow_id": ["s1"],
                "ts_event": _ts([T0 + 11 * S]),
                "fill_qty": [100.0],
                "quirk": ["none"],
            }
        ),
        zoo / "zoo_fills.parquet",
    )
    runs = {"x": {"13_2019-12-30": {mid: _hbt_run([(3, T0 + 11 * S, 60.0)])}}}
    p = rc.compare(plan, zoo, exact, runs, [mid], (S,), None, seed=0)["models"][mid]["engines"]["x"]
    par = p["parity_with_qt_faithful"]
    assert (par["orders"], par["identical_fills"], par["same_first_fill"], par["orders_with_fills"]) == (3, 2, 1, 1)
    with pytest.raises(QtError, match="QT-USAGE-001"):
        rc.compare(plan, zoo, exact, runs, [mid, "hbt_log2[exch=partial;mode=faithful]"], (S,), None, seed=0)
    assert par["differ"] == [{"shadow_id": "s1", "hbt": ((T0 + 11 * S, 60.0),), "qt": ((T0 + 11 * S, 100.0),)}]


def test_parse_horizons() -> None:
    assert rc.parse_horizons(None) == rc.DEFAULT_HORIZONS_NS
    assert rc.parse_horizons("1s,100ms,5m,250") == (250, 100_000_000, 1_000_000_000, 300_000_000_000)
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        rc.parse_horizons("0s")


def test_main_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import hbt_config as hc

    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        hc.dumps(
            hc.document(
                [hc.make_entry("log", "partial", [], "fixed"), hc.make_entry("log2", "no_partial", [], "fixed")],
                "0.1.1",
            )
        )
    )
    base = ["--data", str(tmp_path), "--shadows", str(tmp_path / "s.parquet"), "--out", str(tmp_path / "o")]
    assert rc.main([*base, "--config", str(cfg), "--hbt", "x=/nonexistent/python"]) == 2
    assert "one exchange per reconciliation run" in capsys.readouterr().err
    one = ["--models", "hbt_log[exch=partial;mode=faithful]"]
    assert rc.main([*base, "--config", str(cfg), *one, "--hbt", "x=/nonexistent/python"]) == 2
    assert "QT-USAGE-001" in capsys.readouterr().err
    assert rc.main([*base, "--config", str(cfg), *one]) == 2
    assert "at least one --hbt" in capsys.readouterr().err
    py = str(Path(np.__file__))  # any existing file stands in for the interpreter in this check
    assert rc.main([*base, "--config", str(cfg), *one, "--hbt", f"a={py}", "--reference", "b"]) == 2
    assert "--reference b" in capsys.readouterr().err


def test_first_abort_lines_and_check(tmp_path: Path) -> None:
    w = [
        "hbt_312_abort: hbt_log[exch=partial;mode=faithful]: 411 of 6000 shadow pair(s) aborted at the #312 "
        "InvalidOrderStatus event (faithful hftbacktest stops there), first at shadow s1 ts 1577716211000000000; "
        "their predictions from the abort on carry status hbt_312_abort and are left out of the metrics",
        "info: something else",
    ]
    first = rc.qt_first_aborts(w)
    assert first == {"hbt_log[exch=partial;mode=faithful]": {"n_pairs": 411, "shadow_id": "s1", "ts": T0 + 11 * S}}
    exact, zoo, inst = _write_inputs(tmp_path)
    plan = rc.build_orders(exact, zoo, inst)
    key = "13_2019-12-30"
    doc = {
        "models": {
            "hbt_log[exch=partial;mode=faithful]": {
                "engines": {
                    "a": {"days": {key: {"error": {"code": 14, "ts": T0 + 11 * S}}}},
                    "b": {"days": {key: {"error": {"code": 14, "ts": T0 + 9 * S}}}},
                    "c": {"days": {key: {"error": None}}},
                }
            }
        }
    }
    rc.check_first_aborts(doc, plan, first)
    e = doc["models"]["hbt_log[exch=partial;mode=faithful]"]["engines"]
    assert e["a"]["qt_first_abort"]["engine_stops_at_it"] is True
    assert (e["b"]["qt_first_abort"]["engine_stops_at_it"], e["b"]["qt_first_abort"]["engine_stops_no_later"]) == (
        False,
        True,
    )
    assert e["c"]["qt_first_abort"]["engine_stop_ts"] is None
    assert e["c"]["qt_first_abort"]["day"] == key


def test_child_errors_keep_their_code(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("progress\nQT-INPUT-001: /data/x: dataset not found\nmore\n")
    e = rc.child_error("queuetruth replay", 1, log)
    assert e.code == "QT-INPUT-001"
    assert "queuetruth replay: /data/x: dataset not found" in str(e)
    log.write_text("Traceback (most recent call last):\nSegmentation fault\n")
    assert rc.child_error("hbt_driver.py (a, k)", -11, log).code == "QT-ENGINE-001"
    assert rc.child_error("x", 1, tmp_path / "missing.log").code == "QT-ENGINE-001"


def test_run_cli_failure_reports_the_cli_error(tmp_path: Path) -> None:
    with pytest.raises(QtError) as ei:
        rc.run_cli("replay", ["--quiet", "replay", "--data", str(tmp_path / "nope"), "--out", str(tmp_path / "o")],
                   tmp_path / "replay.log")  # fmt: skip
    assert ei.value.code.startswith("QT-")
    assert ei.value.code != "QT-ENGINE-001"
    assert "replay.log" in str(ei.value)


IDENT = {"hftbacktest": "2.4.4", "native_sha256": {"_hftbacktest.so": "ab" * 32}}
INPUTS = {"dataset_id": "d1", "shadows_sha256": "s1", "l2": {"depth_levels": 10, "cadence_ns": 0},
          "exchange": "partial", "models": ["hbt_log[exch=partial;mode=faithful]"]}  # fmt: skip


def test_cache_manifest_refuses_other_inputs(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.mkdir()
    man = rc.check_cache(out, INPUTS, {"stock": IDENT}, fresh=False)
    (out / "manifest.json").write_text(json.dumps(man))
    (out / "exact").mkdir()
    (out / "hbt-stock").mkdir()
    # same inputs, a new label: accepted and added
    man2 = rc.check_cache(out, INPUTS, {"patched": IDENT | {"native_sha256": {"x": "cd" * 32}}}, fresh=False)
    assert set(man2["hbt"]) == {"stock", "patched"}
    # another L2 view, or another model set, on the same --out: refused
    for change in ({"l2": {"depth_levels": 5, "cadence_ns": 0}}, {"models": ["hbt_log2[exch=partial;mode=faithful]"]}):
        with pytest.raises(QtError, match="QT-USAGE-001") as ei:
            rc.check_cache(out, INPUTS | change, {"stock": IDENT}, fresh=False)
        assert f"{next(iter(change))} differ" in str(ei.value)
    # a rebuilt venv under the same label: refused
    with pytest.raises(QtError, match="different hftbacktest build"):
        rc.check_cache(out, INPUTS, {"stock": IDENT | {"hftbacktest": "2.4.5"}}, fresh=False)
    # --fresh clears the stages and starts a new manifest
    man3 = rc.check_cache(out, INPUTS | {"l2": {"depth_levels": 5, "cadence_ns": 0}}, {"stock": IDENT}, fresh=True)
    assert not (out / "exact").exists() and not (out / "hbt-stock").exists()
    assert man3["inputs"]["l2"]["depth_levels"] == 5


def test_cache_without_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "zoo").mkdir()
    with pytest.raises(QtError, match="without a manifest"):
        rc.check_cache(tmp_path, INPUTS, {}, fresh=False)


def test_main_refuses_a_changed_l2_view(tmp_path: Path, capsys: pytest.CaptureFixture[str], hbt_stock_py: Path) -> None:
    """End to end through main(): a second run with another --l2-depth on the same --out exits 2 before any stage."""
    import hbt_config as hc

    data = tmp_path / "ds"
    data.mkdir()
    (data / "manifest.json").write_text(json.dumps({"dataset_id": "abc"}))
    shadows = tmp_path / "s.parquet"
    shadows.write_bytes(b"x")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(hc.dumps(hc.document([hc.make_entry("log", "partial", [], "fixed")], "0.1.1")))
    out = tmp_path / "o"
    base = ["--data", str(data), "--shadows", str(shadows), "--config", str(cfg), "--out", str(out),
            "--hbt", f"stock={hbt_stock_py}"]  # fmt: skip
    ident = rc.engine_identity(hbt_stock_py)
    inputs = {"dataset_id": "abc", "shadows_sha256": rc.sha256_path(shadows),
              "l2": {"depth_levels": 10, "cadence_ns": 0}, "exchange": "partial",
              "models": ["hbt_log[exch=partial;mode=faithful]"]}  # fmt: skip
    out.mkdir()
    (out / "manifest.json").write_text(json.dumps(rc.check_cache(out, inputs, {"stock": ident}, fresh=False)))
    (out / "exact").mkdir()
    assert rc.main([*base, "--l2-depth", "5"]) == 2
    err = capsys.readouterr().err
    assert "QT-USAGE-001" in err and "l2 differ" in err and "--fresh" in err
