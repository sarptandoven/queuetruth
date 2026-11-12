"""`compare` as an operation: a backtest against the live run of the same strategy (`queuetruth.compare`). Its
`OPERATIONS` join the operation table (`queuetruth.ops.table`)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from queuetruth.ops.table import OperationDef, VerbDocument, ref_path, ref_paths
from queuetruth.schemas.specs import PathRef, SpecModel


class CompareSpec(SpecModel):
    """`compare`: the backtest's files, the live run (a canonical live log, a bot's records or a venue export) and
    optionally the order-level dataset of the same days. `window` is how long after entry a fill counts."""

    backtest: Annotated[list[PathRef], Field(min_length=1)]
    live: Annotated[list[PathRef], Field(min_length=1)]
    data: PathRef | None = None
    backtester: str | None = None
    venue: str | None = None
    instrument_map: PathRef | None = None
    window: str | None = None
    account: str | None = None
    out: str | None = None


def run_compare(spec: CompareSpec) -> Any:
    """`queuetruth.compare.compare` with the spec's fields."""
    from queuetruth.compare import compare

    return compare(
        ref_paths(spec.backtest),
        ref_paths(spec.live),
        data=ref_path(spec.data),
        backtester=spec.backtester,
        venue=spec.venue,
        out=spec.out,
        instrument_map=ref_path(spec.instrument_map),
        window=spec.window,
        account=spec.account,
    )


OPERATIONS = (
    OperationDef(
        "compare",
        CompareSpec,
        run_compare,
        VerbDocument,
        "A backtest against the live run of the same strategy, order by order: fills on one side only, the P&L "
        "gap, time to fill; returns qt.compare.",
        cost="minutes",
        tool="qt_compare",
        title="Compare backtest and live",
    ),
)
