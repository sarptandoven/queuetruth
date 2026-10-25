"""The #312 reproduction (`upstream/repro_312.py`) and the patch's Python tests, in both hftbacktest venvs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

UP = Path(__file__).resolve().parents[1] / "upstream"
CASES = {"equal_leaves", "float_residual", "non_lot_quantity", "trade_through"}


def _repro(py: Path) -> tuple[int, dict]:
    r = subprocess.run([str(py), "-I", str(UP / "repro_312.py"), "--json"], capture_output=True, text=True)
    return r.returncode, json.loads(r.stdout)


def test_released_2_4_4_reproduces_312(hbt_stock_py: Path) -> None:
    code, doc = _repro(hbt_stock_py)
    assert code == 3
    assert doc["hftbacktest"] == "2.4.4"
    assert set(doc["cases"]) == CASES
    for name, c in doc["cases"].items():
        assert c["aborted"] and c["final_code"] == 14, name
        assert c["states"][-1]["status"] == "filled", name
    # the residual case: leaves 0.1 - 0.01 = 0.09000000000000001 > 9 * 0.01, so `>=` alone would not catch it
    assert doc["cases"]["float_residual"]["states"][-1]["leaves_qty"] == (0.1 - 0.01) - 0.09 > 0.0


def test_patched_build_runs_every_case_to_the_end(hbt_patched_py: Path) -> None:
    code, doc = _repro(hbt_patched_py)
    assert code == 0
    for name, c in doc["cases"].items():
        assert not c["aborted"] and c["final_code"] == 1, name
    stock_like = {
        "equal_leaves": ("filled", 1.0, 0.0),
        "float_residual": ("filled", 0.09, (0.1 - 0.01) - 0.09),
        "non_lot_quantity": ("filled", 1.0, 1.4 - 1.0),
        "trade_through": ("filled", 1.0, 0.0),
    }
    for name, want in stock_like.items():
        last = doc["cases"][name]["states"][-1]
        assert (last["status"], last["exec_qty"], last["leaves_qty"]) == want, name


def _patch_tests(tmp_path: Path) -> Path:
    """The Python test file the patch adds, extracted from the patch."""
    text = (UP / "0001-partialfillexchange-remove-completed-orders-312.patch").read_text()
    part = text[text.index("+++ b/py-hftbacktest/tests/test_partial_fill_312.py") :]
    body = part[part.index("@@") :].split("\n", 1)[1]
    lines = [ln[1:] for ln in body.splitlines() if ln.startswith("+")]
    p = tmp_path / "test_partial_fill_312.py"
    p.write_text("\n".join(lines) + "\n")
    return p


@pytest.mark.parametrize(("which", "want"), [("stock", "FAILED (failures=4)"), ("patched", "OK")])
def test_patch_python_tests(which: str, want: str, hbt_stock_py: Path, hbt_patched_py: Path, tmp_path: Path) -> None:
    py = hbt_stock_py if which == "stock" else hbt_patched_py
    r = subprocess.run([str(py), "-I", str(_patch_tests(tmp_path))], capture_output=True, text=True, cwd=tmp_path)
    assert r.stderr.strip().splitlines()[-1] == want, r.stderr[-2000:]
    assert "Ran 5 tests" in r.stderr


def test_patch_touches_only_the_exchange_and_its_tests() -> None:
    text = (UP / "0001-partialfillexchange-remove-completed-orders-312.patch").read_text()
    files = [ln[len("+++ b/") :] for ln in text.splitlines() if ln.startswith("+++ b/")]
    assert files == [
        "hftbacktest/src/backtest/proc/partialfillexchange.rs",
        "py-hftbacktest/tests/test_partial_fill_312.py",
    ]
    assert "Fixes #312." in text
