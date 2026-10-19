"""`scripts/oracle/run_oracle.py` end to end (pure: fixture data and hand-built datasets, no Rust bindings)."""

from __future__ import annotations

import importlib.util
import json

import pytest
from _helpers import FIXTURES, REPO, expectations_file
from oracle_scenarios import audit_scenarios
from qt_oracle.scenarios import write_qt_dataset, write_shadow_spec


def _main():
    spec = importlib.util.spec_from_file_location("run_oracle", REPO / "scripts" / "oracle" / "run_oracle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


def test_run_oracle_on_synth_small_subset(tmp_path) -> None:
    synth = FIXTURES / "synth_small"
    if not (synth / "truth" / "shadows.parquet").exists():
        pytest.skip("synth_small fixture missing")
    rc = _main()(
        [
            "--dataset",
            str(synth),
            "--shadows",
            str(synth / "truth" / "shadows.parquet"),
            "--amends",
            str(synth / "truth" / "amends.parquet"),
            "--qt-results",
            str(synth / "truth"),
            "--instrument-ids",
            "1",
            "--limit-shadows",
            "60",
            "--out",
            str(tmp_path),
        ]
    )
    assert rc == 0
    lines = (tmp_path / "oracle_results.jsonl").read_text().splitlines()
    assert len(lines) == 60
    rep = json.loads((tmp_path / "discrepancy_report.json").read_text())
    assert rep["summary"]["n_compared"] == 60 and rep["summary"]["n_disagree"] == 0
    assert (tmp_path / "discrepancy_report.md").exists()
    summ = json.loads((tmp_path / "oracle_summary.json").read_text())
    assert summ["n_shadows"] == 60 and summ["stats"]["records"] > 0


@pytest.mark.parametrize("name", ["S18b_off_price_print_not_trade_through", "S19_same_size_modify_loses_priority"])
def test_run_oracle_with_json_expectations(tmp_path, name) -> None:
    sc = next(s for s in audit_scenarios() if s.name == name)
    ds = write_qt_dataset(tmp_path / "ds", sc.records(), sc.meta())
    sh = write_shadow_spec(tmp_path / "shadows.parquet", sc.shadows)
    exp = expectations_file(tmp_path, sc)
    rc = _main()(
        ["--dataset", str(ds), "--shadows", str(sh), "--expectations", str(exp), "--out", str(tmp_path / "out")]
    )
    assert rc == 0
    rep = json.loads((tmp_path / "out" / "discrepancy_report.json").read_text())
    assert rep["summary"]["by_category"] == {sc.category: 1}
