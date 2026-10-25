"""Reading the ledger: filters, trial counts, variants, statistics, selection inputs and exports. Every read
hash- and link-checks each record first (`Ledger.scan`), so a tampered ledger raises `QT-LEDGER-001` instead of
returning a quietly wrong trial count."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
from pydantic import ValidationError

from queuetruth.errors import QtError
from queuetruth.ledger import _series
from queuetruth.ledger._capture import as_ledger
from queuetruth.ledger._store import Ledger, Scan
from queuetruth.ledger.models import (
    TRIAL_KINDS,
    Envelope,
    KindStats,
    LedgerDump,
    LedgerExport,
    LedgerFilter,
    LedgerList,
    LedgerRecord,
    LedgerStats,
    SelectionInputs,
    SelectionTrial,
    SeriesData,
    SeriesRef,
)
from queuetruth.schemas.io import sha256_file, write_table

LedgerLike = Ledger | str | os.PathLike[str] | None
FilterLike = LedgerFilter | Mapping[str, Any] | None

#: Arrow layout of the `.parquet` export (one row per selected record).
LEDGER_TABLE = pa.schema(
    [
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("hash", pa.utf8(), nullable=False),
        pa.field("prev", pa.utf8(), nullable=False),
        pa.field("recorded_at", pa.timestamp("ns", tz="UTC"), nullable=False),
        pa.field("started_at", pa.timestamp("ns", tz="UTC")),
        pa.field("kind", pa.utf8(), nullable=False),
        pa.field("status", pa.utf8(), nullable=False),
        pa.field("study", pa.utf8()),
        pa.field("trial_id", pa.utf8()),
        pa.field("run_id", pa.utf8()),
        pa.field("spec_hash", pa.utf8()),
        pa.field("dataset_hash", pa.utf8()),
        pa.field("name", pa.utf8()),
        pa.field("params_json", pa.utf8()),
        pa.field("inputs_json", pa.utf8(), nullable=False),
        pa.field("metrics_json", pa.utf8(), nullable=False),
        pa.field("series_kind", pa.utf8()),
        pa.field("series_sha256", pa.utf8()),
        pa.field("series", pa.list_(pa.float64())),
        pa.field("series_index", pa.list_(pa.int64())),
        pa.field("periods_per_year", pa.float64()),
        pa.field("higher_is_better", pa.bool_()),
        pa.field("run_ids", pa.list_(pa.utf8()), nullable=False),
        pa.field("code_queuetruth", pa.utf8()),
        pa.field("code_engine", pa.utf8()),
        pa.field("code_core", pa.utf8()),
        pa.field("code_git", pa.utf8()),
        pa.field("code_user", pa.utf8()),
        pa.field("error_code", pa.utf8()),
        pa.field("error_message", pa.utf8()),
        pa.field("note", pa.utf8()),
    ]
)


def _table_schema() -> pa.Schema:
    from queuetruth.schemas.arrow import ENGINE_VERSION

    return LEDGER_TABLE.with_metadata(
        {"qt.schema": "qt.ledger", "qt.schema_version": "1", "qt.engine_version": ENGINE_VERSION}
    )


# ---- filters --------------------------------------------------------------------------------------------------------


def as_filter(filter: FilterLike = None, **kwargs: Any) -> LedgerFilter:
    """`filter` (a `LedgerFilter` or a dict of its fields) updated by `kwargs`; `QT-CONFIG-001` when invalid."""
    base: dict[str, Any] = {}
    if isinstance(filter, LedgerFilter):
        base = filter.model_dump(mode="json")
    elif isinstance(filter, Mapping):
        base = dict(filter)
    elif filter is not None:
        raise QtError("QT-CONFIG-001", f"filter: expected a LedgerFilter or a dict, got {type(filter).__name__}")
    base.update({k: v for k, v in kwargs.items() if v is not None})
    if isinstance(base.get("kinds"), str):
        base["kinds"] = [base["kinds"]]
    try:
        return LedgerFilter.model_validate(base)
    except ValidationError as e:
        first = e.errors(include_url=False)[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise QtError("QT-CONFIG-001", f"filter: {loc + ': ' if loc else ''}{first.get('msg', '')}") from None


def matches(rec: LedgerRecord, f: LedgerFilter) -> bool:
    """True when `rec` is a trial-kind record that `f` selects."""
    if rec.kind == "genesis" or rec.kind not in (f.kinds or TRIAL_KINDS):
        return False
    if f.status != "all" and rec.status != f.status:
        return False
    if f.study is not None and not fnmatch.fnmatchcase(rec.study or "", f.study):
        return False
    if f.name is not None and (rec.name is None or not fnmatch.fnmatchcase(rec.name, f.name)):
        return False
    if f.since_ns is not None and rec.recorded_at_ns < f.since_ns:
        return False
    if f.until_ns is not None and rec.recorded_at_ns >= f.until_ns:
        return False
    if f.dataset is not None:
        hay = [rec.dataset_hash or "", *rec.inputs.values()]
        if not any(h.startswith(f.dataset) for h in hay):
            return False
    if f.run_id is not None:
        hay = [rec.run_id or "", *rec.run_ids]
        if not any(h.startswith(f.run_id) for h in hay):
            return False
    return f.spec_hash is None or (rec.spec_hash or "").startswith(f.spec_hash)


def _select(scan: Scan, f: LedgerFilter) -> list[tuple[LedgerRecord, str]]:
    return [(r, h) for r, h in zip(scan.records, scan.hashes, strict=True) if matches(r, f)]


def _scan(ledger: LedgerLike) -> tuple[Ledger, Scan]:
    led = as_ledger(ledger)
    return led, led.scan()


def _ids(scan: Scan) -> tuple[str, Any]:
    assert scan.ledger_id is not None and scan.head is not None
    return scan.ledger_id, scan.head


# ---- trials ---------------------------------------------------------------------------------------------------------


@dataclass
class _Trial:
    """The records of one `trial_id` and the distinct series they carry (in order of first appearance)."""

    trial_id: str
    records: list[LedgerRecord] = field(default_factory=list)
    series: list[SeriesRef] = field(default_factory=list)

    @property
    def units(self) -> int:
        """Trials this `trial_id` counts as: one per distinct series (a rerun that disagrees is another outcome),
        at least one."""
        return max(1, len(self.series))


@dataclass
class _Trials:
    trials: dict[str, _Trial]
    #: Runs left out because a selected variant links them, in order of first record.
    linked: list[str]

    @property
    def n(self) -> int:
        return sum(t.units for t in self.trials.values())

    @property
    def n_with_series(self) -> int:
        return sum(len(t.series) for t in self.trials.values())

    @property
    def conflicts(self) -> list[str]:
        return [tid for tid, t in self.trials.items() if len(t.series) > 1]


def _linked_ids(recs: list[LedgerRecord]) -> set[str]:
    return {rid for r in recs if r.kind == "variant" for rid in r.run_ids}


def _trials(recs: list[LedgerRecord], linked: set[str] | None = None) -> _Trials:
    """Groups the records that are trials by `trial_id`. A run whose `run_id` a variant in `linked` (default: the
    variants among `recs`) names in `run_ids` is part of that variant's trial and is left out."""
    linked = _linked_ids(recs) if linked is None else linked
    out: dict[str, _Trial] = {}
    dropped: dict[str, None] = {}
    for r in recs:
        if not r.trial_id:
            continue
        if r.kind != "variant" and r.run_id in linked:
            dropped[r.run_id] = None
            continue
        t = out.setdefault(r.trial_id, _Trial(r.trial_id))
        t.records.append(r)
        if r.series is not None and all(x.sha256 != r.series.sha256 for x in t.series):
            t.series.append(r.series)
    return _Trials(out, list(dropped))


