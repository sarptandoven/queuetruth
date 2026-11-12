"""Shared fixtures of the queuetruth-hftbacktest tests. Everything is synthetic: the quickstart dataset of
`queuetruth fixtures synth` (CME rules, 2 instruments, 2 session days) and shadows sampled on it, made once per
session in a temporary directory.

Tests that run hftbacktest itself use a separate venv (never this one): `QT_HBT_PY` names its python (default
`<repo>/.scratch/hbt-venv/bin/python`, created by `integrations/hftbacktest/setup_venvs.sh`). Without it they skip
with the reason.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
if importlib.util.find_spec("queuetruth_hftbacktest") is None:  # run from a checkout without installing
    sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))


@pytest.fixture(scope="session")
def synth(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import queuetruth as qt

    out = tmp_path_factory.mktemp("synth") / "synth"
    qt.fixtures_synth(str(out))
    return out


@pytest.fixture(scope="session")
def shadows(synth: Path) -> Path:
    import queuetruth as qt

    p = synth.parent / "shadows.parquet"
    qt.sample_shadows({"dataset": {"path": str(synth)}, "n": 200, "seed": 0, "out": str(p)})
    return p


@pytest.fixture(scope="session")
def prepared(synth: Path, shadows: Path):
    from queuetruth_hftbacktest import prepare

    return prepare(synth, shadows, synth.parent / "prep")


@pytest.fixture(scope="session")
def hbt_py() -> Path:
    default = REPO / ".scratch" / "hbt-venv" / "bin" / "python"
    # QT_HBT_PYTHON: the older name, still read
    p = Path(os.environ.get("QT_HBT_PY") or os.environ.get("QT_HBT_PYTHON") or str(default))
    if not p.exists():
        pytest.skip("hftbacktest venv missing (set QT_HBT_PY or run integrations/hftbacktest/setup_venvs.sh)")
    return p
