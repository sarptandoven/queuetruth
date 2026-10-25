"""Render context shared by the section builders: the loaded inputs, the spec and a deterministic id counter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from queuetruth.eval.leaderboard import DEFAULT_SORT_HORIZON_NS
from queuetruth.report.load import Inputs, RunDir
from queuetruth.schemas.specs import ReportSpec

#: Row caps that keep the page bounded (SPEC 5.9: <= 10 MB) whatever the input size.
MAX_TEST_ROWS = 200
MAX_POSITION_POINTS = 2000
MAX_POSITION_TABLE_ROWS = 200
MAX_POSITION_INSTRUMENTS = 12
MAX_WHY_EVENTS = 300
#: Reliability diagrams of the first N models in leaderboard order are shown open; the rest are collapsed.
OPEN_RELIABILITY_MODELS = 1


@dataclass
class Ctx:
    """State of one render."""

    spec: ReportSpec
    inputs: Inputs
    compact: bool = False
    #: Horizon (ns) the zoo leaderboard is sorted at by absolute fill-rate bias (trader review C1; default 60 s; the
    #: longest horizon below it when absent).
    sort_horizon_ns: int = DEFAULT_SORT_HORIZON_NS
    _n: int = 0

    def uid(self) -> str:
        """Next unique element id (`c1`, `c2`, ...), in document order."""
        self._n += 1
        return f"c{self._n}"

    def runs_with(self, cls: type) -> list[tuple[RunDir, Any]]:
        """(run, document) pairs for every document of `cls`, in input order."""
        return [(r, d) for r in self.inputs.runs for d in r.docs_of(cls)]

    def price_exp(self, instrument_id: int | None) -> int | None:
        """Price exponent of an instrument when a dataset manifest resolved it unambiguously."""
        if instrument_id is None:
            return None
        return self.inputs.provenance.price_exp.get(int(instrument_id))
