"""`demo` as an operation: a real-data answer in about a minute (`queuetruth.demo`). Its `OPERATIONS` join the
operation table (`queuetruth.ops.table`)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from queuetruth.ops.table import NETWORK_ALLOWED, OperationDef, ref_path
from queuetruth.schemas.demo import DemoSummary
from queuetruth.schemas.specs import PathRef, SpecModel


class DemoSpec(SpecModel):
    """`demo`: the backtester the answer is about, the window (`minutes`), the symbols and the number of shadow
    orders. `from_file` is a local order-level file to use instead of the public sample (which needs the
    network)."""

    backtester: str | None = None
    source: str | None = None
    symbols: list[str] | None = None
    minutes: Annotated[int, Field(ge=1)] | None = None
    n: Annotated[int, Field(ge=1)] | None = None
    seed: int = 0
    threads: Annotated[int, Field(ge=1)] | None = None
    bootstrap_b: Annotated[int, Field(ge=1)] | None = None
    from_file: PathRef | None = None
    input_format: str | None = None
    date: str | None = None
    out: str | None = None


def run_demo(spec: DemoSpec) -> Any:
    """`queuetruth.demo.run_demo` with the spec's fields (unset ones keep the demo's defaults), as the `DemoResult`
    `qt.demo` returns."""
    from queuetruth.demo import DemoOptions
    from queuetruth.demo import run_demo as _run
    from queuetruth.demo.result import DemoResult

    fields: dict[str, Any] = {
        k: v
        for k, v in {
            "backtester": spec.backtester,
            "source": spec.source,
            "symbols": spec.symbols,
            "minutes": spec.minutes,
            "threads": spec.threads,
            "n": spec.n,
            "bootstrap_b": spec.bootstrap_b,
            "from_file": ref_path(spec.from_file),
            "input_format": spec.input_format,
            "date": spec.date,
            "out": spec.out,
        }.items()
        if v is not None
    }
    return DemoResult(_run(DemoOptions(seed=spec.seed, yes=NETWORK_ALLOWED.get(), **fields)))


def _demo_needs_network(spec: DemoSpec) -> bool:
    return spec.from_file is None


OPERATIONS = (
    OperationDef(
        "demo",
        DemoSpec,
        run_demo,
        DemoSummary,
        "The demo: shadow orders in the exact queue of a few minutes of order-level data, and how far the "
        "backtester's default fill model is from them; returns qt.demo_summary.",
        network=True,
        needs_network=_demo_needs_network,
        cost="minutes",
        tool="qt_demo",
        title="Run the demo",
    ),
)
