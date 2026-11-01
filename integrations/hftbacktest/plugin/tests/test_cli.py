"""`queuetruth-hftbacktest`: subcommands, exit codes and the `qt.error` documents."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from hbtkit import exact_as_hbt

from queuetruth_hftbacktest import Prepared, __version__
from queuetruth_hftbacktest.cli import duration_ns, main


def test_version() -> None:
    out = subprocess.run([sys.executable, "-m", "queuetruth_hftbacktest", "--version"], capture_output=True, text=True,
                         check=True, env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")})  # fmt: skip
    assert out.stdout.strip() == f"queuetruth-hftbacktest {__version__}"


def test_durations() -> None:
    assert [duration_ns(x) for x in ("1s", "250ms", "5m", "5min", "10us", "7", "1.5s")] == [
        1_000_000_000, 250_000_000, 300_000_000_000, 300_000_000_000, 10_000, 7, 1_500_000_000,
    ]  # fmt: skip


def test_prepare_and_score(synth: Path, shadows: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["prepare", "--data", str(synth), "--orders", str(shadows), "--out", str(tmp_path / "p")]) == 0
    out = capsys.readouterr().out
    assert "orders ready for hftbacktest" in out and "HBT_PYTHON" in out and "--snippet ASSET.py" in out
    p = Prepared.load(tmp_path / "p")
    exact_as_hbt(p, p.out / "hbt_orders.jsonl")
    assert main(["score", str(p.out), "--horizons", "1s,60s", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qthbt.score" and [h["horizon_ns"] for h in doc["horizons"]] == [10**9, 60 * 10**9]
    assert all(h["bias"] == 0.0 for h in doc["horizons"])
    assert main(["score", str(p.out)]) == 0
    assert capsys.readouterr().out.startswith("hftbacktest vs exact FIFO queue:")


def test_calibrate_exit_codes(synth: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["calibrate", "--data", str(synth), "--n-orders", "1500", "--families", "power3"]
    ok = [*base, "--train", "2026-06-15", "--test", "2026-06-16", "--seed", "3", "--out", str(tmp_path / "ok")]
    assert main(ok) == 0
    assert capsys.readouterr().out.startswith("use .power_prob_queue_model3(")
    assert (tmp_path / "ok" / "hbt_queue_model.py").exists()
    bad = [*base, "--train", "2026-06-16", "--test", "2026-06-15", "--seed", "2", "--out", str(tmp_path / "no")]
    assert main([*bad, "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["best"] is None and doc["code"] is None
    assert main([*bad, "--force"]) == 0
    assert "NOT VALIDATED" in (tmp_path / "no" / "hbt_queue_model.py").read_text()


def test_errors_are_qt_error_documents(synth: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["score", str(tmp_path), "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.error" and doc["code"] == "QT-INPUT-001"
    assert main(["calibrate", "--data", str(synth), "--train", "nope", "--out", str(tmp_path / "o")]) == 2
    assert "error: QT-CONFIG-001" in capsys.readouterr().err


def test_queuetruth_errors_keep_their_code(synth: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "orders.parquet"
    bad.write_bytes(b"not parquet")
    assert main(["prepare", "--data", str(synth), "--orders", str(bad), "--out", str(tmp_path / "p"), "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["code"].startswith("QT-") and doc["code"] != "QT-ENGINE-001"