# ---- queries --------------------------------------------------------------------------------------------------------


def list_records(
    filter: FilterLike = None, *, limit: int = 0, ledger: LedgerLike = None, **kwargs: Any
) -> list[LedgerRecord]:
    """The records `filter` selects (default: every record but genesis, failed runs included), oldest first; with
    `limit > 0` the newest `limit`."""
    return [r for r, _ in _limited(filter, limit, ledger, kwargs)[2]]


def _limited(
    filter: FilterLike, limit: int, ledger: LedgerLike, kwargs: dict[str, Any]
) -> tuple[Ledger, Scan, list[tuple[LedgerRecord, str]], int, LedgerFilter]:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise QtError("QT-CONFIG-001", f"limit: {limit!r} must be a non-negative integer (0 = all)")
    f = as_filter(filter, **kwargs)
    led, scan = _scan(ledger)
    sel = _select(scan, f)
    n = len(sel)
    return led, scan, (sel[-limit:] if limit else sel), n, f


def list_document(filter: FilterLike = None, *, limit: int = 0, ledger: LedgerLike = None) -> LedgerList:
    """`ledger list` as a `qt.ledger_list` document."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise QtError("QT-CONFIG-001", f"limit: {limit!r} must be a non-negative integer (0 = all)")
    f = as_filter(filter)
    led, scan = _scan(ledger)
    every = _select(scan, f)
    sel = every[-limit:] if limit else every
    lid, head = _ids(scan)
    return LedgerList(
        ledger=str(led.path),
        ledger_id=lid,
        head=head,
        filter=f,
        n_matching=len(every),
        n_conflicts=len(_trials([r for r, _ in every]).conflicts),
        records=[r for r, _ in sel],
    )


def n_trials(filter: FilterLike = None, *, ledger: LedgerLike = None, **kwargs: Any) -> int:
    """Trials `filter` selects (default: every kind and study; pass `study=` and `kinds=` for the count of one
    search). A rerun of the same spec on the same data (same `run_id`), or the same variant evaluation registered
    again, is one trial; a trial whose records carry k different series counts k times; a run that a selected
    variant links (`run_ids`) is part of that variant's trial; failed runs are none."""
    f = as_filter(filter, **kwargs)
    _, scan = _scan(ledger)
    return _trials([r for r, _ in _select(scan, f)]).n


