"""Gate test (gate-2, `integration`): the report over real replay and zoo outputs of the synthetic fixture, with
the why traces produced by the exact engine through `queuetruth.api.why`."""

from __future__ import annotations

from pathlib import Path

import pytest

from queuetruth.report import render


@pytest.mark.integration
@pytest.mark.requires_core
def test_gate_report_over_pipeline_outputs(tmp_path: Path, repo_root: Path) -> None:
    import queuetruth as qt

    ds = repo_root / "tests" / "fixtures" / "synth_small"
    if not (ds / "manifest.json").exists():
        pytest.fail("tests/fixtures/synth_small is missing (run `make fixtures`)")
    shadows = tmp_path / "shadows.parquet"
    qt.sample_shadows({"dataset": {"path": str(ds)}, "out": str(shadows), "n": 80, "seed": 1})
    qt.replay(
        {
            "dataset": {"path": str(ds)},
            "out": str(tmp_path / "replay"),
            "shadows": {"path": str(shadows)},
            "rank_oracle": True,
        }
    )
    qt.run_zoo(
        {
            "dataset": {"path": str(ds)},
            "shadows": {"path": str(shadows)},
            "out": str(tmp_path / "zoo"),
            "models": ["front_of_queue", "hbt_power3[n=2]", "trade_through_only"],
            "bootstrap_b": 50,
        }
    )
    out = tmp_path / "report.html"
    s1 = render({"inputs": [str(tmp_path / "replay"), str(tmp_path / "zoo")], "out": str(out), "max_why": 3})
    html = out.read_text(encoding="utf-8")
    assert {"header", "data_quality", "zoo", "config"} <= set(s1.sections)
    assert "Synthetic or fixture data" in html  # the fixture's manifest resolves from the spec echo
    if "why" in s1.sections:
        # a zoo run is among the inputs: the exact trace comes with the zoo model's estimate (api.why_explain)
        assert (
            "queuetruth.api.why_explain" in html
            or "exact engine (queuetruth why)" in html
            or "Exact queue trajectory" in html
        )
    s2 = render(
        {"inputs": [str(tmp_path / "replay"), str(tmp_path / "zoo")], "out": str(tmp_path / "r2.html"), "max_why": 3}
    )
    assert s1.sha256 == s2.sha256
