"""Shared helpers of the forecast tests: a fake clock and quick ledger builders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from queuetruth.forecast import ledger as lg
from queuetruth.forecast.schemas import ForecastEntry

T0 = 1_800_000_000_000_000_000  # 2027-01-15T08:00:00Z
SEC = 1_000_000_000
HOUR = 3600 * SEC


class Clock:
    """A settable clock in ns."""

    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def nonce(i: int) -> str:
    """A deterministic 64-hex nonce."""
    return f"{i:064x}"


def fc(qid: str, who: str, i: int, **kw: Any) -> dict[str, Any]:
    """A forecast input row with a deterministic nonce (`p=` or another shape in kw)."""
    return {"question_id": qid, "forecaster": who, "nonce": nonce(i), **kw}


def commit(path: Path, rows: list[dict[str, Any]], clock: Clock, **kw: Any) -> list[ForecastEntry]:
    """Normalises forecast rows and appends them at the clock's time."""
    pls = [lg.normalize_forecast(r, f"row {i}") for i, r in enumerate(rows)]
    return lg.append(path, pls, clock=clock, **kw)


def resolve(path: Path, rows: list[dict[str, Any]], clock: Clock, **kw: Any) -> list[ForecastEntry]:
    """Normalises resolution rows and appends them at the clock's time."""
    pls = [lg.normalize_resolution(r, f"row {i}") for i, r in enumerate(rows)]
    return lg.append(path, pls, clock=clock, **kw)