def _kstats(recs: list[LedgerRecord], linked: set[str]) -> KindStats:
    return KindStats(
        records=len(recs),
        trials=_trials(recs, linked).n,
        configs=len({r.spec_hash for r in recs if r.spec_hash}),
        errors=sum(r.status == "error" for r in recs),
    )


def stats(filter: FilterLike = None, *, ledger: LedgerLike = None, **kwargs: Any) -> LedgerStats:
    """Counts of what `filter` selects: records, trials, configurations and errors, by kind and by study."""
    f = as_filter(filter, **kwargs)
    led, scan = _scan(ledger)
    recs = [r for r, _ in _select(scan, f)]
    lid, head = _ids(scan)
    linked = _linked_ids(recs)
    trials = _trials(recs, linked)
    total = _kstats(recs, linked)
    by_kind = {
        k: _kstats([r for r in recs if r.kind == k], linked) for k in TRIAL_KINDS if any(r.kind == k for r in recs)
    }
    studies = sorted({r.study or "" for r in recs})
    return LedgerStats(
        ledger=str(led.path),
        ledger_id=lid,
        head=head,
        filter=f,
        n_records=total.records,
        n_trials=total.trials,
        n_configs=total.configs,
        n_errors=total.errors,
        n_with_series=trials.n_with_series,
        n_conflicts=len(trials.conflicts),
        n_linked=len(trials.linked),
        by_kind=by_kind,
        by_study={s: _kstats([r for r in recs if (r.study or "") == s], linked) for s in studies},
        first_recorded_at_ns=recs[0].recorded_at_ns if recs else None,
        last_recorded_at_ns=recs[-1].recorded_at_ns if recs else None,
    )


