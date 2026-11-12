"""`atlas` as an operation: the Fill-Realism Atlas answer for a library, symbol or tier (`queuetruth.atlas`). Its
`OPERATIONS` join the operation table (`queuetruth.ops.table`)."""

from __future__ import annotations

from typing import Any

from queuetruth.atlas.answer import AtlasAnswer
from queuetruth.ops.table import NETWORK_ALLOWED, OperationDef, ref_path
from queuetruth.schemas.specs import PathRef, SpecModel


class AtlasSpec(SpecModel):
    """`atlas`: the library (`hftbacktest`, `nautilus`, `databento`; all by default), one symbol or a liquidity
    tier, the horizon (`1min`) and `source`, a local copy of the atlas data API (default: the public atlas, which
    needs the network)."""

    library: str | None = None
    symbol: str | None = None
    tier: str | None = None
    horizon: str | None = None
    source: PathRef | None = None


def run_atlas(spec: AtlasSpec) -> Any:
    """`queuetruth.atlas.lookup` with the spec's fields."""
    from queuetruth.atlas import lookup

    return lookup(
        library=spec.library,
        symbol=spec.symbol,
        tier=spec.tier,
        horizon=spec.horizon,
        source=ref_path(spec.source),
        yes=NETWORK_ALLOWED.get(),
    )


def _atlas_needs_network(spec: AtlasSpec) -> bool:
    return spec.source is None


OPERATIONS = (
    OperationDef(
        "atlas",
        AtlasSpec,
        run_atlas,
        AtlasAnswer,
        "The atlas answer: how far each setting of a backtester's fill model is from the exact queue, with its "
        "95% interval and the closest setting; returns qt.atlas_answer.",
        writes=False,
        network=True,
        needs_network=_atlas_needs_network,
        cost="instant",
        out_field=None,
        tool="qt_atlas",
        title="Read the atlas",
    ),
)
