"""The evidence scripts: README table statistics and the synthetic-regression comparison (no hftbacktest needed)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evidence"))

import readme_tables as rt
import synthetic_regression as sr

OPEN = 1_577_716_200_000_000_000
MIN = 60_000_000_000


def _engine(stop_minutes: list[float | None]) -> dict:
    days = {}
    for i, m in enumerate(stop_minutes):
        err = None if m is None else {"code": 14, "ts": OPEN + int(m * MIN), "live_order_ids": []}
        days[f"13_d{i}"] = {
            "orders": 10,
            "orders_live_before_stop": 4,
            "error": err,
            "session_open_ns": OPEN,
            "first_qt_312_zombie_fill_ts": OPEN + MIN,
            "fills_before_stop_equal_reference": True if err else None,
        }
    return {"days": days, "parity_with_qt_faithful": {"orders": 10 * len(days)}}


def test_stop_median_is_the_true_median() -> None:
    """An even count: the median is the mean of the two middle values, not the upper one."""
    st = rt.stops(_engine([7.3, 12.0, 13.0, 14.4, 14.4, 15.0, 20.0, 26.6]))
    assert st["stop_minutes_median"] == pytest.approx(14.4)
    st = rt.stops(_engine([10.0, 12.0, 13.0, 30.0]))
    assert st["stop_minutes_median"] == pytest.approx(12.5)
    assert (st["stop_minutes_min"], st["stop_minutes_max"]) == pytest.approx((10.0, 30.0))
    assert (st["days_stopped"], st["orders_live_before_stop"], st["stopped_days_equal_reference"]) == (4, 16, 4)
    assert st["stopped_days_at_or_after_first_qt_zombie"] == 4
    none = rt.stops(_engine([None, None]))
    assert none["days_stopped"] == 0 and none["stop_minutes_median"] is None


def test_short_model_ids() -> None:
    assert rt.short("hbt_power3[exch=partial;mode=faithful;n=2]") == "hbt_power3[n=2]"
    assert rt.short("hbt_log[exch=partial;mode=faithful]") == "hbt_log"


def _run(scen: str, err: int | None, fills: list[tuple[int, int, float]]) -> dict:
    return {
        "scenario": scen,
        "model_id": "m",
        "error": None if err is None else {"error_code": err},
        "fills": [{"order_id": o, "ts": t, "fill_qty": q} for o, t, q in fills],
        "order_events": [],
        "first_fill": {},
    }


def test_compare_builds_counts() -> None:
    stock = {"runs": [_run("a", None, [(1, 3, 1.0)]), _run("b", 14, [(1, 3, 1.0)]), _run("c", 14, [(1, 3, 2.0)])]}
    patched = {
        "runs": [_run("a", None, [(1, 3, 1.0)]), _run("b", None, [(1, 3, 1.0), (1, 5, 1.0)]), _run("c", None, [])]
    }
    got = sr.compare_builds(stock, patched)
    assert got == {
        "runs": 3,
        "stock_stopped": 2,
        "stock_error_codes": {"14": 2},
        "patched_stopped": 0,
        "unstopped_runs_identical": 1,
        "unstopped_runs_changed": 0,
        "stopped_runs_with_fills_a_prefix_of_patched": 1,
    }


def test_scenarios_become_npz_feeds(tmp_path: Path) -> None:
    n = sr.write_npz(sr.SCENARIOS, tmp_path)
    assert n == 83
    d = np.load(tmp_path / "H01_golden_family_30_100_80.npz")["data"]
    assert d.dtype.itemsize == 64 and d["exch_ts"][0] == 1000
    assert (tmp_path / "H01_golden_family_30_100_80.orders.json").exists()
