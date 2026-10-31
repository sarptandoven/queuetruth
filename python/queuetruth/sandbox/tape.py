"""The structural no-look-ahead gate: a forward-only cut of one source partition into a scratch dataset.

`SourceDataset` opens a dataset directory (SPEC 3.3) and resolves the instrument-day to trade; every file is resolved
to its real path (and, in MCP mode, checked against the server root) before it is read, and the partition's files are
opened later only through those real paths. `SourceCursor` reads that partition's `qt.mbo` files in feed order, batch
by batch, and releases records only up to a cut: the longest prefix of **complete events** (runs ending with `F_LAST`,
SPEC 5.1.2) whose records all have `ts_event <= t`; at the end of the data it also releases a trailing unterminated
event (the full replay ends it synthetically too). Records past the cut stay in the cursor's private buffer and
nothing outside this module can read them.

`Tape` appends every released slice to a scratch dataset as a new part file (`mbo/date=.../instrument_id=.../
part-NNNNNN.parquet`, listed in order in `manifest.json`), with the source prints of the same span
(`trades_source/...`, which tell the engine which side-N prints are ITCH crosses, OC-28) and a `data_quality.json`
holding the instrument's feed gaps that started before the cut, each ending at most at the cut. The engine acts on a
gap only at the first event after its `ts_start`, where it censors the live shadows (`Engine::set_feed_gaps` reads
nothing else of it), so a prefix replay meets every gap at the same event as the full replay, gaps with records of the
instrument inside them included, and no time past the cut enters a replay input. Every replay the sandbox runs during
an episode reads this scratch dataset, so its input physically ends at the agent's visible cut. Prefixes nest (a later
cut only appends), which is what makes a replay of a prefix equal to the same prefix of one full replay.
`certification_view` builds the single-partition view of the original files (symlinks to the real paths resolved at
open) that the final replay and `verify` run on.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.sandbox._util import config_error, date_of_days, days_of_date, sha256_text
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table

LAST = 1 << 7
#: (modify_records_present, supports_hidden_orders) per rule set, as `qt_types::rules_for` (SPEC 3.2; checked against
#: the engine's book and trades by python/tests/sandbox/test_book.py).
RULES: dict[str, tuple[bool, bool]] = {
    "generic": (True, True),
    "nasdaq": (False, True),
    "cme": (True, False),
    "coinbase": (True, False),
    "crypto_l3": (True, False),
    "iex": (False, True),
}
PART_DIGITS = 6
_EPOCH = datetime.date(1970, 1, 1)
#: Pending records the cursor holds before it releases their complete events early (a cut is read whole, however
#: large); a buffer this large without one `F_LAST` means the data has no event boundaries.
MAX_PENDING_ROWS = 4_000_000
#: `reason` of a feed gap that has started but not ended by the cut (the source's reason may name later data).
GAP_IN_PROGRESS = "feed gap in progress at the visible cut"

#: `contain(path)`: the real path of a file the caller accepts; raises for one it refuses (MCP root containment).
Contain = Callable[[Path], Path]


def _ts_int(col: pa.ChunkedArray | pa.Array) -> np.ndarray:
    return np.asarray(col.cast(pa.int64()).to_numpy(zero_copy_only=False), dtype=np.int64)


@dataclass(frozen=True)
class Partition:
    """One (instrument, session date) partition of the source dataset."""

    instrument_id: int
    session_date: int
    files: tuple[str, ...]
    file_shas: tuple[str, ...]
    rows: int
    ts_min: int
    ts_max: int

    @property
    def date(self) -> str:
        """`YYYY-MM-DD`."""
        return date_of_days(self.session_date)


@dataclass
class SourceDataset:
    """An opened source dataset (manifest, instruments, data quality) and the resolved partition."""

    root: Path
    manifest: dict[str, Any]
    instruments: pa.Table
    partition: Partition
    instrument: dict[str, Any]
    rule_set: str
    data_quality: dict[str, Any] | None = None
    #: Real paths of the partition's `qt.mbo` files (manifest order), resolved and contained once at open.
    mbo_files: tuple[Path, ...] = ()
    #: Real paths of the partition's source print files and their names in the dataset (sorted by name).
    trades_files: tuple[Path, ...] = ()
    trades_names: tuple[str, ...] = ()
    #: Real paths of every file the sandbox reads.
    paths: list[Path] = field(default_factory=list)

    @property
    def dataset_id(self) -> str:
        """The manifest's `dataset_id`."""
        return str(self.manifest.get("dataset_id", ""))

    @property
    def modify_records_present(self) -> bool:
        """Venue rule: `M` records are meaningful (else counted and ignored)."""
        return RULES[self.rule_set][0]

    @property
    def supports_hidden_orders(self) -> bool:
        """Venue rule: a side-`N` print is a hidden print (unless the source marks it a cross)."""
        return RULES[self.rule_set][1]

    def mbo_paths(self) -> list[Path]:
        """The partition's `qt.mbo` files in order (real paths)."""
        return list(self.mbo_files)

    def trades_dir(self) -> Path:
        """`trades_source/date=YYYY-MM-DD/instrument_id=N` (the engine's rule, `Partition::source_trades_dir`)."""
        p = self.partition
        return self.root / "trades_source" / f"date={p.date}" / f"instrument_id={p.instrument_id}"

    def trades_paths(self) -> list[Path]:
        """The partition's source print files, sorted by name as the engine reads them (real paths)."""
        return list(self.trades_files)


