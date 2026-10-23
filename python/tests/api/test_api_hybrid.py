"""Hybrid operations end to end on the synthetic fixtures (gate-2 tests, marker `integration`): zoo (Rust run +
queuetruth.eval leaderboard with the planned-life columns), calibrate (Rust fit + held-out zoo + exports), latency
calibration, calibrate-live, score, referee, features -> fit-hazard, report and export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute
import pyarrow.parquet as pq
import pytest

import queuetruth as qt
from queuetruth import api
from queuetruth.errors import QtError
from queuetruth.schemas import models as m
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import write_table

pytestmark = [pytest.mark.requires_core, pytest.mark.integration]


@pytest.fixture(scope="module")
def synth(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures" / "synth_small"


@pytest.fixture(scope="module")
def work(synth: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    w = tmp_path_factory.mktemp("hybrid")
    qt.sample_shadows(
        {
            "dataset": {"path": str(synth)},
            "out": str(w / "shadows.parquet"),
            "n": 160,
            "seed": 8,
            "lifetime_ns": 60_000_000_000,
        }
    )
    return w


def test_run_zoo_fills_leaderboard_with_planned_life(synth: Path, work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import queuetruth.eval as qeval

    seen: dict[str, Any] = {}
    real = qeval.evaluate_zoo

    def spy(*args: Any, **kw: Any) -> Any:
        seen.update(kw)
        return real(*args, **kw)

    monkeypatch.setattr(qeval, "evaluate_zoo", spy)
    out = work / "zoo"
    models = ["front_of_queue", "hbt_power3[n=2]", "trade_through_only"]
    s = qt.run_zoo(
        {
            "dataset": {"path": str(synth)},
            "shadows": {"path": str(work / "shadows.parquet")},
            "out": str(out),
            "models": models,
            "h_max_ns": 60_000_000_000,
            "bootstrap_b": 50,
        }
    )
    assert isinstance(s, m.ZooSummary) and len(s.leaderboard) == 3
    # faithful hbt #312 (qt ends only the pair where hbt ends the run, E8): only the affected pairs stop, from the
    # abort on;
    # the model keeps its leaderboard entry, its aborted rows carry status hbt_312_abort and the warning counts them
    preds = pq.read_table(out / "zoo_predictions.parquet")
    aborted = preds.filter(pa.compute.is_valid(preds.column("status")))
    aborts = qeval.abort_shares(s.warnings)
    assert set(aborted.column("model_id").to_pylist()) == set(aborts)
    for mid, (n, total) in aborts.items():
        sids = set(aborted.filter(pa.compute.equal(aborted.column("model_id"), mid)).column("shadow_id").to_pylist())
        assert (len(sids), total) == (n, s.n_shadows)
        assert "aborted at the hbt #312 event" in qeval.bias_sentence(
            next(e for e in s.leaderboard if e.model_id == mid), 10_000_000_000, aborts
        )
    assert seen["planned_life"] is not None and seen["planned_life"].column_names == [
        "shadow_id",
        "ts_cancel_planned",
        "ts_admin_end",
    ]
    assert {e.model_id for e in s.leaderboard} == set(s.models)
    assert any(f.name == "zoo_errors.parquet" for f in s.files)
    assert pq.read_schema(out / "zoo_errors.parquet").metadata[b"qt.schema"] == b"qt.zoo_errors"
    disk = m.ZooSummary.model_validate(json.loads((out / "summary.json").read_text()))
    assert disk == s
    by = {e.model_id: e for e in s.leaderboard}
    foq = next(k for k in by if k.startswith("front_of_queue"))
    tto = next(k for k in by if k.startswith("trade_through_only"))
    h = "10000000000"
    assert by[foq].fill_rate_bias[h].value >= by[tto].fill_rate_bias[h].value
    # 0.1.1: one common population. A shadow any model aborted on is left out of every model, so every model's
    # metrics have the same n and the same exact fill rate, and each bias is model rate minus exact rate.
    assert s.n_excluded_hbt_312 == len(set(aborted.column("shadow_id").to_pylist()))
    assert s.n_leaderboard is not None and 0 < s.n_leaderboard <= s.n_shadows - s.n_excluded_hbt_312
    errs = pq.read_table(out / "zoo_errors.parquet").to_pylist()
    ns = {
        r["model_id"]: r["n"]
        for r in errs
        if r["metric"] == "fill_rate_bias" and r["stratum_kind"] == "all" and str(r["horizon_ns"]) == h
    }
    assert len(ns) == 3 and len(set(ns.values())) == 1, ns
    exact = {e.exact_fill_rate[h] for e in s.leaderboard}
    assert len(exact) == 1 and None not in exact
    for e in s.leaderboard:
        assert e.fill_rate_bias[h].value == pytest.approx(e.model_fill_rate[h] - e.exact_fill_rate[h], abs=1e-12)


def test_calibrate_alloc_held_out_and_exports(synth: Path, work: Path) -> None:
    out = work / "cal"
    c = qt.calibrate(
        {
            "kind": "alloc",
            "family": "power3",
            "dataset": {"path": str(synth)},
            "shadows": {"path": str(work / "shadows.parquet")},
            "out": str(out),
            "train": {"dates": ["2026-06-15"]},
            "test": {"dates": ["2026-06-16"]},
            "bootstrap": {"b": 20},
        }
    )
    assert c.kind == "alloc" and c.param_name == "n" and c.estimate is not None
    assert len(c.held_out) == 1 and len(c.baseline_held_out) == 1
    (fitted,) = c.held_out
    (base,) = c.baseline_held_out
    assert fitted.startswith("hbt_power3[") and "n=2]" in base
    assert {str(h) for h in (100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000)} <= set(
        c.held_out[fitted].brier
    )
    assert c.exports.hftbacktest is not None and "power3" in c.exports.hftbacktest.python.lower()
    assert c.exports.nautilus is not None
    assert not any(w.startswith("rust part only") for w in c.warnings)
    disk = m.CalibrationResult.model_validate(json.loads((out / "calibration.json").read_text()))
    assert disk == c
    assert (out / "fit.json").exists() and (out / "alloc_events.parquet").exists()
    # The export's fail-safe verdict is recorded before the snippets are made (queuetruth.export.trust.annotate).
    verdicts = [w for w in c.warnings if w.startswith("trust: verdict: ")]
    assert len(verdicts) == 1 and any(w.startswith("trust: beats_default: ") for w in c.warnings)
    if c.trust is not None:  # schema 1.1 structured record agrees with the lines
        assert verdicts[0].startswith(f"trust: verdict: {c.trust.verdict}")
    snippet = work / "snippet.py"
    spec = {"target": "hftbacktest", "calibration": {"path": str(out)}, "out": str(snippet)}
    if verdicts[0] == "trust: verdict: exportable":
        ex = qt.export(spec)
        assert ex.text == c.exports.hftbacktest.python and snippet.exists() and "NOT VALIDATED" not in ex.text
        snippet.unlink()
    else:
        with pytest.raises(QtError, match="refusing to export"):
            qt.export(spec)
    # A fit that loses to the default out of sample (here: the same result with a worse held-out score) is refused
    # (QT-CALIB-001, exit code 1) and nothing is written ...
    losing = work / "cal_losing"
    _write_losing(c, losing / "calibration.json")
    spec = {**spec, "calibration": {"path": str(losing)}}
    with pytest.raises(QtError) as e:
        qt.export(spec)
    assert e.value.code == "QT-CALIB-001" and e.value.exit_code == 1
    assert "not_beating_default" in e.value.details["reasons"] and not snippet.exists()
    # ... unless forced: the snippet then carries the warning header.
    ex = api.export(spec, force=True)
    assert ex.text is not None and "NOT VALIDATED" in ex.text and snippet.read_text() == ex.text


def _write_losing(c: m.CalibrationResult, path: Path) -> None:
    """`c` with the fitted model's held-out scores made worse than the default's at every horizon."""
    (fid,) = c.held_out
    (bid,) = c.baseline_held_out
    base = c.baseline_held_out[bid]
    worse = m.HeldOut(
        brier={h: (v or 0.0) + 0.05 for h, v in base.brier.items()},
        logloss={h: (v or 0.0) + 0.1 for h, v in base.logloss.items()},
        fill_rate_bias=dict(base.fill_rate_bias),
    )
    kept = [w for w in c.warnings if not w.startswith(("trust: beats_default:", "trust: verdict:"))]
    bad = c.model_copy(update={"held_out": {fid: worse}, "warnings": kept, "trust": None})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bad.model_dump(mode="json")))


def test_calibrate_nt_touch_blocks(synth: Path, work: Path) -> None:
    c = qt.calibrate(
        {
            "kind": "nt_touch",
            "family": "nt_prob_touch",
            "dataset": {"path": str(synth)},
            "shadows": {"path": str(work / "shadows.parquet")},
            "out": str(work / "nt"),
            "train": {"blocks": {"index": [0, 1, 2], "n_blocks": 5}},
            "test": {"blocks": {"index": [3, 4], "n_blocks": 5}},
            "bootstrap": {"b": 10},
        }
    )
    assert c.param_name == "p"
    if c.estimate is not None:
        assert next(iter(c.baseline_held_out)).startswith("nt_prob_touch")


def test_calibrate_latency(synth: Path, work: Path) -> None:
    c = qt.calibrate(
        {
            "kind": "latency",
            "family": "latency",
            "live": {"path": str(synth / "own_orders" / "live")},
            "out": str(work / "lat"),
        }
    )
    assert c.kind == "latency" and c.latency is not None and c.latency.clock_sync_assumed is True
    assert set(type(c.latency).model_fields) == set(api.LATENCY_FIT_KEYS)
    assert (work / "lat" / "calibration.json").exists()
    assert c.exports.hftbacktest is not None


def test_calibrate_live(synth: Path, work: Path) -> None:
    c = api.calibrate_live(
        {
            "dataset": {"path": str(synth)},
            "live": {"path": str(synth / "own_orders" / "live")},
            "family": "power3",
            "out": str(work / "clive"),
            "train": {"dates": ["2026-06-15"]},
            "test": {"dates": ["2026-06-16"]},
            "bootstrap": {"b": 10},
        }
    )
    assert isinstance(c, m.CalibrationResult) and (work / "clive" / "calibration.json").exists()


def test_score_exact_and_gates(synth: Path, work: Path) -> None:
    base = {
        "live": {"path": str(synth / "own_orders" / "live")},
        "sim": {"kind": "exact"},
        "dataset": {"path": str(synth)},
        "bootstrap": {"b": 100},
        "perm_b": 100,
    }
    s = qt.score({**base, "out": str(work / "score")})
    assert isinstance(s, m.ScorecardSummary) and s.n_orders > 0
    assert s.verdict.gates_passed is True
    disk = m.ScorecardSummary.model_validate(json.loads((work / "score" / "summary.json").read_text()))
    assert disk.run_id == s.run_id
    g = qt.score({**base, "sim": {"kind": "zoo", "model": "front_of_queue"}, "gates": {"max_abs_fill_rate_bias": 0.0}})
    assert g.verdict.gates_passed is False and g.verdict.failed_gates


def test_referee_pass_and_fail(repo_root: Path, tmp_path: Path) -> None:
    fx = repo_root / "tests" / "fixtures" / "referee" / "02_level_shrink_caps_queue"
    base = {"dataset": {"path": str(fx / "dataset")}, "schedule": {"path": str(fx / "schedule.parquet")}}
    ok = qt.referee({**base, "sim_fills": {"path": str(fx / "expected_fills.parquet")}, "out": str(tmp_path / "ok")})
    assert ok.pass_ is True and (tmp_path / "ok" / "referee_report.json").exists()
    empty = tmp_path / "empty_fills.parquet"
    write_table(SCHEMAS["qt.shadow_fills"].empty_table(), empty)
    bad = qt.referee({**base, "sim_fills": {"path": str(empty)}})
    assert bad.pass_ is False


def test_features_fit_hazard_and_report(synth: Path, work: Path) -> None:
    f = qt.extract_features(
        {"dataset": {"path": str(synth)}, "out": str(work / "feat"), "shadows": {"path": str(work / "shadows.parquet")}}
    )
    assert f.n_orders > 0
    feats = next(Path(work / "feat").glob("*.parquet"))
    card = qt.fit_hazard(
        {
            "features": {"path": str(feats)},
            "out": str(work / "model"),
            "gbdt": {"max_iter": 20, "min_samples_leaf": 5},
            "bootstrap_b": 20,
        }
    )
    assert isinstance(card, m.HazardModelCard)
    r = qt.report({"inputs": [str(work / "zoo"), str(work / "cal")], "out": str(work / "report.html"), "max_why": 2})
    assert "zoo" in r.sections and (work / "report.html").exists()


def test_calibrate_held_out_with_short_lifetimes(synth: Path, tmp_path: Path) -> None:
    """Shadows living 20 s (shorter than h_max = 300 s): held-out metrics exist at the horizons their planned life
    covers (100 ms, 1 s, 10 s) and are null only at 60 s and h_max, where no label is defined."""
    sh = tmp_path / "short.parquet"
    qt.sample_shadows(
        {"dataset": {"path": str(synth)}, "out": str(sh), "n": 160, "seed": 3, "lifetime_ns": 20_000_000_000}
    )
    c = qt.calibrate(
        {
            "kind": "alloc",
            "family": "power3",
            "dataset": {"path": str(synth)},
            "shadows": {"path": str(sh)},
            "out": str(tmp_path / "cal"),
            "train": {"dates": ["2026-06-15"]},
            "test": {"dates": ["2026-06-16"]},
            "bootstrap": {"b": 10},
        }
    )
    for held in (*c.held_out.values(), *c.baseline_held_out.values()):
        for h in ("100000000", "1000000000", "10000000000"):
            assert held.brier[h] is not None and held.logloss[h] is not None and held.fill_rate_bias[h] is not None, h
        assert held.brier["60000000000"] is None and held.brier["300000000000"] is None
    assert len(c.held_out) == 1 and len(c.baseline_held_out) == 1
    assert any("no shadow's planned life covers" in w for w in c.warnings)