def variants(filter: FilterLike = None, *, ledger: LedgerLike = None, **kwargs: Any) -> list[dict[str, Any]]:
    """The distinct registered variants `filter` selects (kind forced to `variant`), in order of first record:
    plain dicts with `trial_id`, `name`, `params`, `study`, `dataset`, `code_version`, `n_records`,
    `first_recorded_at_ns`, `last_recorded_at_ns`, `series_kind`, `n_obs` and `run_ids` (of the latest record)."""
    f = as_filter(filter, **kwargs).model_copy(update={"kinds": ["variant"]})
    _, scan = _scan(ledger)
    groups: dict[str, list[LedgerRecord]] = {}
    for r, _ in _select(scan, f):
        if r.trial_id:
            groups.setdefault(r.trial_id, []).append(r)
    out = []
    for tid, recs in groups.items():
        last = recs[-1]
        out.append(
            {
                "trial_id": tid,
                "name": last.name,
                "params": last.params,
                "study": last.study,
                "dataset": last.inputs.get("dataset", last.inputs.get("dataset_label")),
                "code_version": last.code.user if last.code else None,
                "n_records": len(recs),
                "first_recorded_at_ns": recs[0].recorded_at_ns,
                "last_recorded_at_ns": last.recorded_at_ns,
                "series_kind": last.series.kind if last.series else None,
                "n_obs": last.series.n_obs if last.series else 0,
                "run_ids": list(last.run_ids),
            }
        )
    return out


def selection_document(
    filter: FilterLike = None,
    *,
    series: str = "returns",
    allow_mixed: bool = False,
    ledger: LedgerLike = None,
    **kwargs: Any,
) -> SelectionInputs:
    """`selection_inputs` as a `qt.selection_inputs` model."""
    if series not in ("returns", "scores"):
        raise QtError("QT-CONFIG-001", f"series: {series!r} is not returns or scores")
    f = as_filter(filter, **kwargs)
    led, scan = _scan(ledger)
    return _selection(led, scan, f, series, allow_mixed)


def _selection(led: Ledger, scan: Scan, f: LedgerFilter, series: str, allow_mixed: bool) -> SelectionInputs:
    if f.kinds is None:  # the strategy variants of a search; runs (zoo, calibrate, ...) only when asked for
        f = f.model_copy(update={"kinds": ["variant"]})
    lid, head = _ids(scan)
    trials = _trials([r for r, _ in _select(scan, f)])
    kinds: dict[str, int] = {}
    studies: set[str] = set()
    for t in trials.trials.values():
        kinds[t.records[-1].kind] = kinds.get(t.records[-1].kind, 0) + t.units
        studies.update(r.study or "" for r in t.records)
    if not allow_mixed and (len(kinds) > 1 or len(studies) > 1):
        raise QtError(
            "QT-CONFIG-001",
            f"selection: the filter spans {len(studies)} studies ({', '.join(sorted(studies))}) and {len(kinds)} "
            f"kinds ({', '.join(sorted(kinds))}); a trial count is the count of one search",
            "filter by study (and kinds), or pass allow_mixed=True (--allow-mixed) to count them together",
        )
    entries: list[SelectionTrial] = []
    for t in trials.trials.values():
        last = t.records[-1]
        refs: list[SeriesRef | None] = [x for x in t.series if x.kind == series] or [None]
        for ref in refs:
            data: SeriesData | None = _series.load(led.directory, ref) if ref is not None else None
            entries.append(
                SelectionTrial(
                    trial_id=t.trial_id,
                    kind=last.kind,
                    study=last.study,
                    name=last.name,
                    run_id=last.run_id,
                    spec_hash=last.spec_hash,
                    dataset_hash=last.dataset_hash,
                    n_records=len(t.records),
                    first_recorded_at_ns=t.records[0].recorded_at_ns,
                    conflict=len(t.series) > 1,
                    series_sha256=ref.sha256 if ref else None,
                    series=list(data.values) if data else None,
                    index=list(data.index) if data and data.index is not None else None,
                    n_obs=ref.n_obs if ref else 0,
                    periods_per_year=ref.periods_per_year if ref else None,
                    higher_is_better=ref.higher_is_better if ref else None,
                )
            )
    first = entries[0].index if entries else None
    aligned = first is not None and all(e.index == first for e in entries)
    return SelectionInputs(
        ledger_id=lid,
        head=head,
        chain_verified=True,
        filter=f,
        series_kind=series,  # type: ignore[arg-type]
        n_trials=trials.n,
        n_configs=len({t.records[-1].spec_hash for t in trials.trials.values() if t.records[-1].spec_hash}),
        by_kind=kinds,
        studies=sorted(studies),
        n_with_series=sum(e.series is not None for e in entries),
        conflicts=trials.conflicts,
        linked_run_ids=trials.linked,
        aligned=aligned,
        index=first if aligned else None,
        trials=entries,
    )


