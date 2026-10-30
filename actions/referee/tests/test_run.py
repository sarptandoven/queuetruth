"""The referee GitHub Action's runner (`actions/referee/run.py`) on simulators that are right, wrong and broken."""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_core

RUN = Path(__file__).resolve().parents[1] / "run.py"
PY = shlex.quote(sys.executable)


def _runner():
    spec = importlib.util.spec_from_file_location("referee_action_run", RUN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _qt() -> str:
    exe = Path(sys.executable).parent / "queuetruth"
    if not exe.exists():
        pytest.skip("the queuetruth console script is not installed next to this interpreter")
    return str(exe)


#: A perfect simulator: the exact engine's own fills.
PERFECT = 'cp "$QT_DATASET/../expected_fills.parquet" "$QT_FILLS"'
#: Loses the last fill of class 05 (the documented wrong simulator); exact elsewhere.
LOSES_LAST_FILL = (
    f'{PY} -c "import os, pyarrow.parquet as pq; '
    "t = pq.read_table(os.path.join(os.environ['QT_DATASET'], '..', 'expected_fills.parquet')); "
    "t = t.slice(0, t.num_rows - 1) if os.environ['QT_CLASS'].startswith('05_') else t; "
    "pq.write_table(t, os.environ['QT_FILLS'])\""
)


def test_a_perfect_simulator_passes_every_class(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rc = _runner().main(
        ["--sim-command", PERFECT, "--simulator", "exact@test", "--out", str(out), "--queuetruth", _qt()]
    )
    assert rc == 0
    s = json.loads((out / "summary.json").read_text())
    assert (s["passed"], s["total"]) == (16, 16), s
    assert all(v["pass"] for v in s["classes"].values())
    badge = json.loads((out / "badge.json").read_text())
    assert badge == {
        "schemaVersion": 1,
        "label": "queuetruth referee",
        "message": "16/16 passed",
        "color": "brightgreen",
    }
    assert "16 of 16 fixture classes pass" in (out / "summary.md").read_text()


def test_a_wrong_simulator_fails_its_class_and_the_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out"
    step = tmp_path / "step.md"
    gh_out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step))
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    rc = _runner().main(
        ["--sim-command", LOSES_LAST_FILL, "--classes", "05,06", "--out", str(out), "--queuetruth", _qt()]
    )
    assert rc == 1
    s = json.loads((out / "summary.json").read_text())
    c05 = s["classes"]["05_partial_fills_sum_to_cum"]
    assert (c05["pass"], c05["qty_mismatch"]) == (False, 1)
    assert s["classes"]["06_fill_equal_to_leaves_completes"]["pass"] is True
    assert "1 of 2 fixture classes pass" in step.read_text()
    assert "passed=1\ntotal=2\n" in gh_out.read_text()
    assert json.loads((out / "badge.json").read_text())["color"] == "red"


def test_a_crashing_simulator_and_bad_selections(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rc = _runner().main(["--sim-command", "exit 3", "--classes", "01", "--out", str(out), "--queuetruth", _qt()])
    assert rc == 1
    v = json.loads((out / "summary.json").read_text())["classes"]["01_unrelated_delete_keeps_position"]
    assert v["pass"] is False and "exit code 3" in v["error"]
    assert _runner().main(["--sim-command", PERFECT, "--classes", "99", "--out", str(out), "--queuetruth", _qt()]) == 2
    rc = _runner().main(
        ["--sim-command", PERFECT, "--classes", "01,02", "--skip", "01,02", "--out", str(out), "--queuetruth", _qt()]
    )
    assert rc == 2
