"""The oracle on hand-built tapes: SPEC 6.2 scenarios and one scenario family per audit category (finding 11).

Pure-oracle tests: no queuetruth bindings involved. `as_written` expectations (SPEC 5.2 as written, derived by
hand) go through the JSON-expectations route of the discrepancy report, which must attribute each disagreement to
the audit category the scenario was built for.
"""

from __future__ import annotations

import pytest
from _helpers import check_oracle_facts, report_vs_expectations, run_scenario
from oracle_scenarios import audit_scenarios, spec_scenarios
from qt_oracle.engine import OracleConfig

SPEC = spec_scenarios()
AUDIT = audit_scenarios()


@pytest.mark.parametrize("sc", SPEC, ids=[s.name for s in SPEC])
def test_oracle_spec_6_2_scenarios(sc) -> None:
    check_oracle_facts(sc, run_scenario(sc))


@pytest.mark.parametrize("sc", SPEC, ids=[s.name for s in SPEC])
def test_oracle_agrees_with_spec_as_written_on_plain_scenarios(sc, tmp_path) -> None:
    rep = report_vs_expectations(tmp_path, sc, run_scenario(sc))
    assert rep["summary"]["n_disagree"] == 0, rep["discrepancies"]


@pytest.mark.parametrize("sc", AUDIT, ids=[s.name for s in AUDIT])
def test_oracle_audit_scenarios(sc) -> None:
    check_oracle_facts(sc, run_scenario(sc))


@pytest.mark.parametrize("sc", AUDIT, ids=[s.name for s in AUDIT])
def test_discrepancy_report_names_the_audit_category(sc, tmp_path) -> None:
    rep = report_vs_expectations(tmp_path, sc, run_scenario(sc))
    if sc.category is None:
        assert rep["summary"]["n_disagree"] == 0, rep["discrepancies"]
        return
    assert rep["summary"]["n_disagree"] == len(sc.as_written)
    for d in rep["discrepancies"]:
        assert d["category"] == sc.category, (sc.name, d)


@pytest.mark.parametrize("sc", SPEC + AUDIT, ids=[s.name for s in SPEC + AUDIT])
def test_directed_mode_equals_fifo_on_fifo_tapes(sc) -> None:
    a = run_scenario(sc)
    b = run_scenario(sc, OracleConfig(match_mode="directed", exclude_order_ids=sc.exclude))
    for sid in a:
        assert a[sid]["fills"] == b[sid]["fills"], sc.name
        assert a[sid]["trajectory"] == b[sid]["trajectory"], sc.name


def test_strict_hidden_policy_does_not_guess_the_resting_side() -> None:
    sc = next(s for s in AUDIT if s.name == "S17d_hidden_inside_spread_infer_side")
    res = run_scenario(sc, OracleConfig(hidden_side_policy="strict"))
    assert res["X"]["filled_qty"] == 0
    assert any(n["detail"] == "ambiguous_side" for n in res["X"]["notes"])


def test_own_order_without_exclusion_sits_behind_its_twin() -> None:
    """Ruling A1: without self-exclusion the shadow joins behind the user's own resting order and misses the
    fills that order received (the self-twin bias)."""
    sc = next(s for s in AUDIT if s.name == "S20_own_order_excluded")
    res = run_scenario(sc, OracleConfig())
    assert res["X"]["queue_ahead_at_live"] == 130
    assert res["X"]["filled_qty"] == 0  # the own order o5 got 20, its shadow nothing


def test_reports_direction_of_bias() -> None:
    """Optimistic as-written rules: the reference fills more than the oracle."""
    import tempfile
    from pathlib import Path

    names = {
        "S8_tape_crossing_add": "reference_fills_more",
        "S18b_off_price_print_not_trade_through": "reference_fills_more",
        "S19b_iceberg_refresh_then_cancel": "reference_fills_more",
        "S17_hidden_after_queue": "reference_fills_less",
        "S18_nonprintable_ahead": "reference_fills_less",
        "S19_same_size_modify_loses_priority": "reference_fills_less",
    }
    with tempfile.TemporaryDirectory() as td:
        for sc in AUDIT:
            if sc.name in names:
                rep = report_vs_expectations(Path(td), sc, run_scenario(sc))
                direction = rep["summary"]["filled_qty_direction_by_category"][sc.category]
                assert direction == {names[sc.name]: 1}, sc.name
