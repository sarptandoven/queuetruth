"""Shared fixtures of the hftbacktest integration tests.

The integration lives outside the `queuetruth` package; its modules are imported from the parent directory. Tests
that need hftbacktest run it in a separate venv through a subprocess (never in the queuetruth venv): set
`QT_HBT_PY` to the python of a venv with the released hftbacktest 2.4.4 and `QT_HBT_PY_312` to one with the
#312-patched build (defaults: `<repo>/.scratch/hbt-venv/bin/python` and `<repo>/.scratch/hbt-venv-312/bin/python`,
created by `integrations/hftbacktest/setup_venvs.sh`). Missing venvs skip those tests with the reason.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

INTEG = Path(__file__).resolve().parents[1]
REPO = INTEG.parents[1]
if str(INTEG) not in sys.path:
    sys.path.insert(0, str(INTEG))


def _venv(env: str, default: Path) -> Path | None:
    p = Path(os.environ.get(env, str(default)))
    return p if p.exists() else None


@pytest.fixture(scope="session")
def hbt_stock_py() -> Path:
    p = _venv("QT_HBT_PY", REPO / ".scratch" / "hbt-venv" / "bin" / "python")
    if p is None:
        pytest.skip("released hftbacktest venv missing (set QT_HBT_PY or run integrations/hftbacktest/setup_venvs.sh)")
    return p


@pytest.fixture(scope="session")
def hbt_patched_py() -> Path:
    p = _venv("QT_HBT_PY_312", REPO / ".scratch" / "hbt-venv-312" / "bin" / "python")
    if p is None:
        pytest.skip("#312-patched hftbacktest venv missing (set QT_HBT_PY_312 or run upstream/verify.sh)")
    return p
