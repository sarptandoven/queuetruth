"""End-to-end pipeline on `tests/fixtures/synth_small` through the installed `queuetruth` console script (ingest ->
shadows -> replay -> zoo -> calibrate -> features -> fit-hazard -> score -> referee -> report -> export) and the same
chain through the Python API; every summary document validates against its JSON Schema (O-SCHEMA-3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from e2e_support import SYNTH, run_pipeline, schema_validate

pytestmark = [pytest.mark.integration, pytest.mark.requires_core]

N_MODELS = 17  # SPEC 5.3.4


@pytest.fixture(scope="module")
def cli_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    w = tmp_path_factory.mktemp("pipeline_cli")
    return w, run_pipeline(w, threads=1, omp=1)


def test_pipeline_cli_every_stage_succeeds_and_validates(cli_run: tuple[Path, dict[str, Any]]) -> None:
    w, docs = cli_run
    seen = {stage: schema_validate(doc) for stage, doc in docs.items()}
    assert seen["ingest"] == "qt.ingest_summary" and seen["replay"] == "qt.replay_summary"
    assert seen["zoo"] == "qt.zoo_summary" and seen["calibrate"] == "qt.calibration_result"
    assert seen["features"] == "qt.feature_summary" and seen["fit_hazard"] == "qt.hazard_model_card"
    assert seen["score"] == "qt.scorecard_summary" and seen["referee"] == "qt.referee_report"
    assert seen["report"] == "qt.report_summary" and seen["export_hbt"] == seen["export_nt"] == "qt.export_summary"
    assert seen["export_hbt_refused"] == seen["export_nt_refused"] == "qt.error"
    # every summary.json the stages wrote validates too
    for p in sorted(w.rglob("*.json")):
        doc = json.loads(p.read_text())
        if isinstance(doc, dict) and str(doc.get("schema", "")).startswith("qt."):
            schema_validate(doc)


def test_pipeline_cli_semantics(cli_run: tuple[Path, dict[str, Any]]) -> None:
    w, d = cli_run
    man = json.loads((SYNTH / "manifest.json").read_text())
    assert d["ingest"]["manifest"]["partitions"] and len(d["ingest"]["manifest"]["partitions"]) == len(
        man["partitions"]
    )
    rp = d["replay"]
    assert rp["stats"]["records"] == sum(p["rows"] for p in man["partitions"])
    assert all(v == 0 for v in rp["invariants"].values()), rp["invariants"]
    assert rp["rank_oracle"]["price_priority_violations"] == 0 and rp["parity"]["pass"] is True
    assert rp["shadows"]["n"] == d["shadows"]["n"] > 0
    models = {row["model_id"] for row in d["zoo"]["leaderboard"]}
    assert len(models) >= N_MODELS, sorted(models)
    assert d["calibrate"]["held_out"] and d["calibrate"]["exports"]["hftbacktest"]
    assert d["features"]["n_orders"] > 0
    assert d["score"]["verdict"]["gates_passed"] is True and d["score"]["n_orders"] > 0
    assert d["referee"]["pass"] is True
    assert (w / "report" / "report.html").stat().st_size > 1000
    for target in ("hbt", "nt"):
        refused = d[f"export_{target}_refused"]
        assert refused["code"] == "QT-CALIB-001", refused
    assert not (w / "export" / "hbt_refused.py").exists() and not (w / "export" / "nautilus_refused.py").exists()
    hbt = (w / "export" / "hbt.py").read_text()
    nt = (w / "export" / "nautilus.py").read_text()
    assert "hftbacktest" in hbt.lower() and "NOT VALIDATED" in hbt
    assert "NOT VALIDATED" in nt


def test_pipeline_python_api(tmp_path: Path) -> None:
    import queuetruth as qt

    ds = tmp_path / "ds"
    ing = qt.ingest({"format": "parquet", "inputs": [{"path": str(SYNTH)}], "venue": "cme", "out": str(ds)})
    D = {"path": str(ds)}
    sh = tmp_path / "shadows.parquet"
    qt.sample_shadows({"dataset": D, "out": str(sh), "n": 120, "seed": 4, "lifetime_ns": 60_000_000_000})
    S = {"path": str(sh)}
    rp = qt.replay({"dataset": D, "out": str(tmp_path / "replay"), "shadows": S, "rank_oracle": True})
    zoo = qt.run_zoo(
        {
            "dataset": D,
            "shadows": S,
            "out": str(tmp_path / "zoo"),
            "models": ["front_of_queue", "hbt_power3[n=2]", "hbt_l3_fifo"],
            "bootstrap_b": 20,
            "h_max_ns": 60_000_000_000,
        }
    )
    cal = qt.calibrate(
        {
            "kind": "alloc",
            "family": "power3",
            "dataset": D,
            "shadows": S,
            "out": str(tmp_path / "cal"),
            "train": {"dates": ["2026-06-15"]},
            "test": {"dates": ["2026-06-16"]},
            "bootstrap": {"b": 10},
        }
    )
    feats = qt.extract_features({"dataset": D, "shadows": S, "out": str(tmp_path / "feat")})
    card = qt.fit_hazard(
        {
            "features": {"path": str(next((tmp_path / "feat").glob("*.parquet")))},
            "out": str(tmp_path / "model"),
            "gbdt": {"max_iter": 20, "min_samples_leaf": 5},
            "bootstrap_b": 20,
        }
    )
    sc = qt.score(
        {
            "live": {"path": str(SYNTH / "own_orders" / "live")},
            "sim": {"kind": "exact"},
            "dataset": D,
            "out": str(tmp_path / "score"),
            "bootstrap": {"b": 50},
            "perm_b": 50,
        }
    )
    rep = qt.report(
        {"inputs": [str(tmp_path / x) for x in ("zoo", "cal", "score")], "out": str(tmp_path / "r.html"), "max_why": 2}
    )
    from queuetruth.errors import QtError

    cal_ref = {"target": "hftbacktest", "calibration": {"path": str(tmp_path / "cal")}}
    try:
        ex = qt.export(cal_ref)
    except QtError as e:  # a fit that does not beat the default out of sample is refused, then forced
        assert e.code == "QT-CALIB-001", e
        ex = qt.export(cal_ref, force=True)
        assert "NOT VALIDATED" in (ex.text or "")
    for doc in (ing, rp, zoo, cal, feats, card, sc, rep, ex):
        schema_validate(doc.model_dump(mode="json", by_alias=True))
    assert all(v == 0 for v in rp.invariants.model_dump().values())
    assert sc.verdict.gates_passed is True and len(zoo.leaderboard) >= 3
