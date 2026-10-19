"""`queuetruth dq`: a data-quality battery for order-level (MBO) datasets, with a `qt.dq_report` JSON document and
a self-contained HTML page.

`run(spec)` checks one ingested dataset (ITCH, DBN, etcsv, crypto L3, IEX: any `queuetruth ingest` output) and,
with `spec.compare`, a second dataset of the same sessions. Each check reports `pass`, `warn`, `fail` or `skip`, the
numbers behind the status and the first `max_evidence` offending rows. Checks, in report order:

- `dq_counters`: the `qt.data_quality` counters and verdict (SPEC 5.1.5), from the replay when it ran, else ingest.
- `sequence`: gaps, regressions, cross-instrument collisions per `(session_date, channel_id)`; exact duplicates.
- `crossed_book`: crossed and locked books at event end (replay).
- `stale_book`: quiet book intervals while trades print; prints through the standing book (replay).
- `orphan_refs`: cancels, fills and modifies of orders never added (scan) and the replay's unknown references.
- `size_price`: zero and overflowing sizes, undefined and non-positive prices, resting prices off the tick grid.
- `trade_parity`: replay-derived trades against `trades_source` (SPEC 5.1.7).
- `timestamps`: `ts_event` regressions, `ts_recv - ts_event` distribution, negative latency and outliers.
- `session_coverage`: data before the open and after the close, late starts, early ends, 00:00 UTC truncation.
- `symbology`: one instrument id and one set of units per symbol across days, and the reverse.
- `snapshots`: snapshot runs, clears, `MAYBE_BAD_BOOK`, records before the first snapshot.
- `vintage`: partition file hashes and `dataset_id` recomputed, recorded inputs, DBN source vintage.
- `cross_source`: record-level parity with `spec.compare`.
- `rate_drift`: per-day record counts against each symbol's median.

The book checks run the exact replay (`queuetruth.api.replay`, `bbo = change`, `parity_trades` when the dataset has
source prints) into a temporary directory; `spec.replay = False` skips them. Everything else reads the dataset's
Parquet files directly; nothing in the dataset is modified.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from queuetruth._version import __version__
from queuetruth.dq.book import CrossedBookCheck, DqCountersCheck, StaleBookCheck, TradeParityCheck
from queuetruth.dq.compare import CrossSourceCheck
from queuetruth.dq.data import Dataset
from queuetruth.dq.feed import (
    OrphanCheck,
    RateDriftCheck,
    SequenceCheck,
    SessionCheck,
    SizePriceCheck,
    SnapshotCheck,
    TimestampCheck,
)
from queuetruth.dq.framework import Check, Ctx, ReplayOut
from queuetruth.dq.html import render_html
from queuetruth.dq.meta import SymbologyCheck, VintageCheck
from queuetruth.dq.model import (
    DqCheck,
    DqCounts,
    DqDataset,
    DqReport,
    DqSpec,
    gate,
    json_schemas,
    worst,
)
from queuetruth.errors import QtError
from queuetruth.schemas.models import FileEntry

#: Every check, in report order.
CHECKS: list[type[Check]] = [
    DqCountersCheck,
    SequenceCheck,
    CrossedBookCheck,
    StaleBookCheck,
    OrphanCheck,
    SizePriceCheck,
    TradeParityCheck,
    TimestampCheck,
    SessionCheck,
    SymbologyCheck,
    SnapshotCheck,
    VintageCheck,
    CrossSourceCheck,
    RateDriftCheck,
]
CHECK_IDS: list[str] = [c.id for c in CHECKS]
#: Checks that need the exact replay (the others use it when it ran).
REPLAY_CHECKS = frozenset({"crossed_book", "stale_book", "orphan_refs", "trade_parity"})
JSON_NAME = "dq_report.json"
HTML_NAME = "dq_report.html"


def _spec(spec: DqSpec | dict[str, Any]) -> DqSpec:
    if isinstance(spec, DqSpec):
        sp = spec
    else:
        try:
            sp = DqSpec.model_validate(spec)
        except ValidationError as e:
            raise QtError("QT-CONFIG-001", f"invalid DqSpec: {e}") from e
    if sp.checks is not None:
        unknown = [c for c in sp.checks if c not in CHECK_IDS]
        if unknown:
            raise QtError(
                "QT-CONFIG-001",
                f"checks: unknown check id(s) {', '.join(unknown)}",
                "valid ids: " + ", ".join(CHECK_IDS),
            )
    return sp


def _replay(ctx: Ctx, tmp: Path) -> None:
    from queuetruth import api

    out = tmp / "replay"
    spec = {
        "dataset": {"path": str(ctx.ds.root)},
        "out": str(out),
        "bbo": "change",
        "parity_trades": ctx.ds.has_trades_source,
    }
    try:
        summary = api.replay(spec)
    except QtError as e:
        ctx.replay_error = f"replay failed: {e}"
        ctx.warnings.append(f"the exact replay failed, book checks skipped: {e}")
        return
    ctx.replay = ReplayOut(summary=summary.model_dump(mode="json", by_alias=True), out=out)


def _file_entry(path: Path) -> FileEntry:
    data = path.read_bytes()
    return FileEntry(name=path.name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


def run(spec: DqSpec | dict[str, Any]) -> DqReport:
    """Runs the checks of `spec` and returns the `qt.dq_report`; with `spec.out`, also writes `dq_report.json` and
    `dq_report.html` there (`files` lists the HTML page)."""
    sp = _spec(spec)
    ds = Dataset(sp.dataset.path)
    other = Dataset(sp.compare.path) if sp.compare is not None else None
    ctx = Ctx(spec=sp, ds=ds, compare=other)
    if not ds.rules_known:
        ctx.warnings.append(
            f"unknown rule set {ds.rule_set_name!r}: venue rules of 'generic' used by the record checks"
        )
    selected = [c for c in CHECKS if sp.checks is None or c.id in sp.checks]
    with tempfile.TemporaryDirectory(prefix="qt-dq-") as tmp:
        if sp.replay and ds.kind == "mbo" and any(c.id in REPLAY_CHECKS for c in selected):
            _replay(ctx, Path(tmp))
        checks = [cls(ctx) for cls in selected]
        streaming = [c for c in checks if c.mbo_only and not c.skip_reason and type(c).partition is not Check.partition]
        if streaming:
            for part in ds.iter_parts():
                for c in streaming:
                    c.partition(part)
        results: list[DqCheck] = [c.finish() for c in checks]
    statuses = [r.status for r in results]
    status = worst(statuses)
    counts = DqCounts(
        **{"pass": statuses.count("pass"), "warn": statuses.count("warn"), "fail": statuses.count("fail")},
        skip=statuses.count("skip"),
    )
    rep = DqReport(
        engine_version=__version__,
        spec=sp.model_dump(mode="json"),
        dataset=DqDataset(**ds.describe()),
        compare=DqDataset(**other.describe()) if other is not None else None,
        status=status,  # type: ignore[arg-type]
        gate_failed=gate(status, sp.fail_on),
        counts=counts,
        checks=results,
        files=[],
        warnings=ctx.warnings,
    )
    if sp.out:
        write(rep, sp.out, title=sp.title)
    return rep


def write(rep: DqReport, out: str | Path, *, title: str | None = None) -> DqReport:
    """Writes `dq_report.html` and `dq_report.json` into `out` (created) and sets `rep.files`."""
    d = Path(out)
    try:
        d.mkdir(parents=True, exist_ok=True)
        html = d / HTML_NAME
        html.write_text(render_html(rep, title), encoding="utf-8")
        rep.files = [_file_entry(html)]
        doc = rep.model_dump(mode="json", by_alias=True)
        (d / JSON_NAME).write_text(
            json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
        )
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{out}: cannot write the report: {e}") from e
    return rep


__all__ = [
    "CHECKS",
    "CHECK_IDS",
    "HTML_NAME",
    "JSON_NAME",
    "DqReport",
    "DqSpec",
    "json_schemas",
    "render_html",
    "run",
    "write",
]
