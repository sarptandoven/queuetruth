"""`check` as an operation: how wrong is a backtest, in one call (`queuetruth.check`). Its `OPERATIONS` join the
operation table (`queuetruth.ops.table`)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from queuetruth.ops.table import NETWORK_ALLOWED, OperationDef, VerbDocument, ref_path, ref_paths
from queuetruth.schemas.specs import PathRef, SpecModel


class CheckSpec(SpecModel):
    """`check`: the backtest's files, and the order-level dataset to replay them in (without it, the atlas
    estimate for a bar backtest). Durations are written as `1h`, `30s`, `250ms`."""

    backtest: Annotated[list[PathRef], Field(min_length=1)]
    data: PathRef | None = None
    backtester: str | None = None
    instrument_map: PathRef | None = None
    window: str = "1h"
    tier: str | None = None
    bar: str | None = None
    atlas: PathRef | None = None
    seed: int | None = None
    out: str | None = None


def run_check(spec: CheckSpec) -> Any:
    """`queuetruth.check.check` with the spec's fields."""
    from queuetruth.check import check

    return check(
        ref_paths(spec.backtest),
        ref_path(spec.data),
        backtester=spec.backtester,
        instrument_map=ref_path(spec.instrument_map),
        out=spec.out,
        window=spec.window,
        tier=spec.tier,
        bar=spec.bar,
        atlas=ref_path(spec.atlas),
        yes=NETWORK_ALLOWED.get(),
        seed=spec.seed,
    )


def _check_needs_network(spec: CheckSpec) -> bool:
    return spec.data is None and spec.atlas is None


OPERATIONS = (
    OperationDef(
        "check",
        CheckSpec,
        run_check,
        VerbDocument,
        "How wrong is a backtest: its own resting limit orders replayed in the exact queue of an order-level "
        "dataset (or, without data, the atlas estimate); returns qt.check.",
        network=True,
        needs_network=_check_needs_network,
        cost="minutes",
        tool="qt_check",
        title="Check a backtest",
    ),
)