def _read_json(path: Path, shown: Path, what: str) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QtError("QT-INPUT-001", f"{shown}: {what} not found") from None
    except (OSError, json.JSONDecodeError) as e:
        raise QtError("QT-INPUT-002", f"{shown}: {what} unreadable: {e}") from None
    if not isinstance(doc, dict):
        raise QtError("QT-INPUT-002", f"{shown}: {what} is not a JSON object")
    return doc


def _realpath(path: Path) -> Path:
    return Path(os.path.realpath(path))


def open_source(
    root: str | Path,
    *,
    instrument_id: int | None = None,
    symbol: str | None = None,
    session_date: str | None = None,
    contain: Contain | None = None,
) -> SourceDataset:
    """Opens a dataset and resolves the instrument-day (by id or symbol; `session_date` when it has several).
    `contain` (MCP local mode) is applied to every file and directory before it is read or listed; the partition's
    files are later opened only through the real paths it returned, so a link changed during the session is never
    followed."""
    real: Contain = contain if contain is not None else _realpath
    root = Path(root)
    if not root.is_dir():
        raise QtError("QT-INPUT-001", f"{root}: dataset directory not found")
    real(root)
    man_shown = root / "manifest.json"
    manifest = _read_json(real(man_shown), man_shown, "manifest.json")
    if manifest.get("schema") != "qt.dataset_manifest":
        raise QtError("QT-INPUT-002", f"{man_shown}: not a qt.dataset_manifest")
    if manifest.get("kind") != "mbo":
        raise config_error(
            "the sandbox needs an order-level (MBO) dataset: exact fills come from the exact replay",
            "L2-only datasets support simulation and markouts only (SPEC 5.1.6a)",
        )
    ipath = root / "instruments.parquet"
    if not ipath.exists():
        raise QtError("QT-INPUT-001", f"{ipath}: not found")
    ireal = real(ipath)
    instruments = pq.read_table(ireal)
    rows = instruments.to_pylist()
    for r in rows:
        sd = r.get("session_date")
        r["_days"] = (sd - _EPOCH).days if hasattr(sd, "year") else int(sd)
    parts = manifest.get("partitions") or []
    want_day = days_of_date(session_date) if session_date is not None else None
    if instrument_id is not None and symbol is not None:
        raise config_error("give instrument_id or symbol, not both")
    if symbol is not None:
        ids = {(int(r["instrument_id"]), r["_days"]) for r in rows if str(r.get("symbol")) == symbol}
        if not ids:
            ids = {
                (int(r["instrument_id"]), r["_days"]) for r in rows if str(r.get("symbol")).upper() == symbol.upper()
            }
        cands = [p for p in parts if (int(p["instrument_id"]), int(p["session_date"])) in ids]
        what = f"symbol {symbol!r}"
    elif instrument_id is not None:
        cands = [p for p in parts if int(p["instrument_id"]) == int(instrument_id)]
        what = f"instrument_id {instrument_id}"
    else:
        cands = list(parts)
        what = "the dataset"
    if want_day is not None:
        cands = [p for p in cands if int(p["session_date"]) == want_day]
    if not cands:
        avail = sorted(
            {
                f"{r.get('symbol')} (instrument_id {r['instrument_id']}, {date_of_days(r['_days'])})"
                for r in rows
                if any(
                    int(p["instrument_id"]) == int(r["instrument_id"]) and int(p["session_date"]) == r["_days"]
                    for p in parts
                )
            }
        )
        raise config_error(
            f"no partition for {what}" + (f" on {session_date}" if session_date else ""),
            "available: " + ", ".join(avail[:20]) + (" ..." if len(avail) > 20 else ""),
        )
    if len(cands) > 1:
        labels = sorted(f"instrument_id {p['instrument_id']} on {date_of_days(int(p['session_date']))}" for p in cands)
        raise config_error(
            f"{what} has {len(cands)} instrument-days: " + ", ".join(labels[:10]),
            "pass instrument_id and/or session_date to pick one",
        )
    p = cands[0]
    files = tuple(str(f["path"]) for f in p.get("files") or [])
    mbo_real = []
    for f in files:
        rel = Path(f)
        if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
            raise QtError("QT-INPUT-002", f"{man_shown}: partition file path {f!r}")
        if not (root / rel).is_file():
            raise QtError("QT-INPUT-001", f"{root / rel}: partition file listed in manifest.json is missing")
        mbo_real.append(real(root / rel))
    part = Partition(
        instrument_id=int(p["instrument_id"]),
        session_date=int(p["session_date"]),
        files=files,
        file_shas=tuple(str(f.get("sha256", "")) for f in p.get("files") or []),
        rows=int(p.get("rows", 0)),
        ts_min=int(p.get("ts_min", 0)),
        ts_max=int(p.get("ts_max", 0)),
    )
    inst = next(
        (r for r in rows if int(r["instrument_id"]) == part.instrument_id and r["_days"] == part.session_date), None
    )
    if inst is None:
        raise QtError(
            "QT-INPUT-002",
            f"{ipath}: no qt.instruments row for instrument_id {part.instrument_id} on {part.date}",
            "the sandbox needs the instrument's tick size, units and session times",
        )
    rule_set = str(manifest.get("rule_set") or manifest.get("venue") or "generic")
    if rule_set not in RULES:
        raise config_error(f"unknown rule set {rule_set!r} in manifest.json")
    dq_path = root / "data_quality.json"
    dq_real = real(dq_path) if dq_path.exists() else None
    dq = _read_json(dq_real, dq_path, "data_quality.json") if dq_real is not None else None
    src = SourceDataset(
        root=root,
        manifest=manifest,
        instruments=instruments,
        partition=part,
        instrument=inst,
        rule_set=rule_set,
        data_quality=dq,
        mbo_files=tuple(mbo_real),
    )
    tdir = src.trades_dir()
    if tdir.is_dir():
        names = sorted(
            x.name for x in real(tdir).iterdir() if x.name.startswith("part-") and x.name.endswith(".parquet")
        )
        src.trades_names = tuple(names)
        src.trades_files = tuple(real(tdir / n) for n in names)
    src.paths = [real(man_shown), ireal, *src.mbo_files, *src.trades_files]
    if dq_real is not None:
        src.paths.append(dq_real)
    return src


