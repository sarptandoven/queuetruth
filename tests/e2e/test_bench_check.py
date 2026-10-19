"""Unit tests for the benchmark gate (benches/qtbench/stats.py, benches/check.py). No engine, no data."""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest

BENCHES = Path(__file__).resolve().parents[2] / "benches"
sys.path.insert(0, str(BENCHES))

import check  # noqa: E402
from qtbench import stats  # noqa: E402

ENV = {
    "machine": "arm64",
    "os": "macOS 15.6.1",
    "cpu_brand": "Apple M1 Pro",
    "ncpu": 8,
    "mem_bytes": 17179869184,
    "power_source": "ac",
    "low_power_mode": False,
    "env_class": "Apple M1 Pro|8c|16G|ac|lpm0",
}


def _doc(samples: dict[str, list[float]], schema: str = "qtbench.results", env: dict | None = None) -> dict:
    cases = []
    for cid, xs in samples.items():
        cases.append(
            {
                "id": cid,
                "group": "g",
                "unit": "records/s",
                "direction": "higher_is_better",
                "status": "ok",
                "samples": {"metric": xs, "metric_round": list(range(len(xs)))},
                "summary": stats.summarize(xs),
                "target": None,
            }
        )
    return {
        "schema": schema,
        "schema_version": 1,
        "suite": "t",
        "created_utc": "x",
        "env": env or dict(ENV),
        "config": {},
        "cases": cases,
        "derived": [],
    }


def _run(tmp: Path, base: dict, cur: dict, *extra: str) -> int:
    (tmp / "b.json").write_text(json.dumps(base))
    (tmp / "c.json").write_text(json.dumps(cur))
    return check.main(
        ["--baseline", str(tmp / "b.json"), "--current", str(tmp / "c.json"), "--json", str(tmp / "r.json"), *extra]
    )


def test_percentile_type7_matches_numpy_linear():
    xs = [1.0, 2.0, 4.0, 8.0, 16.0]
    assert stats.percentile(xs, 0.5) == 4.0
    assert math.isclose(stats.percentile(xs, 0.1), 1.4)
    assert math.isclose(stats.percentile(xs, 0.9), 12.8)


def test_exact_u_distribution_sums_to_binomial():
    for m, n in ((1, 1), (3, 3), (5, 5), (4, 7)):
        assert sum(stats._u_counts(m, n)) == math.comb(m + n, m)


def test_mann_whitney_complete_separation_is_min_p():
    r = stats.mann_whitney([10, 11, 12, 13, 14], [1, 2, 3, 4, 5])
    assert r["method"] == "exact"
    assert math.isclose(r["p"], 1 / 252)
    assert math.isclose(stats.min_attainable_p(5, 5), 1 / 252)


def test_mann_whitney_matches_scipy_when_available():
    sp = pytest.importorskip("scipy.stats")
    x, y = [3.1, 4.2, 5.9, 2.2, 8.8, 7.1], [1.0, 2.5, 3.3, 0.7, 4.4]
    ours = stats.mann_whitney(x, y)["p"]
    ref = sp.mannwhitneyu(x, y, alternative="greater", method="exact").pvalue
    assert math.isclose(ours, ref, rel_tol=1e-12)


def test_compare_classifies():
    base = [10.0, 10.1, 9.9, 10.05, 9.95]
    assert stats.compare(base, [7.0, 7.1, 6.9, 7.05, 6.95], "higher_is_better")["status"] == "regression"
    assert stats.compare(base, [9.0, 9.1, 8.9, 9.05, 8.95], "higher_is_better")["status"] == "ok"
    assert stats.compare(base, [14.0, 14.1, 13.9, 14.05, 13.95], "higher_is_better")["status"] == "improved"
    # 2 vs 2: min attainable p = 1/6 > 0.05 -> cannot be significant
    assert stats.compare(base[:2], [5.0, 5.1], "higher_is_better")["status"] == "insufficient"
    # wall-time metric: 30% more seconds is a regression
    assert (
        stats.compare([1.0, 1.01, 0.99, 1.0, 1.02], [1.3, 1.31, 1.29, 1.3, 1.32], "lower_is_better")["status"]
        == "regression"
    )


def test_compare_suspect_when_noisy():
    base = [10.0, 6.0, 14.0, 9.0, 11.0]
    cur = [7.0, 12.0, 5.0, 9.5, 6.0]
    r = stats.compare(base, cur, "higher_is_better")
    assert r["slowdown"] > 0.2 and r["status"] == "suspect"


def test_bootstrap_is_deterministic():
    a = stats.bootstrap_slowdown_ci([1, 2, 3, 4], [2, 3, 4, 5], b=500, seed=3)
    b = stats.bootstrap_slowdown_ci([1, 2, 3, 4], [2, 3, 4, 5], b=500, seed=3)
    assert a == b


def test_exit_codes(tmp_path):
    base = _doc({"a": [10.0, 10.1, 9.9, 10.05, 9.95]})
    assert _run(tmp_path, base, _doc({"a": [9.8, 9.9, 9.7, 10.0, 9.85]})) == check.EXIT_OK
    assert _run(tmp_path, base, _doc({"a": [7.0, 7.1, 6.9, 7.05, 6.95]})) == check.EXIT_REGRESSION
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["rows"][0]["status"] == "regression" and rep["exit_code"] == 1
    battery = dict(ENV, power_source="battery", low_power_mode=True)
    assert _run(tmp_path, base, _doc({"a": [10.0] * 5}, env=battery)) == check.EXIT_INCONCLUSIVE
    assert _run(tmp_path, base, _doc({"a": [10.0] * 5}, env=battery), "--allow-env-mismatch") == check.EXIT_OK
    errored = _doc({"a": [10.0] * 5})
    errored["cases"][0]["status"] = "error"
    assert _run(tmp_path, base, errored) == check.EXIT_ERROR
    assert _run(tmp_path, base, _doc({"b": [10.0] * 5}), "--require-all") == check.EXIT_INCONCLUSIVE
    bad = copy.deepcopy(base)
    bad["schema"] = "qt.bench"
    assert _run(tmp_path, bad, base) == check.EXIT_ERROR


def test_promote_roundtrip(tmp_path):
    cur = _doc({"a": [10.0, 10.1, 9.9]})
    (tmp_path / "c.json").write_text(json.dumps(cur))
    assert check.main(["--promote", str(tmp_path / "c.json"), "--out", str(tmp_path / "base.json")]) == 0
    base = json.loads((tmp_path / "base.json").read_text())
    assert base["schema"] == "qtbench.baseline" and base["gate"]["threshold"] == 0.2
    assert check.main(["--baseline", str(tmp_path / "base.json"), "--current", str(tmp_path / "c.json")]) == 0


def test_schemas_validate_generated_docs(tmp_path):
    js = pytest.importorskip("jsonschema")
    schema = json.loads((BENCHES / "schema/qtbench.check.schema.json").read_text())
    base = _doc({"a": [10.0, 10.1, 9.9, 10.05, 9.95]})
    _run(tmp_path, base, _doc({"a": [7.0, 7.1, 6.9, 7.05, 6.95]}))
    js.validate(json.loads((tmp_path / "r.json").read_text()), schema)
