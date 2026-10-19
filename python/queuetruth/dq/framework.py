"""Check plumbing: the shared context, the `Check` base class (one pass over the partitions, then `finish`) and
evidence tables capped at `max_evidence` rows."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from queuetruth.dq.data import Dataset, Part
from queuetruth.dq.model import DqCheck, DqEvidence, DqSpec

#: Ratio above which a counted anomaly fails a check (the SPEC 5.1.5 `unusable` threshold).
FAIL_RATIO = 1e-3


def jsonable(v: Any) -> Any:
    """Plain JSON scalar of a numpy / Python value (non-finite floats -> None)."""
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.decode("ascii", "replace")
    return v


def ratio(n: int, d: int) -> float:
    """`n / d` with a zero denominator counted as 1 (SPEC 5.1.5)."""
    return n / max(d, 1)


def chr_or(v: Any) -> str:
    """An action / side byte as its ASCII letter."""
    try:
        return chr(int(v))
    except (TypeError, ValueError):
        return str(v)


class Evidence:
    """An evidence table: every row counts toward `total`, the first `cap` rows are kept."""

    def __init__(self, name: str, columns: Sequence[str], cap: int) -> None:
        self.name = name
        self.columns = list(columns)
        self.cap = cap
        self.rows: list[list[Any]] = []
        self.total = 0

    def add(self, row: Sequence[Any]) -> None:
        self.total += 1
        if len(self.rows) < self.cap:
            self.rows.append([jsonable(x) for x in row])

    def extend(self, rows: Iterable[Sequence[Any]], total: int | None = None) -> None:
        """Adds rows; with `total`, only the first rows are consumed and `total` is added to the count."""
        if total is None:
            for r in rows:
                self.add(r)
            return
        for r in rows:
            if len(self.rows) >= self.cap:
                break
            self.rows.append([jsonable(x) for x in r])
        self.total += total

    @property
    def room(self) -> int:
        return max(self.cap - len(self.rows), 0)

    def model(self) -> DqEvidence:
        return DqEvidence(
            name=self.name,
            columns=self.columns,
            rows=self.rows,
            total=self.total,
            truncated=self.total > len(self.rows),
        )


@dataclass
class ReplayOut:
    """The exact replay run for the book checks: its summary document and output directory."""

    summary: dict[str, Any]
    out: Path


@dataclass
class Ctx:
    """Everything a check may read."""

    spec: DqSpec
    ds: Dataset
    replay: ReplayOut | None = None
    replay_error: str | None = None
    compare: Dataset | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def cap(self) -> int:
        return self.spec.max_evidence


class Check:
    """One check: `partition` is called once per partition in `(session_date, instrument_id)` order (MBO datasets),
    then `finish` builds the result. `skip_reason` (set before the pass) marks the check `skip`."""

    id: str = ""
    title: str = ""
    #: Reads MBO partitions (skipped on L2 datasets).
    mbo_only: bool = True

    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.ds = ctx.ds
        self.cap = ctx.cap
        self.skip_reason: str | None = None
        if self.mbo_only and self.ds.kind != "mbo":
            self.skip_reason = f"needs an MBO dataset (this one is kind {self.ds.kind!r})"

    def evidence(self, name: str, columns: Sequence[str]) -> Evidence:
        return Evidence(name, columns, self.cap)

    def partition(self, p: Part) -> None:
        """Accumulates one partition."""

    def finish(self) -> DqCheck:
        raise NotImplementedError

    def result(
        self,
        status: str,
        summary: str,
        metrics: dict[str, Any] | None = None,
        evidence: Iterable[Evidence] = (),
        notes: Iterable[str] = (),
    ) -> DqCheck:
        return DqCheck(
            id=self.id,
            title=self.title,
            status=status,  # type: ignore[arg-type]
            summary=summary,
            metrics={k: jsonable(v) for k, v in (metrics or {}).items()},
            evidence=[e.model() for e in evidence if e.total or e.rows],
            notes=[str(n) for n in notes],
        )

    def skipped(self, reason: str | None = None) -> DqCheck:
        return self.result("skip", f"not run: {reason or self.skip_reason}")


def level(n_bad: int, denom: int, *, fail_ratio: float | None = FAIL_RATIO) -> str:
    """`pass` for 0, `fail` above `fail_ratio` of `denom` (None = never fail), else `warn`."""
    if n_bad <= 0:
        return "pass"
    if fail_ratio is not None and ratio(n_bad, denom) > fail_ratio:
        return "fail"
    return "warn"


def percentiles(x: np.ndarray, qs: Sequence[float]) -> list[float | None]:
    """Percentiles (nearest-rank on the sorted values, deterministic); None for an empty array."""
    if x.size == 0:
        return [None for _ in qs]
    s = np.sort(x)
    out: list[float | None] = []
    for q in qs:
        k = min(max(math.ceil(q / 100.0 * s.size) - 1, 0), s.size - 1)
        out.append(float(s[k]))
    return out


__all__ = [
    "FAIL_RATIO",
    "Check",
    "Ctx",
    "Evidence",
    "ReplayOut",
    "chr_or",
    "jsonable",
    "level",
    "percentiles",
    "ratio",
]