def check_partition_files(src: SourceDataset) -> list[str]:
    """Problems of the partition's `qt.mbo` files against the manifest (empty when every file's sha256 is the one
    listed): the manifest's `dataset_id` names the data only while the files are the ones it lists."""
    problems = []
    for rel, path, want in zip(src.partition.files, src.mbo_files, src.partition.file_shas, strict=True):
        if not want:
            problems.append(f"{rel}: manifest.json lists no sha256 for it")
            continue
        got = sha256_file(path)
        if got != want:
            problems.append(f"{rel}: sha256 {got[:16]}... is not the {want[:16]}... manifest.json lists")
    return problems


class SourceCursor:
    """Forward-only reader of `qt.mbo` files releasing complete events up to a cut (module docstring)."""

    def __init__(self, files: list[Path], *, batch_rows: int = 65_536) -> None:
        self._files = list(files)
        self._fi = 0
        self._it: Any = None
        self._batch_rows = batch_rows
        # Pending records (read, not released) in feed order: a consolidated head and the batches read since.
        self._pending: pa.Table | None = None
        self._ts = np.zeros(0, dtype=np.int64)
        self._flags = np.zeros(0, dtype=np.uint8)
        self._new: list[tuple[pa.Table, np.ndarray, np.ndarray]] = []
        self._n = 0
        self._exhausted = False
        self.rows_released = 0
        self.last_ts: int | None = None
        #: Records whose `ts_event` is below the previous record's (feed order is never re-sorted, SPEC 5.1.3).
        self.ts_decreases = 0
        #: Complete events whose records do not all carry one `ts_event`.
        self.varying_events = 0

    @property
    def eof(self) -> bool:
        """True once every record of the partition has been released."""
        return self._exhausted and self._n == 0

    def _read_batch(self) -> np.ndarray | None:
        """Reads the next non-empty batch into the pending records; returns its `ts_event` (None at the end)."""
        while True:
            if self._it is None:
                if self._fi >= len(self._files):
                    return None
                pf = pq.ParquetFile(self._files[self._fi])
                meta = pf.schema_arrow.metadata or {}
                if meta.get(b"qt.schema", b"qt.mbo") != b"qt.mbo":
                    raise QtError("QT-INPUT-002", f"{self._files[self._fi]}: not a qt.mbo table")
                self._it = pf.iter_batches(batch_size=self._batch_rows)
                self._fi += 1
            try:
                batch = next(self._it)
            except StopIteration:
                self._it = None
                continue
            if batch.num_rows == 0:
                continue
            ts = _ts_int(batch.column("ts_event"))
            flags = np.asarray(batch.column("flags").to_numpy(zero_copy_only=False), dtype=np.uint8)
            self._new.append((pa.Table.from_batches([batch]), ts, flags))
            self._n += batch.num_rows
            return ts

    def _consolidate(self) -> None:
        if not self._new:
            return
        tabs = [t for t, _, _ in self._new]
        self._pending = pa.concat_tables(tabs if self._pending is None else [self._pending, *tabs])
        self._ts = np.concatenate([self._ts, *(ts for _, ts, _ in self._new)])
        self._flags = np.concatenate([self._flags, *(fl for _, _, fl in self._new)])
        self._new = []

    def _boundary(self, limit: int) -> int:
        """Records of the complete events among the first `limit` pending records."""
        lasts = np.flatnonzero(self._flags[:limit] & LAST)
        return int(lasts[-1]) + 1 if lasts.size else 0

    @staticmethod
    def _no_boundary(n: int) -> QtError:
        return config_error(
            f"{n} records without an F_LAST event boundary: the dataset has no event structure",
            "the exact replay needs F_LAST flags (SPEC 5.1.2)",
        )

    def _release(self, k: int) -> pa.Table:
        """Releases the first `k` pending records (consolidated) and updates the counters."""
        assert self._pending is not None and not self._new
        out = self._pending.slice(0, k)
        ts = self._ts[:k]
        fl = self._flags[:k]
        prev = self.last_ts if self.last_ts is not None else int(ts[0])
        self.ts_decreases += int((np.diff(np.concatenate([[prev], ts])) < 0).sum())
        ends = np.flatnonzero(fl & LAST)
        if ends.size:
            starts = np.concatenate([[0], ends[:-1] + 1])
            self.varying_events += int((ts[starts] != ts[ends]).sum())
        self.last_ts = int(ts[-1])
        self.rows_released += k
        rest = self._pending.slice(k)
        self._pending = rest if rest.num_rows else None
        self._ts = self._ts[k:].copy()
        self._flags = self._flags[k:].copy()
        self._n -= k
        return out

    def take_until(self, t: int) -> pa.Table | None:
        """Releases the records of the cut at `t` not released yet (None when there are none). Reads until a record
        past `t` or the end of the data, however far that is; while every pending record is at or before `t` and
        their number passes `MAX_PENDING_ROWS`, their complete events are released early into the result, so the
        buffer stays bounded and the result is still exactly the cut."""
        out: list[pa.Table] = []
        self._consolidate()
        over = np.flatnonzero(self._ts > t)
        limit: int | None = int(over[0]) if over.size else None
        while limit is None and not self._exhausted:
            if self._n > MAX_PENDING_ROWS:
                self._consolidate()
                k = self._boundary(self._n)
                if k == 0:
                    raise self._no_boundary(self._n)
                out.append(self._release(k))
            ts = self._read_batch()
            if ts is None:
                self._exhausted = True
                break
            hit = np.flatnonzero(ts > t)
            if hit.size:
                limit = self._n - ts.size + int(hit[0])
        self._consolidate()
        if limit is None:  # the data ended with every remaining record at or before t: the unterminated tail goes too
            cut = self._n
        else:
            cut = self._boundary(limit)
            if cut == 0 and limit > MAX_PENDING_ROWS:
                raise self._no_boundary(limit)
        if cut:
            out.append(self._release(cut))
        if not out:
            return None
        return out[0] if len(out) == 1 else pa.concat_tables(out)


