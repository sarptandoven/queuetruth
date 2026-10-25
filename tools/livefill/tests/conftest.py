"""Test setup for tools/livefill: puts the kit and this directory on sys.path; a shared synthetic capture."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
for p in (KIT, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(scope="session")
def synth_default(tmp_path_factory: pytest.TempPathFactory):
    """The default synthetic capture (seed 7) with its harness log, built once per session."""
    from livefill import synth

    return synth.build(tmp_path_factory.mktemp("synth7"))
