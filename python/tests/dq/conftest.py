"""Fixtures of the `queuetruth dq` tests: writable copies of the committed synthetic datasets."""

from __future__ import annotations

from pathlib import Path

import pytest

from dq.helpers import copy_fixture


@pytest.fixture
def nasdaq(tmp_path: Path) -> Path:
    """A writable copy of `tests/fixtures/synth_small_nasdaq` (2 dates x 2 instruments, clean)."""
    return copy_fixture("synth_small_nasdaq", tmp_path / "nasdaq")


@pytest.fixture
def cme(tmp_path: Path) -> Path:
    """A writable copy of `tests/fixtures/synth_small` (CME rules, snapshots with clears, clean)."""
    return copy_fixture("synth_small", tmp_path / "cme")