class _PrintCursor:
    """Forward-only reader of `qt.trades` source prints: releases the in-order prefix with `ts_event <= t`."""

    def __init__(self, files: list[Path]) -> None:
        self._files = list(files)
        self._fi = 0
        self._pending: pa.Table | None = None
        self._ts = np.zeros(0, dtype=np.int64)
        self._done = not self._files

    def take_until(self, t: int) -> pa.Table | None:
        while not self._done and not np.any(self._ts > t):
            if self._fi >= len(self._files):
                self._done = True
                break
            tab = pq.read_table(self._files[self._fi])
            self._fi += 1
            if tab.num_rows:
                self._pending = tab if self._pending is None else pa.concat_tables([self._pending, tab])
                self._ts = np.concatenate([self._ts, _ts_int(tab.column("ts_event"))])
        over = np.flatnonzero(self._ts > t)
        cut = int(over[0]) if over.size else int(self._ts.size)
        if cut == 0 or self._pending is None:
            return None
        out = self._pending.slice(0, cut)
        rest = self._pending.slice(cut)
        self._pending = rest if rest.num_rows else None
        self._ts = self._ts[cut:]
        return out


def _instrument_row(src: SourceDataset) -> pa.Table:
    days = pc.cast(src.instruments.column("session_date"), pa.int32())
    mask = pc.and_(
        pc.equal(src.instruments.column("instrument_id"), pa.scalar(src.partition.instrument_id, pa.uint32())),
        pc.equal(days, src.partition.session_date),
    )
    return src.instruments.filter(mask)


