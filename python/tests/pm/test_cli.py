"""`queuetruth pm ingest | calibrate | score` through the main CLI entry point (`queuetruth.pm.cli`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pm._helpers import FIX

from queuetruth.cli import main
from queuetruth.errors import QtError
from queuetruth.pm import cli as pm_cli
from queuetruth.pm.cli import _money, duration_ns, is_pm, render, split_globals


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    rc = main(list(args))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_routing_globals_and_durations() -> None:
    assert is_pm(["pm", "ingest"]) and is_pm(["--format", "json", "pm"]) and is_pm(["--quiet", "--seed=3", "pm"])
    assert not is_pm(["zoo", "pm"]) and not is_pm([]) and not is_pm(["--format", "json"])
    g, rest = split_globals(["--format=json", "pm", "score", "--quiet", "--seed", "7", "--out", "x"])
    assert (g["format"], g["quiet"], g["seed"], rest) == ("json", True, "7", ["pm", "score", "--out", "x"])
    with pytest.raises(QtError):
        split_globals(["--format", "yaml", "pm"])
    with pytest.raises(QtError):
        split_globals(["pm", "--log-level"])
    assert duration_ns("500ms") == 500_000_000 and duration_ns("1.5s") == 1_500_000_000
    assert duration_ns("5min") == 300_000_000_000 and duration_ns("2us") == 2000
    for bad in ("60", "1.5ns", "5 parsecs", ""):
        with pytest.raises(QtError) as e:
            duration_ns(bad)
        assert e.value.code == "QT-USAGE-001"
    assert [_money(x) for x in (None, 0.0, -10.62, 0.0034, 1234.5)] == [
        "n/a",
        "$0.00",
        "-$10.62",
        "$0.0034",
        "$1,234.50",
    ]


def test_full_pipeline_in_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc, out, _err = run(
        capsys,
        "--format",
        "json",
        "pm",
        "ingest",
        "--venue",
        "kalshi",
        "--in",
        str(FIX / "kalshi" / "ws.jsonl"),
        "--in",
        str(FIX / "kalshi" / "rest.jsonl"),
        "--out",
        str(tmp_path / "in"),
    )
    assert rc == 0
    ing = json.loads(out)
    assert ing["schema"] == "qt.pm_ingest_summary" and ing["queue_samples"] == 76
    rc, out, _ = run(
        capsys,
        "pm",
        "calibrate",
        "--pm",
        str(tmp_path / "in"),
        "--out",
        str(tmp_path / "cal"),
        "--grid",
        "-2:2:5",
        "--families",
        "power3,log",
        "--bootstrap",
        "50",
        "--loss",
        "mae",
        "--no-atomic",
        "--format=json",
    )
    cal = json.loads(out)
    assert rc == 0 and cal["spec"]["grid_lo"] == -2.0 and cal["spec"]["grid_n"] == 5
    assert (cal["loss"], cal["spec"]["loss"], cal["spec"]["atomic"]) == ("mae", "mae", False)
    assert {m["family"] for m in cal["models"]} == {"power3", "log", "cancels_ahead", "risk_adverse", "bounds_midpoint"}
    assert not any(m["atomic"] for m in cal["models"])
    rc, out, _ = run(
        capsys,
        "--output-format",
        "json",
        "pm",
        "score",
        "--pm",
        str(tmp_path / "in"),
        "--out",
        str(tmp_path / "sc"),
        "--calibration",
        str(tmp_path / "cal"),
        "--window",
        "30s",
        "--markout-horizons",
        "1s,5s",
        "--member",
        "non-direct",
        "--settle",
        "KXDEMO-26OCT23-T50=1",
    )
    sc = json.loads(out)
    assert rc == 0 and sc["spec"]["window_ns"] == 30_000_000_000 and sc["spec"]["member"] == "non_direct"
    assert sc["spec"]["markout_horizons_ns"] == [1_000_000_000, 5_000_000_000] and sc["model_id"] == cal["best"]


def test_human_output_answers_first(capsys: pytest.CaptureFixture[str], kalshi_run: Any, tmp_path: Path) -> None:
    rc, out, err = run(
        capsys, "pm", "score", "--pm", str(kalshi_run.pm), "--out", str(tmp_path / "s"), "--model", "log"
    )
    lines = out.splitlines()
    assert rc == 0 and lines[0].startswith("pm score (kalshi, live, model log): total PnL $")
    assert "(scenarios $" in lines[0]  # a range over the three scenarios, not an interval
    assert lines[1].startswith("trading PnL bound over orders: [$") and lines[2].startswith("model: spread capture")
    assert any("sim vs live front_of_queue" in x for x in lines) and err == ""
    text = render("calibrate", kalshi_run.calibration.model_dump(mode="json"), verbose=True)
    assert text.splitlines()[0].startswith("pm calibrate (kalshi, fit on queue_samples): best ")
    assert '"schema": "qt.pm_calibration"' in text  # --verbose appends the document
    ing = render("ingest", kalshi_run.ingest.model_dump(mode="json"), verbose=False)
    assert "official queue positions: 76" in ing and "queue entry points: 26 exact" in ing


def test_calibrate_view_shows_the_reference_rows_and_the_verdict(kalshi_run: Any, poly_run: Any) -> None:
    for run_ in (kalshi_run, poly_run):
        doc = run_.calibration.model_dump(mode="json")
        lines = render("calibrate", doc, verbose=False).splitlines()
        rows = {x.split()[0] for x in lines if x.startswith("  ") and not x.startswith("  model")}
        assert {"cancels_ahead", "risk_adverse", "bounds_midpoint"} <= rows  # always shown, whatever their rank
        assert any(x.startswith(f"verdict: DO NOT USE ({', '.join(doc['trust']['reasons'])})") for x in lines)
    doc = poly_run.calibration.model_dump(mode="json")
    doc.update(best=None, trust={"verdict": "refused", "reasons": ["no_estimate"], "flags": ["no_estimate"]})
    assert render("calibrate", doc, verbose=False).splitlines()[0].endswith("best none (nothing to fit)")


def test_score_view_flags_a_live_total_outside_the_scenarios(poly_run: Any) -> None:
    lines = render("score", poly_run.score.model_dump(mode="json"), verbose=False).splitlines()
    assert lines[0].startswith("pm score (polymarket, live, model cancels_ahead, calibration refused): ")
    assert any("outside the scenario range" in x and "the trading bound holds" in x for x in lines)
    assert any(x.startswith("calibration verdict: refused (") for x in lines)
    k = next(i for i, x in enumerate(lines) if x.startswith("  sim vs live cancels_ahead"))
    assert lines[k + 1].startswith("    on the 9 test orders only")


def test_fixtures_command_writes_the_walkthrough_captures(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = tmp_path / "fx"
    rc, text, _err = run(capsys, "--format", "json", "pm", "fixtures", "--out", str(out))
    doc = json.loads(text)
    assert rc == 0 and doc["schema"] == "qt.pm_fixtures" and len(doc["files"]) == 11
    for f in doc["files"]:  # byte for byte the repository's fixtures
        assert (out / f["name"]).read_bytes() == (FIX / f["name"]).read_bytes()
    rc, text, _ = run(capsys, "pm", "fixtures", "--out", str(out))  # an earlier generation is replaced
    assert rc == 0 and text.startswith("pm fixtures: wrote 11 files under ")
    other = tmp_path / "other"
    other.mkdir()
    (other / "mine.txt").write_text("keep me", encoding="utf-8")
    rc, _, err = run(capsys, "pm", "fixtures", "--out", str(other))
    assert rc == 2 and "QT-CONFIG-001" in err and (other / "mine.txt").read_text() == "keep me"


def test_cli_parity_interrupts_threads_warnings_and_logging(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(_spec: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr("queuetruth.pm.ingest.ingest", interrupted)
    rc, out, _ = run(capsys, "--format", "json", "pm", "ingest", "--venue", "kalshi", "--in", "a", "--out", "o")
    doc = json.loads(out)
    assert rc == 2 and (doc["code"], doc["message"]) == ("QT-USAGE-001", "interrupted")
    rc, out, _ = run(capsys, "--threads", "4", "--format", "json", "pm", "fixtures", "--out", str(tmp_path / "t"))
    assert rc == 2 and json.loads(out)["code"] == "QT-USAGE-001" and "--threads" in json.loads(out)["message"]
    assert not (tmp_path / "t").exists()

    def noisy(_out: Any) -> Any:
        import logging
        import warnings

        warnings.warn("a library complained", UserWarning, stacklevel=1)
        logging.getLogger("queuetruth.pm").warning("logged once")
        return []

    monkeypatch.setattr("queuetruth.pm.fixtures.write_fixtures", noisy)
    import logging

    root = logging.getLogger()
    saved = list(root.handlers)
    for h in saved:  # basicConfig only configures a logger without handlers
        root.removeHandler(h)
    try:
        rc, _out, err = run(capsys, "pm", "fixtures", "--out", str(tmp_path / "w"))
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
    assert rc == 0 and "warning: a library complained" in err.splitlines()
    assert "WARNING logged once" in err.splitlines()


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["pm", "ingest", "--venue", "kalshi"], "required"),
        (["pm", "ingest", "--venue", "nyse", "--in", "a", "--out", "o"], "invalid choice"),
        (["pm", "score", "--pm", "p", "--out", "o", "--window", "60"], "needs a unit"),
        (["pm", "calibrate", "--pm", "p", "--out", "o", "--grid", "1:2"], "LO:HI:N"),
        (["pm", "calibrate", "--pm", "p", "--out", "o", "--grid", "a:b:c"], "expected numbers"),
        (["pm", "ingest", "--venue", "polymarket", "--in", "a", "--out", "o", "--market", "c=yes"], "COND=YES,NO"),
        (["pm", "ingest", "--venue", "kalshi", "--in", "a", "--out", "o", "--instrument-id", "S=x"], "non-negative"),
        (["pm", "score", "--pm", "p", "--out", "o", "--settle", "S"], "KEY=VALUE"),
        (["pm", "score", "--pm", "p", "--out", "o", "--settle", "S=1", "--settle", "S=0"], "given twice"),
        (["pm", "score", "--pm", "p", "--out", "o", "--model", "log", "--calibration", "c"], "not allowed"),
        (["pm", "frobnicate"], "invalid choice"),
    ],
)
def test_usage_errors_are_qt_usage_001_with_exit_code_2(
    capsys: pytest.CaptureFixture[str], args: list[str], needle: str
) -> None:
    rc, out, _ = run(capsys, "--format", "json", *args)
    doc = json.loads(out)
    assert rc == 2 and doc["schema"] == "qt.error" and doc["code"] == "QT-USAGE-001" and needle in doc["message"]
    rc, out, err = run(capsys, *args)
    assert rc == 2 and out == "" and err.startswith("QT-USAGE-001: ")


def test_input_errors_and_unexpected_failures(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out, _ = run(
        capsys, "pm", "calibrate", "--pm", str(tmp_path / "nope"), "--out", str(tmp_path / "o"), "--format", "json"
    )
    assert rc == 2 and json.loads(out)["code"] == "QT-INPUT-001"

    def boom(_spec: Any) -> Any:
        raise RuntimeError("kaput")

    monkeypatch.setattr("queuetruth.pm.ingest.ingest", boom)
    rc, out, _ = run(capsys, "pm", "ingest", "--venue", "kalshi", "--in", "a", "--out", "o", "--format", "json")
    doc = json.loads(out)
    assert rc == 2 and doc["code"] == "QT-ENGINE-001" and "RuntimeError: kaput" in doc["message"]


def test_help_and_non_pm_commands(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, _ = run(capsys, "pm", "--help")
    assert (
        rc == 0 and "usage: queuetruth pm" in out and "Self-hosted use on your own data only" in " ".join(out.split())
    )
    rc, out, _ = run(capsys, "pm")
    assert rc == 0 and "{fixtures,ingest,calibrate,score}" in out
    rc, out, _ = run(capsys, "pm", "score", "--help")
    assert rc == 0 and "--poly-daily-reward" in out
    assert pm_cli.run(["zoo", "--help"]) is None  # not a pm command: the Rust-parsed CLI handles it
