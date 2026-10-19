"""`requires_nautilus`: the real `nautilus_trader` package, in this interpreter or in a separate venv.

The NautilusTrader cross-check (scripts/nt_crosscheck/run.py) runs NautilusTrader in its own interpreter, found as
`QT_NT_PYTHON`, else `.scratch/nt/venv/bin/python`, else this interpreter when it can import `nautilus_trader`.
Tests marked `requires_nautilus` are skipped when none of these has the package.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VENV_PY = REPO_ROOT / ".scratch" / "nt" / "venv" / "bin" / "python"


def nautilus_python() -> str | None:
    """Interpreter that can import `nautilus_trader`, or None."""
    cands = [os.environ.get("QT_NT_PYTHON"), str(DEFAULT_VENV_PY)]
    for c in cands:
        if not c or not Path(c).exists():
            continue
        try:
            r = subprocess.run([c, "-c", "import nautilus_trader"], capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return c
    if importlib.util.find_spec("nautilus_trader") is not None:
        return sys.executable
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_nautilus: needs nautilus_trader (QT_NT_PYTHON or .scratch/nt/venv; see scripts/nt_crosscheck)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).resolve().parent
    mine = [i for i in items if i.get_closest_marker("requires_nautilus") and here in Path(str(i.fspath)).parents]
    if mine and nautilus_python() is None:
        skip = pytest.mark.skip(reason="nautilus_trader not installed (set QT_NT_PYTHON or create .scratch/nt/venv)")
        for i in mine:
            i.add_marker(skip)


@pytest.fixture(scope="session")
def nt_python() -> str:
    py = nautilus_python()
    if py is None:
        pytest.skip("nautilus_trader not installed")
    return py