def _gap_doc(src: SourceDataset, cut: int | None) -> dict[str, Any] | None:
    """`data_quality.json` of a cut: the source document with every counter zeroed and the instrument's feed gaps
    with `ts_start < cut`, each ending at most at `cut` (a gap still in progress there gets `GAP_IN_PROGRESS` as its
    reason); with `cut = None` the source document with all of the instrument's gaps as they are (module docstring:
    the engine acts on a gap at the first event after its start and reads nothing else of it). The instrument's
    trading halts (`trading_halts`, SPEC 1.2) are cut the same way: a halt the feed announced before the cut is
    known at the cut, its end and any later halt are not."""
    if src.data_quality is None:
        return None
    doc = json.loads(json.dumps(src.data_quality))
    gaps = []
    for g in doc.get("feed_gaps") or []:
        if int(g.get("instrument_id", -1)) != src.partition.instrument_id:
            continue
        if cut is not None:
            if int(g["ts_start"]) >= cut:
                continue
            if int(g["ts_end"]) > cut:
                g = {**g, "ts_end": cut, "reason": GAP_IN_PROGRESS}
        gaps.append(g)
    halts = []
    for h in doc.get("trading_halts") or []:
        if int(h.get("instrument_id", -1)) != src.partition.instrument_id:
            continue
        if cut is not None:
            if int(h["ts_start"]) >= cut:
                continue
            if int(h["ts_end"]) > cut:
                h = {**h, "ts_end": cut}
        halts.append(h)
    if halts:
        doc["trading_halts"] = halts
    else:
        doc.pop("trading_halts", None)
    if cut is not None:
        for k, v in list(doc.items()):
            if isinstance(v, int) and not isinstance(v, bool) and k != "schema_version":
                doc[k] = 0
        doc["source_specific"] = {}
        doc["verdict"] = "clean"
        doc["reasons"] = []
    doc["feed_gaps"] = gaps
    return doc


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Tape:
    """The scratch dataset holding the source partition up to the visible cut (module docstring)."""

    def __init__(self, src: SourceDataset, root: Path, *, compact_every: int = 32, batch_rows: int = 65_536) -> None:
        self.src = src
        self.root = Path(root)
        if self.root.exists():
            shutil.rmtree(self.root)
        p = src.partition
        self._mbo_dir = f"mbo/date={p.date}/instrument_id={p.instrument_id}"
        self._trades_dir = f"trades_source/date={p.date}/instrument_id={p.instrument_id}"
        (self.root / self._mbo_dir).mkdir(parents=True)
        self._cursor = SourceCursor(src.mbo_paths(), batch_rows=batch_rows)
        self._prints = _PrintCursor(src.trades_paths())
        self._compact_every = compact_every
        self._parts: list[tuple[str, int, str]] = []  # (relative path, rows, sha256)
        self._trade_parts: list[str] = []
        self._next = 0
        self.rows = 0
        self.ts_min: int | None = None
        self.ts_max: int | None = None
        self.cut_ns: int | None = None
        self._dq_written: str | None = None
        #: The source manifest without its partitions (a full-market manifest lists thousands), copied once.
        self._template = json.loads(json.dumps({k: v for k, v in src.manifest.items() if k != "partitions"}))
        self._template["price_exp_by_instrument"] = {
            k: v for k, v in (self._template.get("price_exp_by_instrument") or {}).items() if k == str(p.instrument_id)
        }
        write_table(_instrument_row(src), self.root / "instruments.parquet")
        self._write_manifest()
        self._write_dq(None)

    # -- public ---------------------------------------------------------------------------------------------------

    @property
    def cursor(self) -> SourceCursor:
        """The source cursor (counters only; it has no accessor for records past the cut)."""
        return self._cursor

    def extend_to(self, t: int) -> tuple[pa.Table | None, pa.Table | None]:
        """Advances the cut to `t` (never backwards): returns the newly released `qt.mbo` records and source prints
        (None when there are none) after writing them to the scratch dataset."""
        if self.cut_ns is not None and t < self.cut_ns:
            raise QtError("QT-ENGINE-001", f"tape cut moved backwards ({t} < {self.cut_ns})")
        self.cut_ns = t
        mbo = self._cursor.take_until(t)
        prints = self._prints.take_until(t)
        if mbo is not None:
            rel = f"{self._mbo_dir}/part-{self._next:0{PART_DIGITS}d}.parquet"
            entry = write_table(mbo, self.root / rel, SCHEMAS["qt.mbo"])
            self._parts.append((rel, int(entry["rows"]), str(entry["sha256"])))
            ts = _ts_int(mbo.column("ts_event"))
            self.rows += mbo.num_rows
            self.ts_min = int(ts.min()) if self.ts_min is None else min(self.ts_min, int(ts.min()))
            self.ts_max = int(ts.max()) if self.ts_max is None else max(self.ts_max, int(ts.max()))
        if prints is not None:
            rel = f"{self._trades_dir}/part-{self._next:0{PART_DIGITS}d}.parquet"
            write_table(prints, self.root / rel, SCHEMAS["qt.trades"])
            self._trade_parts.append(rel)
        if mbo is not None or prints is not None:
            self._next += 1
        if len(self._parts) > self._compact_every or len(self._trade_parts) > self._compact_every:
            self._compact()
        self._write_manifest()
        self._write_dq(t)
        return mbo, prints

    @property
    def empty(self) -> bool:
        """No record released yet."""
        return self.rows == 0

    def files(self) -> list[Path]:
        """Every file of the scratch dataset (absolute)."""
        return sorted(p for p in self.root.rglob("*") if p.is_file())

    # -- internals ------------------------------------------------------------------------------------------------

    def _compact(self) -> None:
        if len(self._parts) > 1:
            tab = pa.concat_tables([pq.read_table(self.root / rel) for rel, _, _ in self._parts])
            rel = f"{self._mbo_dir}/part-{self._next:0{PART_DIGITS}d}.parquet"
            entry = write_table(tab, self.root / rel, SCHEMAS["qt.mbo"])
            for old, _, _ in self._parts:
                (self.root / old).unlink()
            self._parts = [(rel, int(entry["rows"]), str(entry["sha256"]))]
        if len(self._trade_parts) > 1:
            tab = pa.concat_tables([pq.read_table(self.root / rel) for rel in self._trade_parts])
            rel = f"{self._trades_dir}/part-{self._next:0{PART_DIGITS}d}.parquet"
            write_table(tab, self.root / rel, SCHEMAS["qt.trades"])
            for old in self._trade_parts:
                (self.root / old).unlink()
            self._trade_parts = [rel]
        self._next += 1

    def _write_manifest(self) -> None:
        p = self.src.partition
        man = dict(self._template)
        if self._parts:
            man["partitions"] = [
                {
                    "instrument_id": p.instrument_id,
                    "session_date": p.session_date,
                    "files": [{"path": rel, "rows": rows, "sha256": sha} for rel, rows, sha in self._parts],
                    "rows": self.rows,
                    "ts_min": self.ts_min,
                    "ts_max": self.ts_max,
                }
            ]
        else:
            man["partitions"] = []
        shas = sorted(sha for _, _, sha in self._parts)
        man["dataset_id"] = sha256_text("\n".join(shas) + "\n" + str(man.get("source_format", "")))
        _write_json(self.root / "manifest.json", man)

    def _write_dq(self, cut: int | None) -> None:
        doc = _gap_doc(self.src, cut if cut is not None else -(2**63))
        if doc is None:
            return
        text = json.dumps(doc, sort_keys=True)
        if text != self._dq_written:
            _write_json(self.root / "data_quality.json", doc)
            self._dq_written = text


