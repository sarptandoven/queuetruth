"""`queuetruth forecast ...` through the console entry point: the worked example of the how-to, JSON documents,
human views, exit codes and usage errors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from queuetruth import cli
from queuetruth.errors import QtError
from queuetruth.forecast.cli import command_word, parse_duration
from queuetruth.schemas import parse_document


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    try:
        doc = json.loads(out)
    except json.JSONDecodeError:
        doc = out
    return code, doc, err


@pytest.fixture
def example(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    code, doc, _ = run(
        capsys, "forecast", "simulate", "--out", str(tmp_path / "ex"), "--questions", "300", "--format", "json"
    )
    assert code == 0 and doc["questions"] == 300
    return tmp_path / "ex"


def test_worked_example(example: Path, capsys: pytest.CaptureFixture[str]) -> None:
    led = str(example / "ledger.jsonl")
    code, doc, _ = run(
        capsys,
        "--format",
        "json",
        "forecast",
        "commit",
        "--forecasts",
        str(example / "forecasts.csv"),
        "--ledger",
        led,
        "--commitment-out",
        str(example / "commitment.json"),
    )
    assert code == 0
    summary = parse_document(doc)
    assert summary.schema_ == "qt.forecast_ledger_summary"
    assert (doc["appended"], doc["verified"], doc["entries_truncated"]) == (600, True, True)
    assert len(doc["entries"]) == 50
    commitment = json.loads((example / "commitment.json").read_text())
    assert commitment == doc["commitment"] and commitment["head_seq"] == 599
    code, doc, _ = run(
        capsys,
        "forecast",
        "resolve",
        "--resolutions",
        str(example / "resolutions.csv"),
        "--ledger",
        led,
        "--format=json",
    )
    assert (code, doc["appended"], doc["action"]) == (0, 300, "resolve")
    code, doc, _ = run(capsys, "forecast", "commit", "--forecasts", str(example / "late.csv"), "--ledger", led)
    assert code == 0 and "forecast commit: verified, appended 300" in doc
    code, doc, _ = run(
        capsys,
        "forecast",
        "verify",
        "--ledger",
        led,
        "--commitment",
        str(example / "commitment.json"),
        "--output-format",
        "json",
    )
    assert (code, doc["verified"], doc["appended"]) == (0, True, 0)
    out = example / "score"
    code, doc, err = run(
        capsys,
        "forecast",
        "score",
        "--ledger",
        led,
        "--market",
        str(example / "quotes.csv"),
        "--out",
        str(out),
        "--bootstrap-b",
        "200",
        "--fee-quadratic",
        "0.07",
        "--fee-round-up-to",
        "0.01",
        "--format",
        "json",
    )
    assert code == 0, err
    card = parse_document(doc)
    assert card.schema_ == "qt.forecast_scorecard"
    assert doc["counts"]["by_status"] == {"after_resolution_entry": 300, "scored": 600}
    assert doc["spec"]["pnl"]["fees"] == {
        "per_contract": 0.0,
        "notional_rate": 0.0,
        "quadratic": 0.07,
        "round_up_to": 0.01,
    }
    assert json.loads((out / "forecast_scorecard.json").read_text()) == doc
    assert "rejected by the leakage" in err
    # the human view leads with the answer
    code, text, _ = run(
        capsys,
        "forecast",
        "score",
        "--ledger",
        led,
        "--market",
        str(example / "quotes.csv"),
        "--out",
        str(out),
        "--bootstrap-b",
        "50",
    )
    assert code == 0
    first = text.splitlines()[0]
    assert first.startswith("forecast scorecard ") and "600 of 900 forecasts scored, 300 rejected" in first
    assert "sharp: 300 scored" in text and "vs baseline:market: skill" in text
    # the per-question PnL comes first; the bankroll path is marked when cash ran out
    assert text.index("PnL per question (isolated)") < text.index("bankroll path (CASH-LIMITED")


def test_verify_detects_tampering_with_exit_1(example: Path, capsys: pytest.CaptureFixture[str]) -> None:
    led = example / "ledger.jsonl"
    run(
        capsys,
        "forecast",
        "commit",
        "--forecasts",
        str(example / "forecasts.csv"),
        "--ledger",
        str(led),
        "--commitment-out",
        str(example / "c.json"),
    )
    led.write_text(led.read_text().replace('"p":0.', '"p":0.0', 1))
    code, doc, err = run(capsys, "forecast", "verify", "--ledger", str(led), "--format", "json")
    assert code == 1 and doc["verified"] is False and doc["problems"]
    assert "QT-GATE-001" in err
    code, text, _ = run(capsys, "forecast", "verify", "--ledger", str(led), "--commitment", str(example / "c.json"))
    assert code == 1 and "INTEGRITY PROBLEMS" in text
    code, doc, _ = run(
        capsys, "forecast", "score", "--ledger", str(led), "--out", str(example / "o"), "--format", "json"
    )
    assert code == 2 and doc["schema"] == "qt.error" and doc["code"] == "QT-INPUT-002"


def test_declared_time_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "f.jsonl"
    f.write_text('{"question_id": "q", "p": 0.4}\n')
    code, doc, err = run(
        capsys,
        "forecast",
        "commit",
        "--forecasts",
        str(f),
        "--ledger",
        str(tmp_path / "l.jsonl"),
        "--forecaster",
        "me",
        "--at",
        "2026-01-01T00:00:00Z",
        "--format",
        "json",
    )
    assert code == 0 and "clock: declared" in err
    assert doc["warnings"] and doc["entries"][0]["forecaster"] == "me"


def test_power_command(capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run(capsys, "forecast", "power", "--edge", "0.01", "--sd", "0.1", "--n", "400", "--format", "json")
    assert code == 0 and 785 <= doc["n_required"] <= 788
    assert 0.4 < doc["power_at_n"] < 0.6
    code, doc, _ = run(capsys, "forecast", "power", "--edge", "0.01", "--sd", "0", "--format", "json")
    assert code == 2 and doc["code"] == "QT-CONFIG-001"


@pytest.mark.parametrize(
    ("argv", "code", "err_code"),
    [
        (["forecast", "bogus"], 2, "QT-USAGE-001"),
        (["forecast", "commit", "--ledger", "x"], 2, "QT-USAGE-001"),
        (["forecast", "score", "--ledger", "x", "--out", "o", "--min-lead", "5"], 2, "QT-USAGE-001"),
        (["forecast", "score", "--ledger", "x", "--out", "o", "--bins", "1"], 2, "QT-CONFIG-001"),
        (["forecast", "commit", "--forecasts", "missing.csv", "--ledger", "x"], 2, "QT-INPUT-001"),
        (["--format", "xml", "forecast", "verify", "--ledger", "x"], 2, "QT-USAGE-001"),
    ],
)
def test_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], code: int, err_code: str) -> None:
    got, doc, err = run(capsys, *argv, "--format", "json")
    assert got == code
    assert doc["schema"] == "qt.error" and doc["code"] == err_code, (doc, err)


def test_help_and_human_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["forecast"]) == 2
    assert "commit" in capsys.readouterr().out
    assert cli.main(["forecast", "--help"]) == 0
    assert cli.main(["forecast", "score", "--help"]) == 0
    assert "--kelly-fraction" in capsys.readouterr().out
    assert cli.main(["forecast", "verify", "--ledger", "/nonexistent/x.jsonl"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("QT-INPUT-001:")


def test_command_word_and_durations() -> None:
    assert command_word(["--format", "json", "--quiet", "forecast", "score"]) == "forecast"
    assert command_word(["--seed=3", "forecast"]) == "forecast"
    assert command_word(["--log-level", "info", "replay"]) == "replay"
    assert command_word(["--verbose"]) is None
    assert cli.main is not None
    assert parse_duration("0", "x") == 0
    assert parse_duration("250ms", "x") == 250_000_000
    assert parse_duration("1.5s", "x") == 1_500_000_000
    assert parse_duration("2h", "x") == 7_200_000_000_000
    for bad in ("5", "1.5", "five", "1.5ns"):
        with pytest.raises(QtError):
            parse_duration(bad, "--x")


def test_global_seed_reaches_the_spec(example: Path, capsys: pytest.CaptureFixture[str]) -> None:
    led = str(example / "ledger.jsonl")
    run(capsys, "forecast", "commit", "--forecasts", str(example / "forecasts.csv"), "--ledger", led)
    run(capsys, "forecast", "resolve", "--resolutions", str(example / "resolutions.csv"), "--ledger", led)
    code, doc, _ = run(
        capsys,
        "--seed",
        "7",
        "forecast",
        "score",
        "--ledger",
        led,
        "--out",
        str(example / "s"),
        "--no-pnl",
        "--bootstrap-b",
        "20",
        "--baselines",
        "uniform",
        "--policy",
        "first",
        "--format",
        "json",
    )
    assert code == 0
    assert (doc["spec"]["seed"], doc["spec"]["pnl"], doc["spec"]["baselines"], doc["spec"]["policy"]) == (
        7,
        None,
        ["uniform"],
        "first",
    )
    assert all(s["reference"] == "baseline:uniform" for f in doc["forecasters"] for s in f["skill"])


def test_commit_repeats_and_declared_column(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "f.csv"
    f.write_text("question_id,forecaster,p\nq1,a,0.4\nq2,a,0.6\n")
    led = str(tmp_path / "l.jsonl")
    assert run(capsys, "forecast", "commit", "--forecasts", str(f), "--ledger", led)[0] == 0
    code, doc, _ = run(capsys, "forecast", "commit", "--forecasts", str(f), "--ledger", led, "--format", "json")
    assert (code, doc["code"]) == (2, "QT-INPUT-003") and "repeated forecast" in doc["message"]
    code, doc, _ = run(
        capsys, "forecast", "commit", "--forecasts", str(f), "--ledger", led, "--allow-repeat", "--format", "json"
    )
    assert (code, doc["appended"]) == (0, 2)
    h = tmp_path / "history.csv"
    h.write_text("question_id,forecaster,p,declared_at\nq3,b,0.2,2026-01-01T00:00:00Z\nq4,b,0.3,2026-01-02T00:00:00Z\n")
    code, doc, err = run(capsys, "forecast", "commit", "--forecasts", str(h), "--ledger", led, "--format", "json")
    assert code == 0 and "clock: declared" in err
    from queuetruth.forecast.ledger import verify_ledger

    tail = verify_ledger(led).entries[-2:]
    assert [(e.clock, e.recorded_at_ns) for e in tail] == [
        ("declared", 1_767_225_600_000_000_000),
        ("declared", 1_767_312_000_000_000_000),
    ]
    h.write_text("question_id,forecaster,p,declared_at\nq5,b,0.2,2026-01-02T00:00:00Z\nq6,b,0.3,2026-01-01T00:00:00Z\n")
    code, doc, _ = run(capsys, "forecast", "commit", "--forecasts", str(h), "--ledger", led, "--format", "json")
    assert code == 2 and "goes backwards" in doc["message"]
    code, doc, _ = run(
        capsys,
        "forecast",
        "commit",
        "--forecasts",
        str(h),
        "--ledger",
        led,
        "--at",
        "2026-01-01T00:00:00Z",
        "--format",
        "json",
    )
    assert (code, doc["code"]) == (2, "QT-USAGE-001")


def test_score_with_anchored_commitments(example: Path, capsys: pytest.CaptureFixture[str]) -> None:
    led = str(example / "ledger.jsonl")
    c = str(example / "commitment.json")
    run(
        capsys,
        "forecast",
        "commit",
        "--forecasts",
        str(example / "forecasts.csv"),
        "--ledger",
        led,
        "--commitment-out",
        c,
    )
    run(capsys, "forecast", "resolve", "--resolutions", str(example / "resolutions.csv"), "--ledger", led)
    run(capsys, "forecast", "commit", "--forecasts", str(example / "late.csv"), "--ledger", led)
    anchored = json.loads(Path(c).read_text())["head_recorded_at_ns"] + 1_000_000_000
    base = ["forecast", "score", "--ledger", led, "--out", str(example / "s"), "--no-pnl", "--bootstrap-b", "20"]
    code, doc, _ = run(
        capsys, *base, "--commitment", c, "--anchored-at", str(anchored), "--require-anchor", "--format", "json"
    )
    assert code == 0, doc
    assert doc["counts"]["by_status"] == {"after_resolution_entry": 300, "scored": 600}
    assert doc["ledger"]["anchors"][0]["head_seq"] == 599
    assert all(f["n_anchored"] == 300 for f in doc["forecasters"])
    code, text, _ = run(capsys, *base, "--commitment", c, "--anchored-at", str(anchored))
    assert code == 0 and "anchor: head seq 599 attested" in text
    code, doc, _ = run(capsys, *base, "--commitment", c, "--format", "json")
    assert (code, doc["code"]) == (2, "QT-USAGE-001")
    code, doc, _ = run(capsys, *base, "--pairwise", "some", "--format", "json")
    assert (code, doc["code"]) == (2, "QT-USAGE-001")
    code, doc, _ = run(capsys, *base, "--pairwise", "none", "--unresolved", "fail", "--format", "json")
    assert code == 0 and doc["comparisons"] == [] and doc["spec"]["unresolved"] == "fail"


def test_simulate_human_view_lists_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, text, _ = run(
        capsys,
        "forecast",
        "simulate",
        "--out",
        str(tmp_path / "ex"),
        "--questions",
        "5",
        "--start",
        "2027-01-01T00:00:00Z",
    )
    assert code == 0
    lines = text.splitlines()
    assert "files:" in lines and any(x.startswith("  forecasts: ") for x in lines)
