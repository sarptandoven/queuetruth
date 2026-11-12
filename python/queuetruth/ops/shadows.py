"""`shadows sample`, `shadows mirror-real` and `shadows from-live`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from queuetruth.ledger import recorded as _ledger_recorded
from queuetruth.ops._call import _native, _validate
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import write_table


@_ledger_recorded("shadows-sample")
def sample_shadows(spec: s.ShadowSampleSpec | dict[str, Any]) -> m.ShadowSampleSummary:
    """`shadows sample`."""
    return _native("sample_shadows", _validate(s.ShadowSampleSpec, spec), m.ShadowSampleSummary)


def mirror_real_orders(spec: s.MirrorSpec | dict[str, Any]) -> m.ShadowSampleSummary:
    """`shadows mirror-real`."""
    return _native("mirror_real_orders", _validate(s.MirrorSpec, spec), m.ShadowSampleSummary)


def shadows_from_live(
    live: str, out: str, *, entry: str = "exchange_ack", latency_ns: int | None = None
) -> m.ShadowSampleSummary:
    """`shadows from-live`: the live log's schedule as `qt.shadow_spec` at `out`; priority-keeping amends (if any)
    go to `<out stem>_amends.parquet` next to it."""
    from queuetruth import live as qlive

    entry = entry.replace("-", "_")
    spec_t, amend_t = qlive.to_schedule(str(live), entry=entry, latency_ns=latency_ns)
    out_p = Path(out)
    files = [m.FileEntry.model_validate(write_table(spec_t, out_p, SCHEMAS["qt.shadow_spec"]))]
    if amend_t.num_rows:
        amend_p = out_p.with_name(out_p.stem + "_amends.parquet")
        files.append(m.FileEntry.model_validate(write_table(amend_t, amend_p, SCHEMAS["qt.shadow_amend"])))
    by_inst: dict[str, int] = {}
    for v in spec_t.column("instrument_id").to_pylist():
        by_inst[str(v)] = by_inst.get(str(v), 0) + 1
    by_place: dict[str, int] = {}
    for v in spec_t.column("offset_ticks").to_pylist():
        by_place[str(v)] = by_place.get(str(v), 0) + 1
    echo = {"live": {"path": str(live)}, "entry": entry, "latency_ns": latency_ns}
    return m.ShadowSampleSummary(
        spec=echo,
        n=spec_t.num_rows,
        by_instrument=dict(sorted(by_inst.items(), key=lambda kv: int(kv[0]))),
        by_placement=dict(sorted(by_place.items(), key=lambda kv: int(kv[0]))),
        files=files,
    )
