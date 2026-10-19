"""Loader for the committed reference fixtures in tests/fixtures/reference (MANIFEST-checked)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REF_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "reference"


def _manifest() -> dict[str, str]:
    out = {}
    for line in (REF_DIR / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        out[name] = digest
    return out


@pytest.fixture(scope="session")
def reference() -> Callable[[str], Any]:
    """`reference("mcnemar.json")` -> parsed fixture, after checking its sha256 against MANIFEST.sha256."""
    manifest = _manifest()

    def load(name: str) -> Any:
        raw = (REF_DIR / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == manifest[name], f"{name} does not match MANIFEST.sha256"
        return json.loads(raw)

    return load