def certification_view(src: SourceDataset, root: Path) -> Path:
    """A single-partition view of the original files (symlinks to the real paths resolved at open, under the
    dataset's own names; nothing copied but small JSON and the instrument row), equivalent to the source dataset for
    that partition (partitions replay independently, SPEC 3.3)."""
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    p = src.partition
    for rel, target in zip(p.files, src.mbo_files, strict=True):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, dst)
    tdir = root / "trades_source" / f"date={p.date}" / f"instrument_id={p.instrument_id}"
    for name, target in zip(src.trades_names, src.trades_files, strict=True):
        tdir.mkdir(parents=True, exist_ok=True)
        os.symlink(target, tdir / name)
    write_table(_instrument_row(src), root / "instruments.parquet")
    man = json.loads(json.dumps(src.manifest))
    man["partitions"] = [
        q
        for q in man.get("partitions") or []
        if int(q["instrument_id"]) == p.instrument_id and int(q["session_date"]) == p.session_date
    ]
    _write_json(root / "manifest.json", man)
    dq = _gap_doc(src, None)
    if dq is not None:
        _write_json(root / "data_quality.json", dq)
    return root


__all__ = [
    "GAP_IN_PROGRESS",
    "LAST",
    "RULES",
    "Contain",
    "Partition",
    "SourceCursor",
    "SourceDataset",
    "Tape",
    "certification_view",
    "check_partition_files",
    "open_source",
]
