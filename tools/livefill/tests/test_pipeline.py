"""End to end on synthetic data: capture, check, export, match, canonical log, ingest, score; and the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lf_testkit import needs_core
from livefill import cli, pipeline, synth

from queuetruth.errors import QtError


@needs_core
def test_pipeline_end_to_end(synth_default, tmp_path: Path) -> None:
    doc = pipeline.run_pipeline(
        synth_default.capture, [synth_default.log], tmp_path / "p", account="acct1", models=["front_of_queue"]
    )
    (cap,) = doc["per_capture"]
    assert cap["check"]["verdict"] == "consistent"
    assert cap["match"]["matched_by"] == {"id": len(synth_default.own)}
    seg = cap["segments"]["0"]
    assert seg["validation"]["errors"] == [] and seg["data_quality"]["crossed_books_after_last"] == 0
    exact, foq = seg["scores"]
    assert exact["arm"] == "exact" and foq["arm"] == "front_of_queue"
    n = len(synth_default.own)
    n_filled = sum(1 for o in synth_default.own if o["filled_qty"] > 0)
    # the exact arm re-enters each own order in its own queue place: it reproduces every live outcome
    assert seg["exact_agrees"] is True and doc["exact_agrees"] is True
    # an own order that rested and filled within 100 us is named as the maker by its trade, so the ingest keeps it
    # (maker_in_taker_window) and every own order has an MBO twin
    assert seg["ingest_suppressed"] == cap["match"]["ingest_suppressed"] == 0
    assert exact["twin_match_rate"] == 1.0
    assert exact["two_by_two"] == {"both": n_filled, "neither": n - n_filled, "live_only": 0, "sim_only": 0}
    assert exact["fill_rate_bias"] == 0.0
    # pooled over the one segment: the same table, exact McNemar p 1
    pe = doc["pooled"]["exact"]
    assert pe["two_by_two"] == exact["two_by_two"] and pe["n_segments"] == 1 and pe["fill_rate_bias"] == 0.0
    assert pe["mcnemar_p_exact"] == 1.0 and pe["exact_agrees"] is True
    # the optimistic bound never fills less than live
    assert foq["two_by_two"]["live_only"] == 0 and foq["fill_rate_bias"] >= 0
    for name in ("check.json", "match/own_orders.parquet", "live/seg-0000/events.parquet"):
        assert (tmp_path / "p" / "cap-0" / name).exists(), name
    assert (tmp_path / "p" / "pipeline.json").exists()
    assert any(c.startswith("queuetruth score --live") for c in doc["commands"])
    assert len(doc["run_id"]) == 16 and int(doc["run_id"], 16) >= 0
    # deterministic: the same inputs give the same run id and the same scorecards
    doc2 = pipeline.run_pipeline(
        synth_default.capture, [synth_default.log], tmp_path / "p2", account="acct1", models=["front_of_queue"]
    )
    assert doc2["run_id"] == doc["run_id"] and doc2["pooled"] == doc["pooled"]
    seg2 = doc2["per_capture"][0]["segments"]["0"]
    assert [s["run_id"] for s in seg2["scores"]] == [s["run_id"] for s in seg["scores"]]
    a = json.loads((tmp_path / "p/cap-0/score/seg-0000/exact/summary.json").read_text())
    b = json.loads((tmp_path / "p2/cap-0/score/seg-0000/exact/summary.json").read_text())
    assert a["two_by_two"] == b["two_by_two"] and a["run_id"] == b["run_id"] and a["bias"] == b["bias"]
    for name in ("score_orders.parquet", "score_fills.parquet"):
        pa_, pb_ = (x / "cap-0/score/seg-0000/exact" / name for x in (tmp_path / "p", tmp_path / "p2"))
        assert pa_.read_bytes() == pb_.read_bytes(), name
    # options enter the run id
    doc3 = pipeline.run_pipeline(synth_default.capture, [synth_default.log], tmp_path / "p3", models=[], exact=True)
    assert doc3["run_id"] != doc["run_id"]


@needs_core
def test_pipeline_with_gaps_pools_the_segments(tmp_path: Path) -> None:
    r = synth.build(tmp_path / "g", seed=5, steps=1200, n_own=16, drop=(900,), reconnect_at=(1500 // 2,))
    doc = pipeline.run_pipeline(r.capture, [r.log], tmp_path / "p", models=["front_of_queue"])
    (cap,) = doc["per_capture"]
    assert cap["check"]["gaps"] == 2 and cap["check"]["verdict"] == "gaps"
    scored = [s for s in cap["segments"].values() if s.get("scores")]
    assert len(scored) >= 2 and all(s["exact_agrees"] for s in scored)
    excluded = cap["live"]["excluded"]
    n_scored = sum(s["scores"][0]["n_orders"] for s in scored)
    n_logged = sum(v["n_orders"] for v in cap["live"]["segments"].values())
    assert n_scored == n_logged == 16 - excluded["crosses_gap"]
    for arm in ("exact", "front_of_queue"):
        pooled = doc["pooled"][arm]
        assert pooled["n_orders"] == n_scored and pooled["n_segments"] == len(scored)
        per_seg = [next(x for x in s["scores"] if x["arm"] == arm)["two_by_two"] for s in scored]
        assert pooled["two_by_two"] == {k: sum(t[k] for t in per_seg) for k in per_seg[0]}
        lo, hi = pooled["bias_ci"]
        assert lo is None or lo <= pooled["fill_rate_bias"] <= hi


@needs_core
def test_pipeline_pools_several_captures(synth_default, tmp_path: Path) -> None:
    other = synth.build(tmp_path / "o", seed=9, steps=900, n_own=10, market="sybusd")
    doc = pipeline.run_pipeline(
        [synth_default.capture, other.capture], [synth_default.log, other.log], tmp_path / "p", models=[]
    )
    a, b = doc["per_capture"]
    assert a["match"]["n_orders"] == len(synth_default.own) and b["match"]["n_orders"] == 10
    assert doc["orders_outside_captures"] == 0
    n = sum(s["scores"][0]["n_orders"] for c in doc["per_capture"] for s in c["segments"].values() if s.get("scores"))
    assert doc["pooled"]["exact"]["n_orders"] == n and doc["pooled"]["exact"]["n_segments"] == 2
    assert (tmp_path / "p/cap-1/match/own_orders.parquet").exists()
    # the same market recorded by two captures at once: an order would be scored twice
    with pytest.raises(QtError) as e:
        pipeline.run_pipeline(
            [synth_default.capture, other.capture, synth.build(tmp_path / "d", seed=7).capture],
            [synth_default.log],
            tmp_path / "q",
        )
    assert e.value.code == "QT-USAGE-001"


def test_pipeline_requires_a_log_and_can_refuse_inconsistent_captures(
    synth_default, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(QtError):
        pipeline.run_pipeline(synth_default.capture, [], tmp_path / "x")
    real = pipeline.check.check_capture

    def inconsistent(*a, **k):
        return {**real(*a, **k), "verdict": "inconsistent", "unexplained": 3}

    monkeypatch.setattr(pipeline.check, "check_capture", inconsistent)
    with pytest.raises(QtError) as e:
        pipeline.run_pipeline(synth_default.capture, [synth_default.log], tmp_path / "y", require_consistent=True)
    assert e.value.code == "QT-DATA-001" and "3 unexplained" in e.value.message


def test_pipeline_refuses_a_non_empty_out(synth_default, tmp_path: Path) -> None:
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "keep.txt").write_text("precious")
    for out in (tmp_path / "used", Path(synth_default.capture)):
        with pytest.raises(QtError) as e:
            pipeline.run_pipeline(synth_default.capture, [synth_default.log], out)
        assert e.value.code == "QT-CONFIG-001"
    assert (tmp_path / "used" / "keep.txt").read_text() == "precious"


def test_split_models() -> None:
    assert pipeline.split_models("hbt_power3[n=2],nt_prob_touch[p=0.3]") == ["hbt_power3[n=2]", "nt_prob_touch[p=0.3]"]
    assert pipeline.split_models("front_of_queue; trade_through_only,front_of_queue") == [
        "front_of_queue",
        "trade_through_only",
    ]


@needs_core
def test_split_models_all() -> None:
    models = pipeline.split_models("all")
    assert len(models) == 16 and "hbt_power3[n=2]" in models and "front_of_queue" in models


def test_cli_commands(synth_default, tmp_path: Path, capsys) -> None:
    assert cli.main(["check", str(synth_default.capture), "--strict"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "consistent"
    assert cli.main(["export", str(synth_default.capture), "--out", str(tmp_path / "e")]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "livefill.export"
    assert (
        cli.main(["match", str(synth_default.capture), "--log", str(synth_default.log), "--out", str(tmp_path / "m")])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["n_in_feed"] == len(synth_default.own)
    assert cli.main(["check", str(synth_default.capture), "--arrival-ties", "--book-seed", "ws"]) == 0
    assert json.loads(capsys.readouterr().out)["id_ties"] is False
    assert cli.main(["synth", "--out", str(tmp_path / "bad"), "--steps", "100"]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "QT-USAGE-001"
    assert cli.main(["synth", "--out", str(tmp_path / "s"), "--steps", "300", "--n-own", "3", "--drop", "50"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dropped"] == [50]
    assert cli.main(["check", out["capture"], "--strict"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "gaps"
    assert cli.main(["check", str(tmp_path / "missing")]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "QT-INPUT-001"
    assert cli.duration_s("2m") == 120 and cli.duration_s("500ms") == 0.5 and cli.duration_s("90") == 90
    for bad in ("x", "-1s", "0"):
        with pytest.raises(QtError):
            cli.duration_s(bad)


@needs_core
def test_cli_pipeline(synth_default, tmp_path: Path, capsys, monkeypatch) -> None:
    rc = cli.main(
        [
            "pipeline",
            str(synth_default.capture),
            "--log",
            str(synth_default.log),
            "--out",
            str(tmp_path / "p"),
            "--models",
            "trade_through_only",
            "--book-seed",
            "ws",
            "--strict",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    arms = [s["arm"] for s in doc["per_capture"][0]["segments"]["0"]["scores"]]
    assert arms == ["exact", "trade_through_only"] and doc["options"]["book_seed"] == "ws"
    assert "  exact: 24 orders in 1 segment(s); both" in captured.err and "trade_through_only:" in captured.err
    # --strict fails when the exact arm disagrees with live
    real = pipeline.run_pipeline
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: {**real(*a, **k), "exact_agrees": False})
    args = ["pipeline", str(synth_default.capture), "--log", str(synth_default.log), "--models", ""]
    assert cli.main([*args, "--out", str(tmp_path / "q"), "--strict"]) == 1
    assert cli.main([*args, "--out", str(tmp_path / "r")]) == 0