def selection_inputs(
    filter: FilterLike = None,
    *,
    series: str = "returns",
    allow_mixed: bool = False,
    ledger: LedgerLike = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Plain data for selection statistics, from the trials of one search: `filter` (default kind: `variant`)
    must select one study and one kind unless `allow_mixed` (`QT-CONFIG-001` otherwise). Returns `n_trials`, the
    count to deflate by (every trial, with or without a series; a run linked by a selected variant counts with
    it), `n_configs`, `by_kind`, `studies` and, per trial in order of first record, its `series` of kind `series`
    (`returns` or `scores`; None when it has none) with its `index`, `n_obs`, `periods_per_year`,
    `higher_is_better` and `dataset_hash`. `aligned` (with the common `index`) says whether every trial's series
    sits on the same periods, which PBO, SPA and model confidence sets need; the Deflated Sharpe Ratio needs only
    `n_trials` and the trials' Sharpe ratios. Also `ledger_id`, `head` (the chain position it was read at),
    `chain_verified`, `conflicts` (trials whose reruns carry different series: every series is listed and counted)
    and `linked_run_ids`. JSON types only."""
    return selection_document(filter, series=series, allow_mixed=allow_mixed, ledger=ledger, **kwargs).model_dump(
        mode="json"
    )


# ---- export ---------------------------------------------------------------------------------------------------------


def _atomic_write(out: Path, data: bytes) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _series_data(led: Ledger, sel: list[tuple[LedgerRecord, str]]) -> dict[str, SeriesData]:
    out: dict[str, SeriesData] = {}
    for r, _ in sel:
        if r.series is not None and r.series.sha256 not in out:
            out[r.series.sha256] = _series.load(led.directory, r.series)
    return out


def _rows(sel: list[tuple[LedgerRecord, str]], data: Mapping[str, SeriesData]) -> pa.Table:
    def js(v: Any) -> str | None:
        return None if v is None else json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    cols: dict[str, list[Any]] = {f.name: [] for f in LEDGER_TABLE}
    for r, h in sel:
        s, c, e = r.series, r.code, r.error
        d = data[s.sha256] if s else None
        row = {
            "seq": r.seq,
            "hash": h,
            "prev": r.prev,
            "recorded_at": r.recorded_at_ns,
            "started_at": r.started_at_ns,
            "kind": r.kind,
            "status": r.status,
            "study": r.study,
            "trial_id": r.trial_id,
            "run_id": r.run_id,
            "spec_hash": r.spec_hash,
            "dataset_hash": r.dataset_hash,
            "name": r.name,
            "params_json": js(r.params),
            "inputs_json": js(r.inputs),
            "metrics_json": js(r.metrics),
            "series_kind": s.kind if s else None,
            "series_sha256": s.sha256 if s else None,
            "series": list(d.values) if d else None,
            "series_index": list(d.index) if d and d.index is not None else None,
            "periods_per_year": s.periods_per_year if s else None,
            "higher_is_better": s.higher_is_better if s else None,
            "run_ids": list(r.run_ids),
            "code_queuetruth": c.queuetruth if c else None,
            "code_engine": c.engine if c else None,
            "code_core": c.core if c else None,
            "code_git": c.git if c else None,
            "code_user": c.user if c else None,
            "error_code": e.code if e else None,
            "error_message": e.message if e else None,
            "note": r.note,
        }
        for k, v in row.items():
            cols[k].append(v)
    return pa.Table.from_pydict(cols, schema=_table_schema())


def export(
    out: str | os.PathLike[str],
    filter: FilterLike = None,
    *,
    selection: bool = False,
    series: str = "returns",
    allow_mixed: bool = False,
    ledger: LedgerLike = None,
    **kwargs: Any,
) -> LedgerExport:
    """Writes the ledger to `out`, the format chosen by its extension: `.jsonl` is a byte copy of the whole chain
    with its series files copied to `series/` next to it (verifiable on its own; no filter), `.json` a
    `qt.ledger_dump` of the selected records with their hashes and series, `.parquet` one row per selected record
    (Arrow layout `LEDGER_TABLE`); with `selection` (`.json` only) the `qt.selection_inputs` document. Everything
    comes from one read of the ledger; files are written atomically."""
    path = Path(out)
    ext = path.suffix.lower()
    if ext not in (".jsonl", ".json", ".parquet"):
        raise QtError("QT-CONFIG-001", f"out: {path.name!r} must end in .jsonl, .json or .parquet")
    if series not in ("returns", "scores"):
        raise QtError("QT-CONFIG-001", f"series: {series!r} is not returns or scores")
    f = as_filter(filter, **kwargs)
    if selection and ext != ".json":
        raise QtError("QT-CONFIG-001", "out: the selection inputs are written as .json")
    led = as_ledger(ledger)
    if ext == ".jsonl":
        if f != LedgerFilter():
            raise QtError(
                "QT-CONFIG-001",
                "filter: a .jsonl export copies the whole chain so that it verifies on its own; "
                "filters apply to .json and .parquet",
            )
        if path.resolve() == led.path.resolve():
            raise QtError("QT-CONFIG-001", "out: is the ledger itself")
        scan, data = led.snapshot()
        _copy_series(led, scan, path.parent)
        _atomic_write(path, data)
        n, fmt = len(scan.records), "jsonl"
    elif selection:
        scan = led.scan()
        doc = _selection(led, scan, f, series, allow_mixed)
        text = json.dumps(doc.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        _atomic_write(path, text.encode("utf-8"))
        n, fmt = doc.n_trials, "selection"
    else:
        scan = led.scan()
        sel = _select(scan, f)
        sdata = _series_data(led, sel)
        if ext == ".json":
            lid, head = _ids(scan)
            dump = LedgerDump(
                ledger_id=lid,
                head=head,
                filter=f,
                entries=[Envelope(hash=h, record=r) for r, h in sel],
                series=dict(sorted(sdata.items())),
            )
            text = json.dumps(dump.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
            _atomic_write(path, text.encode("utf-8"))
            fmt = "json"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_table(_rows(sel, sdata), path)
            fmt = "parquet"
        n = len(sel)
    lid, head = _ids(scan)
    return LedgerExport(
        ledger=str(led.path),
        ledger_id=lid,
        head=head,
        out=str(path),
        format=fmt,  # type: ignore[arg-type]
        n_records=n,
        bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )


def _copy_series(led: Ledger, scan: Scan, dest: Path) -> None:
    """Copies every series file the chain references to `dest/series/`, each checked first."""
    if dest.resolve() == led.directory.resolve():
        refs = []
    else:
        refs = list({r.series.sha256: r.series for r in scan.records if r.series is not None}.values())
    for ref in refs:
        _series.load(led.directory, ref)  # QT-LEDGER-001 when missing or altered
        target = _series.path_of(dest, ref.sha256)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_series.path_of(led.directory, ref.sha256), target)


def verify_chain(anchor: str | None = None, *, ledger: LedgerLike = None) -> Any:
    """The full integrity check (`qt.ledger_verification`): every line's hash, canonical form, schema, seq
    continuity, `prev` link and `ledger_id`, and, with `anchor` (`"<seq>:<hash>"` noted earlier, for example
    from `ledger verify` output), that the history up to it is unchanged. Never raises on a problem; read `ok`."""
    return as_ledger(ledger).verify(anchor)
