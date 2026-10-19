"""The oracle on the testkit fixtures (synthetic data, no market data, no Rust bindings needed).

- synth_small (cme): the oracle must agree exactly with the fixture's independent expectations (`truth/`).
- synth_small_nasdaq: every disagreement must be attributed to an audit category (hidden prints on this tape);
  none may be unexplained or caused by the oracle's own reconstruction diverging from the tape.
- synth_small/own_orders (ruling A1): with the user's own orders excluded, each shadow reproduces its live order's
  fills exactly (zero discordance against the live log); without exclusion the shadow sits behind its twin
  (never fills earlier than the live order, never fills when it did not).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from _helpers import FIXTURES
from qt_oracle.compare import _read_parquet_rows, build_report, read_qt_results
from qt_oracle.engine import OracleConfig
from qt_oracle.runner import read_amends, read_order_ids, read_shadows, run_oracle
from qt_oracle.tape import read_qt_dataset

SYNTH = FIXTURES / "synth_small"
SYNTH_NQ = FIXTURES / "synth_small_nasdaq"
OWN = SYNTH / "own_orders"


def _need(path):
    if not path.exists():
        pytest.skip(f"fixture missing: {path}")


def _run(ds, shadows, amends=None, cfg=None):
    return run_oracle(read_qt_dataset(ds), read_shadows(shadows), read_amends(amends), cfg)


def test_oracle_matches_synth_small_cme_truth() -> None:
    _need(SYNTH / "truth" / "shadow_results.parquet")
    res = _run(SYNTH, SYNTH / "truth" / "shadows.parquet", SYNTH / "truth" / "amends.parquet")
    rep = build_report(res["results"], read_qt_results(SYNTH / "truth"), reference_kind="queuetruth")
    assert rep["summary"]["n_compared"] == 1000
    assert rep["summary"]["n_disagree"] == 0, rep["discrepancies"][:5]
    assert res["stats"]["exec_chunks"] > 10_000


def test_oracle_on_synth_small_nasdaq_attributes_every_disagreement() -> None:
    _need(SYNTH_NQ / "truth" / "shadow_results.parquet")
    res = _run(SYNTH_NQ, SYNTH_NQ / "truth" / "shadows.parquet", SYNTH_NQ / "truth" / "amends.parquet")
    rep = build_report(res["results"], read_qt_results(SYNTH_NQ / "truth"), reference_kind="queuetruth")
    s = rep["summary"]
    assert s["n_compared"] == 1000
    assert set(s["by_category"]) <= {"hidden_prints", "crosses", "off_price_executions"}, s["by_category"]
    assert s["n_disagree"] <= 0.1 * s["n_compared"]
    for d in rep["discrepancies"]:
        assert d["category"] != "unexplained" and not d["category_from_earlier_note"], d
    assert all(r["diag"]["unmatched_exec_volume"] == 0 for r in res["results"])


def _live_fills():
    rows = _read_parquet_rows(OWN / "live" / "events.parquet")
    out: dict[str, dict[int, int]] = {}
    for r in rows:
        if r["event_type"] == "fill":
            d = out.setdefault(r["order_key"], {})
            d[r["ts_exchange"]] = d.get(r["ts_exchange"], 0) + r["qty"]
    return out


def _shadow_fills(results):
    out: dict[str, dict[int, int]] = {}
    for r in results:
        d = out.setdefault(r["shadow_id"], {})
        for f in r["fills"]:
            d[f["ts_event"]] = d.get(f["ts_event"], 0) + f["fill_qty"]
    return out


def test_own_orders_excluded_reproduce_live_fills_exactly() -> None:
    _need(OWN / "schedule.parquet")
    excl = read_order_ids(OWN / "live" / "orders.parquet")
    res = _run(SYNTH, OWN / "schedule.parquet", cfg=OracleConfig(exclude_order_ids=excl))
    live = _live_fills()
    sim = _shadow_fills(res["results"])
    assert len(sim) == 200
    for key, fills in sim.items():
        assert fills == live.get(key, {}), key
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "shadow_results.parquet").symlink_to(OWN / "expected_results.parquet")
        (Path(td) / "shadow_fills.parquet").symlink_to(OWN / "expected_fills.parquet")
        ref = read_qt_results(td)
    rep = build_report(res["results"], ref, reference_kind="queuetruth")
    assert rep["summary"]["n_disagree"] == 0, rep["discrepancies"][:3]


def test_own_orders_not_excluded_show_the_self_twin_bias() -> None:
    _need(OWN / "schedule.parquet")
    res = _run(SYNTH, OWN / "schedule.parquet")
    live = _live_fills()
    sim = _shadow_fills(res["results"])
    live_only = sim_only = 0
    for key, fills in sim.items():
        lf = live.get(key, {})
        if fills and not lf:
            sim_only += 1
        if lf and (not fills or min(fills) > min(lf) or sum(fills.values()) < sum(lf.values())):
            live_only += 1
        if fills and lf:
            assert min(fills) >= min(lf), key  # never before its twin
    assert sim_only == 0
    assert live_only > 0
