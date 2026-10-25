"""Every ledger test runs against its own ledger directory (`QT_LEDGER_DIR`) with recording on and no study set."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`QT_LEDGER_DIR` for the test (not created)."""
    d = tmp_path / "ledger"
    monkeypatch.setenv("QT_LEDGER_DIR", str(d))
    for var in ("QT_LEDGER", "QT_LEDGER_STUDY", "QT_LEDGER_LOCK_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    return d
