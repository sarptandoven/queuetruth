"""`fit` as an operation: the parameter that fixes a backtester's fill model (`queuetruth.fit`). Its `OPERATIONS`
join the operation table (`queuetruth.ops.table`)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from queuetruth.ops.table import OperationDef, VerbDocument, ref_path
from queuetruth.schemas.specs import PathRef, SpecModel


class FitSpec(SpecModel):
    """`fit`: the backtester, the order-level dataset (and shadows or live fills), and the held-out split
    (`split` such as `70%`, or `test_after` a venue-local time such as `09:42`)."""

    backtester: str
    data: PathRef | None = None
    shadows: PathRef | None = None
    live: PathRef | None = None
    family: str | None = None
    split: str | None = None
    test_after: str | None = None
    n_shadows: Annotated[int, Field(ge=1)] = 2000
    seed: int = 0
    threads: Annotated[int, Field(ge=1)] = 1
    out: str | None = None


def run_fit(spec: FitSpec) -> Any:
    """`queuetruth.fit.fit` with the spec's fields."""
    from queuetruth.fit import fit

    kwargs: dict[str, Any] = {"out": spec.out} if spec.out is not None else {}
    return fit(
        spec.backtester,
        data=ref_path(spec.data),
        shadows=ref_path(spec.shadows),
        live=ref_path(spec.live),
        family=spec.family,
        split=spec.split,
        test_after=spec.test_after,
        n_shadows=spec.n_shadows,
        seed=spec.seed,
        threads=spec.threads,
        **kwargs,
    )


OPERATIONS = (
    OperationDef(
        "fit",
        FitSpec,
        run_fit,
        VerbDocument,
        "The parameter that fixes a backtester's fill model, fitted on order-level data or live fills and checked "
        "against the library default on held-out data; returns qt.fit.",
        cost="minutes",
        tool="qt_fit",
        title="Fit a fill model",
    ),
)
